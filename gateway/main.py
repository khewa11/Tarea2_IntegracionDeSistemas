import os
import json
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional
from urllib.parse import parse_qs, quote

import httpx
import jwt
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

app = FastAPI(title="API Gateway - Estacionamiento Forma C", version="1.0.0")

SERVICE_NAME = os.getenv("SERVICE", "gateway")
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
BFF_URL = os.getenv("BFF_URL", "http://bff:3000")
ORQUESTADOR_URL = os.getenv("ORQUESTADOR_URL", "http://orquestador:4000")

# Rate limiting por cliente (configurable via env)
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "40"))
RATE_LIMIT_WINDOW_S = int(os.getenv("RATE_LIMIT_WINDOW_S", "60"))
request_history: dict[str, list[float]] = defaultdict(list)

# Excepciones explícitas a la validación JWT (las únicas):
#   1) retorno de Webpay: /pagos/retorno (GET o POST), solo reenvía token_ws/TBK_* al orquestador
#   2) archivos estáticos de la demo: GET /bff/demo y GET /bff/demo/<archivo>
RUTA_RETORNO_PAGO = "/pagos/retorno"
PARAMETROS_RETORNO = ("token_ws", "TBK_TOKEN", "TBK_ORDEN_COMPRA", "TBK_ID_SESION")
DEMO_PREFIJO = "/bff/demo"


def log_json(
    paso: str,
    resultado: str,
    saga_id: Optional[str] = None,
    codigo: Optional[int] = None,
    duracion_ms: Optional[float] = None,
    detalle: Optional[str] = None,
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
    if detalle is not None:
        entry["detalle"] = detalle
    print(json.dumps(entry, ensure_ascii=False), flush=True)


class TokenRequest(BaseModel):
    clienteId: str = "cliente-web"


@app.get("/health")
async def health():
    return {
        "servicio": SERVICE_NAME,
        "estado": "ok",
        "implementado": True,
        "jwtConfigurado": bool(JWT_SECRET),
    }


def es_ruta_demo(ruta: str, metodo: str) -> bool:
    """GET de la demo estática. Se rechaza cualquier intento de salir del prefijo."""
    if metodo != "GET" or ".." in ruta or "\\" in ruta:
        return False
    return ruta == DEMO_PREFIJO or ruta.startswith(DEMO_PREFIJO + "/")


def cliente_ip(request: Request) -> str:
    return f"ip:{request.client.host if request.client else 'desconocida'}"


async def llamar_upstream(method: str, url: str, body: bytes, headers: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=float(os.getenv("UPSTREAM_TIMEOUT_S", "35"))) as client:
        return await client.request(method=method, url=url, content=body, headers=headers)


@app.post("/auth/token")
async def generar_token(body: TokenRequest):
    """Emite un JWT HS256 válido para autenticar clientes contra el Gateway."""
    if not JWT_SECRET:
        raise HTTPException(status_code=500, detail="JWT_SECRET_NO_CONFIGURADO")

    ahora = datetime.now(timezone.utc)
    payload = {
        "sub": body.clienteId,
        "iat": int(ahora.timestamp()),
        "exp": int((ahora + timedelta(hours=2)).timestamp()),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    log_json(paso="EMITIR_TOKEN", resultado="OK", codigo=200, detalle=f"cliente={body.clienteId}")
    return {"access_token": token, "token_type": "Bearer", "expires_in": 7200}


def verificar_rate_limit(cliente_id: str) -> bool:
    ahora = time.time()
    inicio_ventana = ahora - RATE_LIMIT_WINDOW_S
    historial = [t for t in request_history[cliente_id] if t > inicio_ventana]
    if len(historial) >= RATE_LIMIT_MAX:
        request_history[cliente_id] = historial
        return False
    historial.append(ahora)
    request_history[cliente_id] = historial
    return True


def validar_jwt(request: Request) -> str:
    if not JWT_SECRET:
        raise HTTPException(status_code=500, detail="JWT_SECRET_NO_CONFIGURADO")

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="TOKEN_FALTANTE_O_INVALIDO")

    token = auth_header.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return str(payload.get("sub", "anonimo"))
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="TOKEN_EXPIRADO")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="TOKEN_INVALIDO")


@app.api_route(RUTA_RETORNO_PAGO, methods=["GET", "POST"])
async def retorno_pago(request: Request):
    """Retorno de Webpay (sin JWT: el navegador del usuario llega redirigido por Transbank).

    No contiene lógica de negocio: toma token_ws / TBK_TOKEN / TBK_ORDEN_COMPRA /
    TBK_ID_SESION (query o formulario), los reenvía al orquestador y redirige al
    navegador a la página de resultado de la demo.
    """
    inicio = time.perf_counter()
    if not verificar_rate_limit(cliente_ip(request)):
        log_json(paso="RATE_LIMIT", resultado="EXCEDIDO", codigo=429, detalle="retorno_pago")
        return JSONResponse(status_code=429, content={"error": "RATE_LIMIT_EXCEEDED", "limitePorVentana": RATE_LIMIT_MAX})

    valores = parse_qs(request.url.query)
    if request.method == "POST":
        cuerpo = (await request.body()).decode("utf-8", errors="replace")
        if "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
            for clave, lista in parse_qs(cuerpo).items():
                valores.setdefault(clave, lista)
    parametros = {k: valores[k][0][:64] for k in PARAMETROS_RETORNO if valores.get(k) and valores[k][0]}

    saga_id = None
    try:
        resp = await llamar_upstream("POST", f"{ORQUESTADOR_URL}{RUTA_RETORNO_PAGO}",
                                     json.dumps(parametros).encode("utf-8"),
                                     {"Content-Type": "application/json", "X-Authenticated-Client": "webpay-retorno"})
        codigo = resp.status_code
        if codigo == 200:
            saga_id = resp.json().get("sagaId")
    except (httpx.RequestError, ValueError):
        codigo = 502

    tipo = "TBK_TOKEN" if "TBK_TOKEN" in parametros else ("token_ws" if "token_ws" in parametros else "sin_token")
    log_json(paso="RETORNO_PAGO", resultado="OK" if saga_id else "ERROR", saga_id=saga_id, codigo=codigo,
             duracion_ms=(time.perf_counter() - inicio) * 1000, detalle=f"tipo={tipo}")
    if saga_id:
        destino = f"{DEMO_PREFIJO}?sagaId={quote(saga_id)}"
    else:
        error = "PAGO_NO_ENCONTRADO" if codigo in (404, 422) else "SERVICIO_NO_DISPONIBLE"
        destino = f"{DEMO_PREFIJO}?error={error}"
    return RedirectResponse(destino, status_code=303)


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def gateway_proxy(full_path: str, request: Request):
    inicio = time.perf_counter()
    ruta = f"/{full_path}"

    # 1. Bloqueo explícito de rutas administrativas internas
    if "/admin/falla" in ruta:
        duracion = (time.perf_counter() - inicio) * 1000
        log_json(paso="BLOQUEO_ADMIN", resultado="RECHAZADO", codigo=403, duracion_ms=duracion)
        return JSONResponse(status_code=403, content={"error": "FORBIDDEN_ADMIN_ENDPOINT"})

    # 2. Autenticación centralizada JWT HS256 (excepto los estáticos de la demo)
    if es_ruta_demo(ruta, request.method):
        cliente_id = cliente_ip(request)
    else:
        try:
            cliente_id = validar_jwt(request)
        except HTTPException as exc:
            duracion = (time.perf_counter() - inicio) * 1000
            log_json(paso="VALIDAR_JWT", resultado="DENEGADO", codigo=exc.status_code, duracion_ms=duracion)
            return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    # 3. Rate limiting por cliente
    if not verificar_rate_limit(cliente_id):
        duracion = (time.perf_counter() - inicio) * 1000
        log_json(
            paso="RATE_LIMIT",
            resultado="EXCEDIDO",
            codigo=429,
            duracion_ms=duracion,
            detalle=f"cliente={cliente_id}",
        )
        return JSONResponse(
            status_code=429,
            content={"error": "RATE_LIMIT_EXCEEDED", "limitePorVentana": RATE_LIMIT_MAX},
        )

    # 4. Resolución de destino (enrutamiento puro sin lógica de negocio)
    if ruta.startswith("/bff/"):
        target_base = BFF_URL
    elif ruta.startswith("/reservas-estacionamiento") or ruta == "/metrics":
        target_base = ORQUESTADOR_URL
    else:
        duracion = (time.perf_counter() - inicio) * 1000
        log_json(paso="ENRUTAMIENTO", resultado="NO_ENCONTRADO", codigo=404, duracion_ms=duracion)
        return JSONResponse(status_code=404, content={"error": "RUTA_NO_ENCONTRADA"})

    target_url = f"{target_base}{ruta}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    body_bytes = await request.body()
    saga_id = None
    if body_bytes:
        try:
            body_json = json.loads(body_bytes.decode("utf-8"))
            if isinstance(body_json, dict):
                saga_id = body_json.get("sagaId")
        except Exception:
            pass
    if not saga_id and "/" in ruta:
        posible_id = ruta.rstrip("/").split("/")[-1]
        if posible_id.startswith("S-") or posible_id.startswith("s"):
            saga_id = posible_id

    forward_headers = {
        "Content-Type": request.headers.get("Content-Type", "application/json"),
        "X-Authenticated-Client": cliente_id,
    }

    try:
        upstream_resp = await llamar_upstream(request.method, target_url, body_bytes, forward_headers)
        duracion = (time.perf_counter() - inicio) * 1000
        log_json(
            paso=f"PROXY_{request.method}_{ruta}",
            resultado="OK" if upstream_resp.status_code < 400 else "ERROR_UPSTREAM",
            saga_id=saga_id,
            codigo=upstream_resp.status_code,
            duracion_ms=duracion,
        )
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            media_type=upstream_resp.headers.get("content-type", "application/json"),
        )
    except httpx.RequestError as exc:
        duracion = (time.perf_counter() - inicio) * 1000
        log_json(
            paso=f"PROXY_{request.method}_{ruta}",
            resultado="UPSTREAM_NO_DISPONIBLE",
            saga_id=saga_id,
            codigo=502,
            duracion_ms=duracion,
            detalle=str(exc),
        )
        return JSONResponse(
            status_code=502,
            content={"error": "BAD_GATEWAY", "detalle": "Servicio interno no disponible"},
        )
