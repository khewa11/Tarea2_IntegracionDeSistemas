"""Saga ORQUESTADA de reserva de estacionamiento.

Avance:        reservar espacio -> cobrar -> habilitar acceso
Compensación:  revocar acceso -> reembolsar -> liberar espacio (orden inverso)

Estados persistidos:
    INICIADA -> ESPACIO_RESERVADO -> PAGO_COBRADO -> COMPLETADA
    INICIADA -> ESPACIO_RESERVADO -> PAGO_PENDIENTE -> PAGO_COBRADO -> COMPLETADA   (Webpay)
    PAGO_PENDIENTE -> COMPENSANDO -> COMPENSADA   (rechazado, anulado o PAGO_TIMEOUT_S vencido)
    FALLIDA                  rechazo antes de producir efectos (nada que compensar)
    COMPENSANDO -> COMPENSADA
    COMPENSANDO -> COMPENSACION_PENDIENTE (alguna compensación no se pudo aplicar;
                                           se reintenta en segundo plano)

Regla clave: el estado se guarda en la base ANTES de llamar al siguiente servicio.
La columna `avance` indica el último paso intentado, de modo que tras una caída del
orquestador se sabe exactamente qué compensar. Todas las operaciones de A son
idempotentes por sagaId, así que reintentar una compensación nunca duplica efectos.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Callable

from .clientes import Dependencia, FalloDependencia, RechazoNegocio
from .db import BaseSagas
from .logs import log
from .resiliencia import BulkheadLleno, CircuitoAbierto

# Estados
INICIADA = "INICIADA"
ESPACIO_RESERVADO = "ESPACIO_RESERVADO"
PAGO_COBRADO = "PAGO_COBRADO"
PAGO_PENDIENTE = "PAGO_PENDIENTE"
COMPLETADA = "COMPLETADA"
FALLIDA = "FALLIDA"
COMPENSANDO = "COMPENSANDO"
COMPENSADA = "COMPENSADA"
COMPENSACION_PENDIENTE = "COMPENSACION_PENDIENTE"

FINALES = (COMPLETADA, FALLIDA, COMPENSADA)
NO_FINALES = (INICIADA, ESPACIO_RESERVADO, PAGO_PENDIENTE, PAGO_COBRADO, COMPENSANDO, COMPENSACION_PENDIENTE)

# Pasos de avance (columna `avance`)
NINGUNO, RESERVAR, COBRAR, HABILITAR = "NINGUNO", "RESERVAR", "COBRAR", "HABILITAR"

# Compensaciones necesarias según el último paso intentado, ya en orden inverso.
# El paso intentado se incluye porque pudo aplicarse aunque la respuesta no llegara
# (timeout): por eso tras fallar "habilitar" se revoca de forma preventiva.
COMPENSACIONES = {
    NINGUNO: [],
    RESERVAR: ["LIBERAR"],
    COBRAR: ["REEMBOLSAR", "LIBERAR"],
    HABILITAR: ["REVOCAR", "REEMBOLSAR", "LIBERAR"],
}
ANTERIOR = {RESERVAR: NINGUNO, COBRAR: RESERVAR, HABILITAR: COBRAR}


def _monto_json(monto: Decimal) -> int | float:
    return int(monto) if monto == monto.to_integral_value() else float(monto)


class Orquestador:
    def __init__(self, db: BaseSagas, espacios: Dependencia, pagos: Dependencia, acceso: Dependencia,
                 reintentos_compensacion: int = 3, pausa_compensacion_s: float = 0.3,
                 pago_timeout_s: float = 300.0, reloj_pared: Callable[[], float] = time.time):
        self.db = db
        self.espacios = espacios
        self.pagos = pagos
        self.acceso = acceso
        self.reintentos = reintentos_compensacion
        self.pausa = pausa_compensacion_s
        self.pago_timeout_s = pago_timeout_s
        self._reloj_pared = reloj_pared
        self._activas: set[str] = set()

    @property
    def dependencias(self) -> list[Dependencia]:
        return [self.espacios, self.pagos, self.acceso]

    # ------------------------------------------------------------------ avance
    async def ejecutar(self, saga_id: str, espacio_id: str, patente: str, monto: Decimal) -> dict:
        # Fallback rápido: si algún circuito está abierto no se toca ningún servicio,
        # así se evita reservar y cobrar algo que de todos modos habría que devolver.
        for dep in self.dependencias:
            if dep.breaker.rechaza_llamadas():
                motivo = f"CIRCUITO_ABIERTO_{dep.nombre.upper()}"
                if not self.db.crear(saga_id, espacio_id, patente, str(monto), FALLIDA, NINGUNO, motivo):
                    return self.db.obtener(saga_id)
                dep.breaker.rechazos += 1
                dep.metricas.registrar("circuito_abierto", None)
                log("saga.rechazo_rapido", FALLIDA, saga_id, motivo=motivo)
                return self.db.obtener(saga_id)

        if not self.db.crear(saga_id, espacio_id, patente, str(monto), INICIADA, RESERVAR):
            return self.db.obtener(saga_id)  # sagaId repetido: se devuelve el estado vigente
        log("saga.inicio", INICIADA, saga_id, espacioId=espacio_id)

        self._activas.add(saga_id)
        try:
            return await self._avanzar(saga_id, espacio_id, patente, monto)
        finally:
            self._activas.discard(saga_id)

    async def _avanzar(self, saga_id: str, espacio_id: str, patente: str, monto: Decimal,
                       desde: int = 0) -> dict:
        pasos = [
            (RESERVAR, ESPACIO_RESERVADO, self.espacios, "/reservas",
             {"sagaId": saga_id, "espacioId": espacio_id}),
            (COBRAR, PAGO_COBRADO, self.pagos, "/cobros",
             {"sagaId": saga_id, "monto": _monto_json(monto)}),
            (HABILITAR, COMPLETADA, self.acceso, "/habilitaciones",
             {"sagaId": saga_id, "patente": patente, "espacioId": espacio_id}),
        ]
        for i, (paso, estado_ok, dep, ruta, cuerpo) in enumerate(pasos):
            if i < desde:
                continue
            try:
                respuesta = await dep.llamar("POST", ruta, json=cuerpo, saga_id=saga_id, paso=f"{dep.nombre}.{paso.lower()}")
            except (CircuitoAbierto, BulkheadLleno) as exc:
                # La llamada ni siquiera se envió: solo se compensan los pasos previos.
                motivo = ("CIRCUITO_ABIERTO_" if isinstance(exc, CircuitoAbierto) else "BULKHEAD_LLENO_") \
                    + dep.nombre.upper()
                return await self._fallar(saga_id, ANTERIOR[paso], motivo)
            except RechazoNegocio as exc:
                # Rechazo explícito: el paso no se aplicó.
                return await self._fallar(saga_id, ANTERIOR[paso], exc.error or f"HTTP_{exc.codigo}")
            except FalloDependencia as exc:
                # Timeout / 5xx: el paso pudo haberse aplicado, se compensa también.
                motivo = f"{dep.nombre.upper()}_{exc.tipo.upper()}" + (f"_{exc.codigo}" if exc.codigo else "")
                return await self._fallar(saga_id, paso, motivo)

            if paso == COBRAR and respuesta.get("estado") == "PENDIENTE":
                # Pasarela asíncrona: el usuario debe pagar en Webpay. La saga queda a la
                # espera del retorno (o de PAGO_TIMEOUT_S); avance sigue en COBRAR para que
                # la compensación incluya reembolsar/anular el cobro y liberar el espacio.
                limite = self._reloj_pared() + self.pago_timeout_s
                self.db.registrar_pago(saga_id, respuesta.get("token"), respuesta.get("url"),
                                       respuesta.get("buyOrder"), limite)
                self.db.transicion(saga_id, PAGO_PENDIENTE, avance=COBRAR, detalle="esperando pago en Webpay")
                log("saga.transicion", PAGO_PENDIENTE, saga_id, timeoutPagoS=self.pago_timeout_s)
                return self.db.obtener(saga_id)

            siguiente = pasos[i + 1][0] if i + 1 < len(pasos) else None
            self.db.transicion(saga_id, estado_ok, avance=siguiente, detalle=f"{paso} ok")
            log("saga.transicion", estado_ok, saga_id)
        return self.db.obtener(saga_id)

    # ------------------------------------------------------- pago asíncrono
    async def confirmar_pago(self, saga_id: str, token: str) -> dict:
        """Retorno de Webpay con token_ws: commit en Pagos y, si se aprueba, sigue con Acceso."""
        saga = self.db.obtener(saga_id)
        if saga is None or saga["estado"] != PAGO_PENDIENTE or saga_id in self._activas:
            return saga  # retorno repetido o concurrente: se informa el estado vigente
        self._activas.add(saga_id)
        try:
            # Regla de la saga: persistir antes de llamar.
            self.db.transicion(saga_id, PAGO_PENDIENTE, avance=COBRAR, detalle="confirmando pago")
            log("pago.retorno", "CONFIRMANDO", saga_id)
            try:
                cuerpo = await self.pagos.llamar("POST", f"/cobros/{saga_id}/confirmar", json={"token": token},
                                                 saga_id=saga_id, paso="pagos.confirmar")
            except (CircuitoAbierto, BulkheadLleno):
                # El usuario pudo haber pagado: Pagos consultará Webpay al compensar.
                return await self.compensar(saga_id, COBRAR, "CIRCUITO_ABIERTO_PAGOS")
            except RechazoNegocio as exc:
                return await self.compensar(saga_id, COBRAR, exc.error or f"HTTP_{exc.codigo}")
            except FalloDependencia as exc:
                motivo = f"PAGOS_{exc.tipo.upper()}" + (f"_{exc.codigo}" if exc.codigo else "")
                return await self.compensar(saga_id, COBRAR, motivo)

            if cuerpo.get("estado") != "COBRADO":
                log("pago.resultado", "RECHAZADO", saga_id, responseCode=cuerpo.get("responseCode"))
                return await self.compensar(saga_id, COBRAR, "PAGO_RECHAZADO")
            log("pago.resultado", "COBRADO", saga_id)
            self.db.transicion(saga_id, PAGO_COBRADO, avance=HABILITAR, detalle="COBRAR ok (Webpay)")
            log("saga.transicion", PAGO_COBRADO, saga_id)
            return await self._avanzar(saga_id, saga["espacioId"], saga["patente"], Decimal(saga["monto"]), desde=2)
        finally:
            self._activas.discard(saga_id)

    async def anular_pago(self, saga_id: str, motivo: str = "PAGO_ANULADO") -> dict:
        """El usuario anuló en Webpay (TBK_TOKEN) o venció el formulario: compensa."""
        saga = self.db.obtener(saga_id)
        if saga is None or saga["estado"] != PAGO_PENDIENTE or saga_id in self._activas:
            return saga
        self._activas.add(saga_id)
        try:
            log("pago.retorno", motivo, saga_id)
            return await self.compensar(saga_id, COBRAR, motivo)
        finally:
            self._activas.discard(saga_id)

    async def _fallar(self, saga_id: str, avance_efectivo: str, motivo: str) -> dict:
        if not COMPENSACIONES[avance_efectivo]:
            self.db.transicion(saga_id, FALLIDA, avance=NINGUNO, motivo=motivo)
            log("saga.fin", FALLIDA, saga_id, motivo=motivo)
            return self.db.obtener(saga_id)
        return await self.compensar(saga_id, avance_efectivo, motivo)

    # ------------------------------------------------------------ compensación
    async def compensar(self, saga_id: str, avance: str, motivo: str | None = None) -> dict:
        self.db.transicion(saga_id, COMPENSANDO, avance=avance, motivo=motivo,
                           detalle=f"compensando tras {avance}: {motivo}")
        log("saga.compensacion_inicio", COMPENSANDO, saga_id, motivo=motivo, avance=avance)

        pendientes = []
        for accion in COMPENSACIONES[avance]:
            if not await self._compensar_uno(saga_id, accion):
                pendientes.append(accion)

        if pendientes:
            detalle = "pendiente: " + ",".join(pendientes)
            self.db.transicion(saga_id, COMPENSACION_PENDIENTE, detalle=detalle)
            log("saga.fin", COMPENSACION_PENDIENTE, saga_id, pendientes=pendientes)
        else:
            self.db.transicion(saga_id, COMPENSADA, detalle="compensación completa")
            log("saga.fin", COMPENSADA, saga_id, motivo=motivo)
        return self.db.obtener(saga_id)

    async def _compensar_uno(self, saga_id: str, accion: str) -> bool:
        dep, ruta = {
            "REVOCAR": (self.acceso, f"/habilitaciones/{saga_id}/revocar"),
            "REEMBOLSAR": (self.pagos, f"/cobros/{saga_id}/reembolso"),
            "LIBERAR": (self.espacios, f"/reservas/{saga_id}/liberar"),
        }[accion]
        for intento in range(1, self.reintentos + 1):
            try:
                cuerpo = await dep.llamar("POST", ruta, saga_id=saga_id,
                                          paso=f"{dep.nombre}.{accion.lower()}", proteger=False)
                self.db.transicion(saga_id, COMPENSANDO, detalle=f"{accion} ok ({cuerpo.get('estado')})")
                return True
            except RechazoNegocio as exc:
                # 4xx en una compensación: el servicio la consideró innecesaria/inválida.
                # No tiene sentido reintentar; se registra y se da por aplicada.
                self.db.transicion(saga_id, COMPENSANDO, detalle=f"{accion} rechazada {exc.codigo} {exc.error}")
                return True
            except FalloDependencia:
                if intento < self.reintentos:
                    await asyncio.sleep(self.pausa * intento)
        self.db.transicion(saga_id, COMPENSANDO, detalle=f"{accion} falló tras {self.reintentos} intentos")
        return False

    # ------------------------------------------------------------ recuperación
    async def recuperar(self) -> int:
        """Compensa sagas que quedaron a medias (caída del orquestador o compensación pendiente)."""
        recuperadas = 0
        for saga in self.db.pendientes(NO_FINALES):
            saga_id = saga["sagaId"]
            if saga_id in self._activas:
                continue
            motivo = saga["motivo"] or "RECUPERACION"
            if saga["estado"] == PAGO_PENDIENTE:
                limite = (saga.get("pago") or {}).get("limite")
                if limite is not None and self._reloj_pared() < limite:
                    continue  # el usuario aún puede pagar
                motivo = "PAGO_TIMEOUT"
            self._activas.add(saga_id)
            try:
                log("saga.recuperacion", saga["estado"], saga_id, avance=saga["avance"])
                await self.compensar(saga_id, saga["avance"], motivo)
                recuperadas += 1
            finally:
                self._activas.discard(saga_id)
        return recuperadas
