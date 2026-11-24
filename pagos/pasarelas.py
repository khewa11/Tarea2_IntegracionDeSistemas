"""Adaptadores de pasarela de pago (patrón Adaptador).

El microservicio Pagos conserva la persistencia, la idempotencia por sagaId y las
"lápidas" de compensación; la pasarela solo traduce operaciones hacia el proveedor:

    iniciar(sagaId, monto)   -> cobro inmediato (simulada) o transacción PENDIENTE (Webpay)
    confirmar(token)         -> resultado de la autorización (solo pasarelas asíncronas)
    reembolsar(token, monto) -> devolución del monto total

Se elige con la variable de entorno PASARELA=simulada|webpay (por defecto: simulada).
"""
from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


class ErrorPasarela(Exception):
    """Fallo técnico o rechazo de la API del proveedor (no es un rechazo de la tarjeta)."""

    def __init__(self, operacion: str, detalle: str, codigo: int | None = None):
        super().__init__(f"{operacion}: {detalle}")
        self.operacion = operacion
        self.detalle = detalle
        self.codigo = codigo


@dataclass(frozen=True)
class Inicio:
    estado: str                    # "COBRADO" (síncrona) o "PENDIENTE" (requiere redirección)
    referencia: str
    token: str | None = None
    url: str | None = None
    buy_order: str | None = None


@dataclass(frozen=True)
class Confirmacion:
    aprobado: bool
    estado_proveedor: str | None
    response_code: int | None
    authorization_code: str | None


@dataclass(frozen=True)
class Reembolso:
    tipo: str | None
    response_code: int | None


class Pasarela(ABC):
    nombre: str
    asincrona: bool

    @abstractmethod
    def iniciar(self, saga_id: str, monto_centavos: int) -> Inicio: ...

    @abstractmethod
    def confirmar(self, token: str) -> Confirmacion: ...

    @abstractmethod
    def reembolsar(self, token: str | None, monto_centavos: int) -> Reembolso: ...

    def consultar(self, token: str) -> str | None:
        """Estado de la transacción en el proveedor (p. ej. AUTHORIZED); None si no aplica."""
        return None


class PasarelaSimulada(Pasarela):
    """Comportamiento histórico: cobro inmediato con referencia SIM-<sagaId>, sin red."""

    nombre = "SIMULADA"
    asincrona = False

    def iniciar(self, saga_id: str, monto_centavos: int) -> Inicio:
        return Inicio(estado="COBRADO", referencia="SIM-" + saga_id)

    def confirmar(self, token: str) -> Confirmacion:
        raise ErrorPasarela("confirmar", "La pasarela simulada no requiere confirmación")

    def reembolsar(self, token: str | None, monto_centavos: int) -> Reembolso:
        return Reembolso(tipo="SIMULADO", response_code=0)


BUY_ORDER_MAX = 26     # límite de Webpay Plus (ApiConstants.BUY_ORDER_LENGTH del SDK)
SESSION_ID_MAX = 61    # ApiConstants.SESSION_ID_LENGTH


def buy_order_de(saga_id: str) -> str:
    """buy_order determinista derivado del sagaId (máx. 26 caracteres).

    Si el sagaId cabe se usa tal cual (legible en el portal de Transbank); si no,
    se usa un hash estable, de modo que reintentar con el mismo sagaId produce la
    misma orden de compra.
    """
    if len(saga_id) <= BUY_ORDER_MAX:
        return saga_id
    return "B" + hashlib.sha256(saga_id.encode("utf-8")).hexdigest()[: BUY_ORDER_MAX - 1]


class PasarelaWebpay(Pasarela):
    """Webpay Plus (Transbank) mediante el SDK oficial `transbank-sdk`.

    Solo ambiente de INTEGRACIÓN. El objeto `transaccion` se puede inyectar (pruebas
    sin red); si no se entrega, se construye con el SDK:
        Transaction(WebpayOptions(commerce_code, api_key, IntegrationType.TEST, timeout))
    Importante: nunca registrar el objeto WebpayOptions, su __repr__ incluye la API key.
    """

    nombre = "WEBPAY_PLUS_INTEGRACION"
    asincrona = True

    def __init__(self, return_url: str, transaccion=None, commerce_code: str | None = None,
                 api_key: str | None = None, timeout_s: float = 8.0):
        if not return_url:
            raise ValueError("PAGOS_RETURN_URL es obligatoria con PASARELA=webpay")
        self.return_url = return_url
        if transaccion is None:
            from transbank.common.integration_api_keys import IntegrationApiKeys
            from transbank.common.integration_commerce_codes import IntegrationCommerceCodes
            from transbank.common.integration_type import IntegrationType
            from transbank.common.options import WebpayOptions
            from transbank.webpay.webpay_plus.transaction import Transaction

            opciones = WebpayOptions(commerce_code or IntegrationCommerceCodes.WEBPAY_PLUS,
                                     api_key or IntegrationApiKeys.WEBPAY,
                                     IntegrationType.TEST, timeout=timeout_s)
            transaccion = Transaction(opciones)
        self._tx = transaccion
        # Valores que jamás deben salir en respuestas ni logs (p. ej. si el proveedor los
        # repite en un mensaje de error).
        opciones_tx = getattr(transaccion, "options", None)
        self._secretos = [v for v in (api_key, getattr(opciones_tx, "api_key", None)) if v]

    def _sanear(self, texto: str) -> str:
        for secreto in self._secretos:
            texto = texto.replace(secreto, "***")
        return texto[:200]

    def _llamar(self, operacion: str, funcion, *args):
        try:
            return funcion(*args)
        except Exception as exc:  # noqa: BLE001 — se traduce a un error propio sin datos sensibles
            nombre = type(exc).__name__
            if "Timeout" in nombre:
                raise ErrorPasarela(operacion, "timeout hacia Webpay", 504) from None
            if hasattr(exc, "message") and hasattr(exc, "code"):  # TransbankError y derivados
                raise ErrorPasarela(operacion, self._sanear(str(exc.message)), getattr(exc, "code", None) or 502) from None
            raise ErrorPasarela(operacion, f"error de comunicación con Webpay ({nombre})", 502) from None

    def iniciar(self, saga_id: str, monto_centavos: int) -> Inicio:
        if monto_centavos % 100:
            raise ValueError("Webpay Plus en CLP solo acepta montos enteros")
        orden = buy_order_de(saga_id)
        r = self._llamar("iniciar", self._tx.create, orden, saga_id[:SESSION_ID_MAX],
                         monto_centavos // 100, self.return_url)
        return Inicio(estado="PENDIENTE", referencia=orden, token=r["token"], url=r["url"], buy_order=orden)

    def confirmar(self, token: str) -> Confirmacion:
        r = self._llamar("confirmar", self._tx.commit, token)
        estado, codigo = r.get("status"), r.get("response_code")
        return Confirmacion(aprobado=(estado == "AUTHORIZED" and codigo == 0), estado_proveedor=estado,
                            response_code=codigo, authorization_code=r.get("authorization_code"))

    def consultar(self, token: str) -> str | None:
        return self._llamar("consultar", self._tx.status, token).get("status")

    def reembolsar(self, token: str | None, monto_centavos: int) -> Reembolso:
        if not token:
            raise ErrorPasarela("reembolsar", "cobro Webpay sin token")
        r = self._llamar("reembolsar", self._tx.refund, token, monto_centavos // 100)
        tipo, codigo = r.get("type"), r.get("response_code")
        if tipo not in ("REVERSED", "NULLIFIED") or codigo not in (None, 0):
            raise ErrorPasarela("reembolsar", f"reembolso no aceptado (type={tipo}, response_code={codigo})", 502)
        return Reembolso(tipo=tipo, response_code=codigo)


def crear_pasarela(nombre: str | None = None, transaccion=None) -> Pasarela:
    nombre = (nombre or os.getenv("PASARELA", "simulada")).strip().lower()
    if nombre == "simulada":
        return PasarelaSimulada()
    if nombre == "webpay":
        entorno = os.getenv("TBK_ENV", "integracion").strip().lower()
        if entorno != "integracion":
            raise ValueError("TBK_ENV solo admite 'integracion': este proyecto no opera en producción")
        return PasarelaWebpay(
            return_url=os.getenv("PAGOS_RETURN_URL", ""),
            transaccion=transaccion,
            commerce_code=os.getenv("TBK_COMMERCE_CODE") or None,
            api_key=os.getenv("TBK_API_KEY") or None,
            timeout_s=float(os.getenv("TBK_TIMEOUT_S", "8")),
        )
    raise ValueError(f"PASARELA desconocida: {nombre!r} (use 'simulada' o 'webpay')")
