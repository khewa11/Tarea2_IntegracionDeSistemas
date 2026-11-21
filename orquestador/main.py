"""Orquestador Saga (Persona B) — http://orquestador:4000

Arranque: uvicorn --factory orquestador.main:create_app --host 0.0.0.0 --port 4000
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import saga as s
from .clientes import Dependencia
from .config import Config
from .db import BaseSagas
from .logs import log
from .resiliencia import Bulkhead, CircuitBreaker

SAGA_ID_PATRON = r"^[A-Za-z0-9_-]{1,64}$"


class SolicitudReserva(BaseModel):
    espacioId: str = Field(min_length=1, max_length=32)
    patente: str = Field(min_length=1, max_length=16)
    monto: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    # Opcional: si el cliente (o el gateway al reintentar) envía su propio sagaId,
    # repetir la solicitud devuelve la misma saga en vez de crear otra.
    sagaId: str | None = Field(default=None, pattern=SAGA_ID_PATRON)


def codigo_http(saga: dict) -> int:
    estado = saga["estado"]
    if estado == s.COMPLETADA:
        return 201
    if estado == s.FALLIDA:
        motivo = saga.get("motivo") or ""
        # Rechazo de negocio (espacio ocupado / inexistente) vs. dependencia no disponible.
        return 503 if motivo.startswith(("CIRCUITO_ABIERTO", "BULKHEAD_LLENO")) else 409
    if estado in (s.COMPENSADA, s.COMPENSACION_PENDIENTE):
        return 503
    return 202  # todavía en curso (solo visible al repetir un sagaId en ejecución)


def resumen(saga: dict) -> dict:
    return {"sagaId": saga["sagaId"], "estado": saga["estado"], "motivo": saga["motivo"]}


def create_app(config: Config | None = None, transport: httpx.AsyncBaseTransport | None = None,
               reloj=time.monotonic) -> FastAPI:
    config = config or Config.desde_entorno()
    estado: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        limites = httpx.Limits(max_connections=100, max_keepalive_connections=20)
        async with httpx.AsyncClient(transport=transport, limits=limites) as http:
            def al_cambiar(servicio: str, anterior: str, nuevo: str) -> None:
                log("circuit_breaker", nuevo, None, destino=servicio, anterior=anterior)

            def dep(nombre: str, url: str, bulkhead: Bulkhead | None = None) -> Dependencia:
                cb = CircuitBreaker(nombre, config.cb_umbral_fallos, config.cb_espera_s, reloj, al_cambiar)
                return Dependencia(nombre, url, http, config.timeout_s, cb, bulkhead)

            orq = s.Orquestador(
                BaseSagas(config.db_path),
                dep("espacios", config.espacios_url),
                dep("pagos", config.pagos_url),
                dep("acceso", config.acceso_url, Bulkhead("acceso", config.bulkhead_max)),
                config.compensacion_reintentos,
                config.compensacion_pausa_s,
            )
            estado["orq"] = orq
            estado["inicio"] = time.time()
            log("arranque", "ok", None, config={k: v for k, v in vars(config).items()})

            async def bucle_recuperacion():
                while True:
                    try:
                        await orq.recuperar()
                    except Exception as exc:  # noqa: BLE001 — el bucle no debe morir
                        log("saga.recuperacion", "error", None, error=repr(exc))
                    if config.recuperacion_intervalo_s <= 0:
                        return
                    await asyncio.sleep(config.recuperacion_intervalo_s)

            tarea = asyncio.create_task(bucle_recuperacion())
            try:
                yield
            finally:
                tarea.cancel()

    app = FastAPI(title="Orquestador Saga — Estacionamiento", version="1.0", lifespan=lifespan)

    @app.get("/health")
    async def health():
        orq: s.Orquestador = estado["orq"]
        return {
            "servicio": "orquestador",
            "estado": "ok",
            "implementado": True,
            "circuitos": {d.nombre: d.breaker.estado for d in orq.dependencias},
        }

    @app.post("/reservas-estacionamiento")
    async def crear_reserva(solicitud: SolicitudReserva):
        orq: s.Orquestador = estado["orq"]
        saga_id = solicitud.sagaId or f"S-{uuid.uuid4().hex}"
        resultado = await orq.ejecutar(saga_id, solicitud.espacioId, solicitud.patente.upper(), solicitud.monto)
        return JSONResponse(resumen(resultado), status_code=codigo_http(resultado))

    @app.get("/reservas-estacionamiento/{saga_id}")
    async def obtener_reserva(saga_id: str):
        orq: s.Orquestador = estado["orq"]
        saga = orq.db.obtener(saga_id)
        if saga is None:
            return JSONResponse({"error": "SAGA_NO_ENCONTRADA", "sagaId": saga_id}, status_code=404)
        return saga

    @app.get("/metrics")
    async def metrics():
        orq: s.Orquestador = estado["orq"]
        return {
            "servicio": "orquestador",
            "uptimeSegundos": round(time.time() - estado["inicio"], 1),
            "sagas": {"porEstado": orq.db.conteo_por_estado(), "enCurso": len(orq._activas)},
            "dependencias": {d.nombre: d.snapshot() for d in orq.dependencias},
        }

    return app
