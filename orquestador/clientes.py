"""Clientes HTTP hacia los servicios de dominio (Persona A).

Cada llamada tiene timeout. Las llamadas de avance de la Saga pasan además por el
Circuit Breaker del servicio y, en Acceso, por el Bulkhead. Las compensaciones NO
pasan por el Circuit Breaker ni el Bulkhead: deben intentarse siempre, aunque el
circuito esté abierto, porque dejar un cobro sin reembolsar es peor que una
llamada extra a un servicio con problemas.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from typing import Any

import httpx

from .logs import log
from .resiliencia import Bulkhead, BulkheadLleno, CircuitBreaker, CircuitoAbierto


class FalloDependencia(Exception):
    """Fallo técnico: timeout, conexión rechazada o respuesta 5xx."""

    def __init__(self, servicio: str, tipo: str, codigo: int | None = None, detalle: str = ""):
        super().__init__(f"{servicio}: {tipo} {codigo or ''} {detalle}".strip())
        self.servicio = servicio
        self.tipo = tipo
        self.codigo = codigo


class RechazoNegocio(Exception):
    """El servicio respondió 4xx: regla de negocio o validación (no es fallo técnico)."""

    def __init__(self, servicio: str, codigo: int, error: str | None, cuerpo: Any):
        super().__init__(f"{servicio}: {codigo} {error}")
        self.servicio = servicio
        self.codigo = codigo
        self.error = error
        self.cuerpo = cuerpo


def extraer_error(cuerpo: Any) -> str | None:
    """Obtiene el código de error de A, sea `{"error": ..}` o `{"detail": {"error": ..}}`."""
    if isinstance(cuerpo, dict):
        if isinstance(cuerpo.get("error"), str):
            return cuerpo["error"]
        detalle = cuerpo.get("detail")
        if isinstance(detalle, dict) and isinstance(detalle.get("error"), str):
            return detalle["error"]
        if isinstance(detalle, str):
            return detalle
    return None


class MetricasServicio:
    def __init__(self, ventana: int = 500):
        self.resultados: Counter[str] = Counter()
        self.latencias_ms: deque[float] = deque(maxlen=ventana)

    def registrar(self, resultado: str, duracion_ms: float | None) -> None:
        self.resultados[resultado] += 1
        if duracion_ms is not None:
            self.latencias_ms.append(duracion_ms)

    def snapshot(self) -> dict:
        lat = sorted(self.latencias_ms)

        def percentil(p: float) -> float | None:
            if not lat:
                return None
            return round(lat[min(len(lat) - 1, int(p * len(lat)))], 1)

        return {
            "llamadas": sum(self.resultados.values()),
            "resultados": dict(self.resultados),
            "latenciaMs": {
                "muestras": len(lat),
                "promedio": round(sum(lat) / len(lat), 1) if lat else None,
                "p50": percentil(0.50),
                "p95": percentil(0.95),
                "max": round(lat[-1], 1) if lat else None,
            },
        }


class Dependencia:
    def __init__(self, nombre: str, base_url: str, http: httpx.AsyncClient, timeout_s: float,
                 breaker: CircuitBreaker, bulkhead: Bulkhead | None = None):
        self.nombre = nombre
        self.base_url = base_url.rstrip("/")
        self.http = http
        self.timeout_s = timeout_s
        self.breaker = breaker
        self.bulkhead = bulkhead
        self.metricas = MetricasServicio()

    async def llamar(self, metodo: str, ruta: str, *, saga_id: str, paso: str,
                     json: Any = None, proteger: bool = True) -> dict:
        """Ejecuta la llamada y devuelve el cuerpo JSON de una respuesta 2xx.

        Lanza CircuitoAbierto, BulkheadLleno, FalloDependencia o RechazoNegocio.
        """
        if proteger:
            try:
                self.breaker.antes_de_llamar()
            except CircuitoAbierto:
                self.metricas.registrar("circuito_abierto", None)
                log(paso, "circuito_abierto", saga_id, destino=self.nombre)
                raise
        try:
            if proteger and self.bulkhead is not None:
                try:
                    async with self.bulkhead.cupo():
                        return await self._ejecutar(metodo, ruta, saga_id, paso, json, proteger)
                except BulkheadLleno:
                    self.breaker.liberar_prueba()
                    self.metricas.registrar("bulkhead_lleno", None)
                    log(paso, "bulkhead_lleno", saga_id, destino=self.nombre)
                    raise
            return await self._ejecutar(metodo, ruta, saga_id, paso, json, proteger)
        except BaseException:
            # Nunca dejar "tomada" la llamada de prueba de SEMI_ABIERTO (cancelación, error inesperado).
            if proteger:
                self.breaker.liberar_prueba()
            raise

    async def _ejecutar(self, metodo: str, ruta: str, saga_id: str, paso: str,
                        json: Any, proteger: bool) -> dict:
        inicio = time.perf_counter()
        url = f"{self.base_url}{ruta}"
        try:
            # wait_for garantiza el timeout total aunque el transporte no lo aplique.
            respuesta = await asyncio.wait_for(
                self.http.request(metodo, url, json=json, timeout=self.timeout_s), self.timeout_s)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return self._fallo(proteger, saga_id, paso, inicio, "timeout")
        except httpx.HTTPError as exc:
            return self._fallo(proteger, saga_id, paso, inicio, "conexion", detalle=type(exc).__name__)

        duracion = (time.perf_counter() - inicio) * 1000
        try:
            cuerpo = respuesta.json()
        except ValueError:
            cuerpo = {"texto": respuesta.text[:200]}

        if respuesta.status_code >= 500:
            return self._fallo(proteger, saga_id, paso, inicio, "http_5xx", codigo=respuesta.status_code)
        if respuesta.status_code >= 400:
            # Un 4xx demuestra que el servicio responde: no cuenta como fallo del circuito.
            if proteger:
                self.breaker.registrar_exito()
            error = extraer_error(cuerpo)
            self.metricas.registrar("rechazo_4xx", duracion)
            log(paso, "rechazado", saga_id, destino=self.nombre, codigo=respuesta.status_code,
                error=error, duracionMs=round(duracion, 1))
            raise RechazoNegocio(self.nombre, respuesta.status_code, error, cuerpo)

        if proteger:
            self.breaker.registrar_exito()
        self.metricas.registrar("ok", duracion)
        log(paso, "ok", saga_id, destino=self.nombre, codigo=respuesta.status_code,
            duracionMs=round(duracion, 1))
        return cuerpo if isinstance(cuerpo, dict) else {"respuesta": cuerpo}

    def _fallo(self, proteger: bool, saga_id: str, paso: str, inicio: float, tipo: str,
               codigo: int | None = None, detalle: str = ""):
        duracion = (time.perf_counter() - inicio) * 1000
        if proteger:
            self.breaker.registrar_fallo()
        self.metricas.registrar(tipo, duracion)
        log(paso, tipo, saga_id, destino=self.nombre, codigo=codigo, duracionMs=round(duracion, 1),
            circuito=self.breaker.estado if proteger else None)
        raise FalloDependencia(self.nombre, tipo, codigo, detalle)

    def snapshot(self) -> dict:
        datos = {"url": self.base_url, "timeoutSegundos": self.timeout_s, **self.metricas.snapshot(),
                 "circuitBreaker": self.breaker.snapshot()}
        if self.bulkhead is not None:
            datos["bulkhead"] = self.bulkhead.snapshot()
        return datos
