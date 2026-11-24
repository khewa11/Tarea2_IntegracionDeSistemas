"""Dobles de prueba de los servicios de Persona A, fieles a CONTRATO.md §3.

Sirven para probar el orquestador sin el código real de A. Implementan lo que
importa a la Saga: idempotencia por sagaId, compensaciones idempotentes con
"lápida" (una compensación adelantada bloquea el comando tardío) y los modos de
falla de Acceso (`ok`, `lento`, `caido`, `error`). Para las pruebas también se
puede forzar un modo de falla en Espacios y Pagos (eso no existe en A real).
"""
from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def _err(codigo: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=codigo)


class Falla:
    def __init__(self):
        self.modo = "ok"
        self.delay_ms = 0

    async def aplicar(self) -> JSONResponse | None:
        if self.modo == "lento":
            await asyncio.sleep(self.delay_ms / 1000)
        elif self.modo == "caido":
            return _err(503, "CONTROLADOR_NO_DISPONIBLE")
        elif self.modo == "error":
            return _err(502, "CONTROLADOR_ERROR")
        return None


class Registro:
    """Lleva la cuenta de las llamadas recibidas, para verificar orden y efectos."""

    def __init__(self):
        self.llamadas: list[tuple[str, str]] = []


def fake_espacios(reg: Registro) -> FastAPI:
    app = FastAPI()
    app.state.falla = Falla()
    reservas: dict[str, dict] = {}

    @app.post("/reservas")
    async def reservar(req: Request):
        datos = await req.json()
        saga, espacio = datos["sagaId"], datos["espacioId"]
        reg.llamadas.append(("espacios", f"reservar {saga}"))
        if (r := await app.state.falla.aplicar()) is not None:
            return r
        if saga in reservas:
            if reservas[saga]["estado"] == "LIBERADA" and reservas[saga].get("lapida"):
                return _err(409, "SAGA_COMPENSADA")
            return JSONResponse({"sagaId": saga, "espacioId": espacio, "estado": "RESERVADA"}, 201)
        if espacio not in {f"E-{i:03d}" for i in range(1, 11)}:
            return _err(404, "ESPACIO_NO_ENCONTRADO")
        if any(r["espacioId"] == espacio and r["estado"] == "RESERVADA" for r in reservas.values()):
            return _err(409, "ESPACIO_OCUPADO")
        reservas[saga] = {"espacioId": espacio, "estado": "RESERVADA"}
        return JSONResponse({"sagaId": saga, "espacioId": espacio, "estado": "RESERVADA"}, 201)

    @app.post("/reservas/{saga}/liberar")
    async def liberar(saga: str):
        reg.llamadas.append(("espacios", f"liberar {saga}"))
        if (r := await app.state.falla.aplicar()) is not None:
            return r
        if saga not in reservas:
            reservas[saga] = {"espacioId": None, "estado": "LIBERADA", "lapida": True}
            return {"sagaId": saga, "estado": "SIN_RESERVA"}
        reservas[saga]["estado"] = "LIBERADA"
        return {"sagaId": saga, "estado": "LIBERADA"}

    @app.get("/reservas/{saga}")
    async def ver(saga: str):
        if saga not in reservas:
            return _err(404, "RESERVA_NO_ENCONTRADA")
        return {"sagaId": saga, **reservas[saga]}

    return app


def fake_pagos(reg: Registro) -> FastAPI:
    app = FastAPI()
    app.state.falla = Falla()
    cobros: dict[str, dict] = {}
    app.state.cobros = cobros
    # Modo Webpay (pasarela asíncrona): POST /cobros responde 202 PENDIENTE y el cobro se
    # confirma con POST /cobros/{saga}/confirmar. resultado_pago: AUTHORIZED | FAILED.
    app.state.webpay = False
    app.state.resultado_pago = "AUTHORIZED"
    app.state.commits = 0

    @app.post("/cobros")
    async def cobrar(req: Request):
        datos = await req.json()
        saga = datos["sagaId"]
        reg.llamadas.append(("pagos", f"cobrar {saga}"))
        if (r := await app.state.falla.aplicar()) is not None:
            return r
        if saga in cobros:
            if cobros[saga].get("lapida"):
                return _err(409, "SAGA_COMPENSADA")
        elif app.state.webpay:
            cobros[saga] = {"monto": str(datos["monto"]), "estado": "PENDIENTE", "token": f"tok-{saga}",
                            "buyOrder": saga[:26]}
        else:
            cobros[saga] = {"monto": str(datos["monto"]), "estado": "COBRADO", "efectos": 1}
        if cobros[saga]["estado"] == "PENDIENTE" or "token" in cobros[saga]:
            c = cobros[saga]
            return JSONResponse({"sagaId": saga, "monto": c["monto"], "moneda": "CLP", "estado": "PENDIENTE",
                                 "referenciaPasarela": c["buyOrder"], "token": c["token"],
                                 "url": "https://webpay.test/init", "buyOrder": c["buyOrder"]}, 202)
        return JSONResponse({"sagaId": saga, "monto": str(datos["monto"]), "moneda": "CLP",
                             "estado": "COBRADO", "referenciaPasarela": f"SIM-{saga}"}, 201)

    @app.post("/cobros/{saga}/confirmar")
    async def confirmar(saga: str, req: Request):
        datos = await req.json()
        reg.llamadas.append(("pagos", f"confirmar {saga}"))
        if (r := await app.state.falla.aplicar()) is not None:
            return r
        c = cobros.get(saga)
        if c is None:
            return _err(404, "COBRO_NO_ENCONTRADO")
        if c.get("lapida"):
            return _err(409, "SAGA_COMPENSADA")
        if c.get("token") != datos["token"]:
            return _err(409, "TOKEN_NO_CORRESPONDE")
        if c["estado"] == "PENDIENTE":
            app.state.commits += 1
            c["estado"] = "COBRADO" if app.state.resultado_pago == "AUTHORIZED" else "RECHAZADO"
        return {"sagaId": saga, "estado": c["estado"], "responseCode": 0 if c["estado"] == "COBRADO" else -1}

    @app.post("/cobros/{saga}/reembolso")
    async def reembolsar(saga: str):
        reg.llamadas.append(("pagos", f"reembolsar {saga}"))
        if (r := await app.state.falla.aplicar()) is not None:
            return r
        if saga not in cobros:
            cobros[saga] = {"estado": "REEMBOLSADO", "lapida": True}
            return {"sagaId": saga, "estado": "SIN_COBRO"}
        c = cobros[saga]
        c["lapida"] = True
        if c["estado"] == "PENDIENTE":
            c["estado"] = "ANULADO"
        elif c["estado"] == "COBRADO":
            c["estado"] = "REEMBOLSADO"
        return {"sagaId": saga, "estado": c["estado"]}

    @app.get("/cobros/{saga}")
    async def ver(saga: str):
        if saga not in cobros:
            return _err(404, "COBRO_NO_ENCONTRADO")
        return {"sagaId": saga, **cobros[saga]}

    return app


def fake_acceso(reg: Registro) -> FastAPI:
    app = FastAPI()
    app.state.falla = Falla()
    habs: dict[str, dict] = {}

    @app.post("/habilitaciones")
    async def habilitar(req: Request):
        datos = await req.json()
        saga = datos["sagaId"]
        reg.llamadas.append(("acceso", f"habilitar {saga}"))
        if (r := await app.state.falla.aplicar()) is not None:
            return r
        # Se vuelve a mirar el registro DESPUÉS de la espera: evita habilitaciones tardías.
        if habs.get(saga, {}).get("estado") == "REVOCADA":
            return _err(409, "SAGA_COMPENSADA")
        habs[saga] = {"patente": datos["patente"], "espacioId": datos["espacioId"], "estado": "HABILITADA"}
        return JSONResponse({"sagaId": saga, **habs[saga]}, 201)

    @app.post("/habilitaciones/{saga}/revocar")
    async def revocar(saga: str):
        reg.llamadas.append(("acceso", f"revocar {saga}"))
        habs.setdefault(saga, {})["estado"] = "REVOCADA"
        return {"sagaId": saga, "estado": "REVOCADA"}

    @app.get("/habilitaciones/{saga}")
    async def ver(saga: str):
        if saga not in habs:
            return _err(404, "HABILITACION_NO_ENCONTRADA")
        return {"sagaId": saga, **habs[saga]}

    @app.post("/admin/falla")
    async def admin_falla(req: Request):
        datos = await req.json()
        app.state.falla.modo = datos["modo"]
        app.state.falla.delay_ms = datos.get("delayMs", 0)
        return {"modo": app.state.falla.modo, "delayMs": app.state.falla.delay_ms}

    return app


class Enrutador(httpx.AsyncBaseTransport):
    """Transporte httpx que envía cada request al fake según el host (espacios/pagos/acceso)."""

    def __init__(self, apps: dict[str, FastAPI]):
        self.transportes = {host: httpx.ASGITransport(app=a) for host, a in apps.items()}
        self.caidos: set[str] = set()  # hosts que simulan "connection refused"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self.caidos:
            raise httpx.ConnectError(f"conexión rechazada por {host}", request=request)
        return await self.transportes[host].handle_async_request(request)
