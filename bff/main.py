import os
import json
import time
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="BFF Web - Estacionamiento Forma C", version="1.0.0")

SERVICE_NAME = os.getenv("SERVICE", "bff")
ORQUESTADOR_URL = os.getenv("ORQUESTADOR_URL", "http://orquestador:4000")
ESPACIOS_URL = os.getenv("ESPACIOS_URL", "http://espacios:5001")
PAGOS_URL = os.getenv("PAGOS_URL", "http://pagos:5002")
ACCESO_URL = os.getenv("ACCESO_URL", "http://acceso:5003")


def log_json(
    paso: str,
    resultado: str,
    saga_id: Optional[str] = None,
    codigo: Optional[int] = None,
    duracion_ms: Optional[float] = None,
    modo: Optional[str] = None,
) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sagaId": saga_id,
        "servicio": SERVICE_NAME,
        "paso": paso,
        "resultado": resultado,
    }
    if codigo is not None:
        entry["codigo"] = codigo
    if duracion_ms is not None:
        entry["duracionMs"] = round(duracion_ms, 2)
    if modo is not None:
        entry["modo"] = modo
    print(json.dumps(entry, ensure_ascii=False), flush=True)


# Demo web estática (HTML + JS sin build). El Gateway la publica sin JWT en /bff/demo.
DEMO_DIR = Path(__file__).resolve().parent / "demo"
DEMO_ARCHIVOS = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}


def _archivo_demo(nombre: str):
    if nombre not in DEMO_ARCHIVOS:  # lista blanca: no hay acceso a otras rutas del disco
        return JSONResponse(status_code=404, content={"error": "ARCHIVO_NO_ENCONTRADO"})
    return FileResponse(DEMO_DIR / nombre, media_type=DEMO_ARCHIVOS[nombre],
                        headers={"Cache-Control": "no-store"})


@app.get("/bff/demo")
async def demo_index():
    return _archivo_demo("index.html")


@app.get("/bff/demo/{archivo}")
async def demo_archivo(archivo: str):
    return _archivo_demo(archivo)


@app.get("/health")
async def health():
    return {"servicio": SERVICE_NAME, "estado": "ok", "implementado": True}


async def consultar_servicio(
    client: httpx.AsyncClient,
    url: str,
    timeout_s: float = 2.0,
) -> tuple[int, Any]:
    try:
        resp = await client.get(url, timeout=timeout_s)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return resp.status_code, data
    except httpx.TimeoutException:
        return 504, {"error": "TIMEOUT"}
    except httpx.RequestError:
        return 503, {"error": "CONNECTION_ERROR"}


@app.get("/bff/mi-reserva/{saga_id}")
async def obtener_mi_reserva(saga_id: str):
    """
    Compone en una sola respuesta para el cliente web los datos de:
    Orquestador, Espacios, Pagos y Acceso.
    Si Acceso está lento o caído, responde igual marcándolo como 'no_disponible' (degradación elegante).
    """
    inicio = time.perf_counter()

    async with httpx.AsyncClient() as client:
        res_orq, res_esp, res_pag, res_acc = await asyncio.gather(
            consultar_servicio(client, f"{ORQUESTADOR_URL}/reservas-estacionamiento/{saga_id}", timeout_s=2.0),
            consultar_servicio(client, f"{ESPACIOS_URL}/reservas/{saga_id}", timeout_s=2.0),
            consultar_servicio(client, f"{PAGOS_URL}/cobros/{saga_id}", timeout_s=2.0),
            consultar_servicio(client, f"{ACCESO_URL}/habilitaciones/{saga_id}", timeout_s=1.2),
        )

    status_orq, data_orq = res_orq
    status_esp, data_esp = res_esp
    status_pag, data_pag = res_pag
    status_acc, data_acc = res_acc

    # Si no existe en ningún servicio de dominio ni en el orquestador
    if status_orq == 404 and status_esp == 404 and status_pag == 404:
        duracion = (time.perf_counter() - inicio) * 1000
        log_json(paso="BFF_MI_RESERVA", resultado="NO_ENCONTRADA", saga_id=saga_id, codigo=404, duracion_ms=duracion)
        return JSONResponse(status_code=404, content={"error": "RESERVA_NO_ENCONTRADA", "sagaId": saga_id})

    # Degradación elegante sobre Acceso (dependencia frágil)
    degradado = False
    if status_acc == 200:
        bloque_acceso = data_acc
    elif status_acc == 404:
        bloque_acceso = {"estado": "SIN_HABILITACION"}
    else:
        degradado = True
        bloque_acceso = {
            "estado": "no_disponible",
            "motivo": f"Controlador de barrera no disponible (HTTP {status_acc})",
        }

    bloque_espacio = data_esp if status_esp == 200 else {"estado": "no_disponible"}
    bloque_pago = data_pag if status_pag == 200 else {"estado": "no_disponible"}

    respuesta = {
        "sagaId": saga_id,
        "estadoGeneral": data_orq.get("estado") if status_orq == 200 else "DESCONOCIDO",
        "motivo": data_orq.get("motivo") if status_orq == 200 else None,
        "espacio": bloque_espacio,
        "pago": bloque_pago,
        "acceso": bloque_acceso,
        "degradacionActiva": degradado,
    }
    # Pasarela asíncrona (Webpay): mientras el usuario no paga, se expone la URL de pago
    # para retomar el pago desde la web. En modo simulado este bloque no aparece.
    pago_orq = data_orq.get("pago") if status_orq == 200 else None
    if respuesta["estadoGeneral"] == "PAGO_PENDIENTE" and pago_orq:
        respuesta["pagoPendiente"] = {"url": pago_orq.get("url"), "token": pago_orq.get("token")}

    duracion = (time.perf_counter() - inicio) * 1000
    log_json(
        paso="BFF_MI_RESERVA",
        resultado="OK_DEGRADADO" if degradado else "OK",
        saga_id=saga_id,
        codigo=200,
        duracion_ms=duracion,
        modo="degradado" if degradado else "normal",
    )
    return respuesta


@app.get("/bff/dashboard")
async def obtener_dashboard():
    """Agrega catálogo de espacios y métricas del orquestador en una única llamada web."""
    inicio = time.perf_counter()

    async with httpx.AsyncClient() as client:
        res_esp, res_met = await asyncio.gather(
            consultar_servicio(client, f"{ESPACIOS_URL}/espacios", timeout_s=2.0),
            consultar_servicio(client, f"{ORQUESTADOR_URL}/metrics", timeout_s=2.0),
        )

    status_esp, data_esp = res_esp
    status_met, data_met = res_met

    duracion = (time.perf_counter() - inicio) * 1000
    log_json(paso="BFF_DASHBOARD", resultado="OK", codigo=200, duracion_ms=duracion)

    return {
        "espacios": data_esp.get("espacios", []) if status_esp == 200 else "no_disponible",
        "metricasSistema": data_met if status_met == 200 else "no_disponible",
    }