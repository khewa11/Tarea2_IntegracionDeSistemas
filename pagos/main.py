"""Microservicio Pagos: cobro/reembolso exactly-once lógico sobre una pasarela adaptable.

PASARELA=simulada (defecto): cobro inmediato COBRADO, sin red (comportamiento histórico).
PASARELA=webpay: Webpay Plus en integración, flujo asíncrono
    POST /cobros                     -> 202 PENDIENTE con token y URL de pago
    POST /cobros/{sagaId}/confirmar  -> commit del token_ws: COBRADO o RECHAZADO
    POST /cobros/{sagaId}/reembolso  -> refund total (COBRADO) o anulación (PENDIENTE)

Las llamadas a la pasarela se hacen dentro de la transacción de escritura (BEGIN
IMMEDIATE): confirmar y reembolsar del mismo cobro quedan serializados. Un fallo de
la pasarela NO se guarda en `comandos`, para que el orquestador pueda reintentar.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.models import CobroIn, ConfirmacionPagoIn
from common.util import database, db_location, log_event, now, out, prior_command, record_command, setup_journal
from pagos.pasarelas import ErrorPasarela, Pasarela, crear_pasarela

SERVICE = "pagos"
ESTADOS = ("PENDIENTE", "COBRADO", "RECHAZADO", "ANULADO", "REEMBOLSADO")
COLUMNAS_V1 = "saga_id, monto_centavos, moneda, estado, referencia_pasarela, creado_en, actualizado_en"

ESQUEMA_COBROS = f"""CREATE TABLE cobros (
    saga_id TEXT PRIMARY KEY,
    monto_centavos INTEGER NOT NULL CHECK(monto_centavos>0),
    moneda TEXT NOT NULL DEFAULT 'CLP',
    estado TEXT NOT NULL CHECK(estado IN ({",".join(repr(e) for e in ESTADOS)})),
    referencia_pasarela TEXT NOT NULL UNIQUE,
    creado_en TEXT NOT NULL, actualizado_en TEXT NOT NULL,
    pasarela TEXT NOT NULL DEFAULT 'SIMULADA',
    tbk_token TEXT,
    buy_order TEXT,
    authorization_code TEXT,
    response_code INTEGER
)"""


def init_db(path: str) -> None:
    with database(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    with database(path, write=True) as db:
        fila = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='cobros'").fetchone()
        if fila is None:
            db.execute(ESQUEMA_COBROS)
        elif "PENDIENTE" not in fila["sql"]:
            # Migración v1 -> v2: SQLite no permite modificar un CHECK, se reconstruye la
            # tabla dentro de la misma transacción conservando todas las filas.
            db.execute("ALTER TABLE cobros RENAME TO cobros_v1")
            db.execute(ESQUEMA_COBROS)
            db.execute(f"INSERT INTO cobros ({COLUMNAS_V1}) SELECT {COLUMNAS_V1} FROM cobros_v1")
            db.execute("DROP TABLE cobros_v1")
            log_event(None, SERVICE, "migracion_cobros", "OK", version=2)
        setup_journal(db)


def _monto(centavos: int) -> str:
    return str(Decimal(centavos) / 100)


def _error_pasarela(exc: ErrorPasarela) -> tuple[int, dict]:
    codigo = 504 if exc.codigo == 504 else 502
    return codigo, {"error": "PASARELA_NO_DISPONIBLE", "operacion": exc.operacion, "detalle": exc.detalle}


def _tiene_lapida(db, saga_id: str) -> bool:
    return db.execute("SELECT 1 FROM comandos WHERE saga_id=? AND accion='reembolsar'", (saga_id,)).fetchone() is not None


def create_app(db_path: str | None = None, pasarela: Pasarela | None = None) -> FastAPI:
    path = db_location(db_path, "/data/pagos.db")
    pasarela = pasarela or crear_pasarela()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(path)
        log_event(None, SERVICE, "arranque", "OK", pasarela=pasarela.nombre)
        yield

    app = FastAPI(title="Microservicio Pagos", version="2.0.0", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"servicio": SERVICE, "estado": "ok", "pasarela": pasarela.nombre}

    @app.post("/cobros", status_code=201)
    def cobrar(request: CobroIn):
        centavos = int(request.monto * 100)
        amount = _monto(centavos)
        data = {"montoCentavos": centavos}
        duplicated = False
        cachear = True
        with database(path, write=True) as db:
            existing = prior_command(db, request.sagaId, "cobrar", data)
            if existing is not None:
                result = existing
                duplicated = True
            elif _tiene_lapida(db, request.sagaId):
                result = (409, {"error": "SAGA_COMPENSADA", "detalle": "No es posible cobrar tras un reembolso"})
            elif pasarela.asincrona and centavos % 100:
                result, cachear = (422, {"error": "MONTO_CLP_ENTERO", "detalle": "Webpay en CLP no admite decimales"}), False
            else:
                try:
                    inicio = pasarela.iniciar(request.sagaId, centavos)
                except ErrorPasarela as exc:
                    result, cachear = _error_pasarela(exc), False
                else:
                    instant = now()
                    db.execute(
                        "INSERT INTO cobros (saga_id, monto_centavos, moneda, estado, referencia_pasarela, creado_en,"
                        " actualizado_en, pasarela, tbk_token, buy_order) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (request.sagaId, centavos, "CLP", inicio.estado, inicio.referencia, instant, instant,
                         pasarela.nombre, inicio.token, inicio.buy_order))
                    cuerpo = {"sagaId": request.sagaId, "monto": amount, "moneda": "CLP",
                              "estado": inicio.estado, "referenciaPasarela": inicio.referencia}
                    if inicio.estado == "PENDIENTE":
                        cuerpo.update({"token": inicio.token, "url": inicio.url, "buyOrder": inicio.buy_order})
                        result = (202, cuerpo)
                    else:
                        result = (201, cuerpo)
            if not duplicated and cachear:
                record_command(db, request.sagaId, "cobrar", data, *result)
        resultado = {201: "OK", 202: "PENDIENTE"}.get(result[0], "FALLO")
        log_event(request.sagaId, SERVICE, "cobrar", resultado, codigo=result[0], idempotente=duplicated,
                  **({"pasarela": pasarela.nombre} if pasarela.asincrona else {}))
        return out(result)

    @app.post("/cobros/{saga_id}/confirmar")
    def confirmar(saga_id: str, request: ConfirmacionPagoIn):
        data = {"token": request.token}
        duplicated = False
        cachear = True
        with database(path, write=True) as db:
            existing = prior_command(db, saga_id, "confirmar", data)
            row = db.execute("SELECT * FROM cobros WHERE saga_id=?", (saga_id,)).fetchone()
            if existing is not None:
                result = existing
                duplicated = True
            elif _tiene_lapida(db, saga_id):
                result = (409, {"error": "SAGA_COMPENSADA", "detalle": "El cobro ya fue anulado o reembolsado"})
            elif row is None:
                result, cachear = (404, {"error": "COBRO_NO_ENCONTRADO"}), False
            elif row["tbk_token"] != request.token:
                result, cachear = (409, {"error": "TOKEN_NO_CORRESPONDE"}), False
            elif row["estado"] != "PENDIENTE":
                result, cachear = (409, {"error": "COBRO_NO_PENDIENTE", "estado": row["estado"]}), False
            else:
                try:
                    conf = pasarela.confirmar(request.token)
                except ErrorPasarela as exc:
                    result, cachear = _error_pasarela(exc), False
                else:
                    estado = "COBRADO" if conf.aprobado else "RECHAZADO"
                    db.execute("UPDATE cobros SET estado=?, authorization_code=?, response_code=?, actualizado_en=?"
                               " WHERE saga_id=?",
                               (estado, conf.authorization_code, conf.response_code, now(), saga_id))
                    result = (200, {"sagaId": saga_id, "monto": _monto(row["monto_centavos"]), "moneda": "CLP",
                                    "estado": estado, "referenciaPasarela": row["referencia_pasarela"],
                                    "estadoPasarela": conf.estado_proveedor, "responseCode": conf.response_code,
                                    "authorizationCode": conf.authorization_code})
            if not duplicated and cachear:
                record_command(db, saga_id, "confirmar", data, *result)
        estado_log = result[1].get("estado") if result[0] == 200 else "FALLO"
        log_event(saga_id, SERVICE, "confirmar", estado_log, codigo=result[0], idempotente=duplicated,
                  responseCode=result[1].get("responseCode"))
        return out(result)

    @app.post("/cobros/{saga_id}/reembolso")
    def reembolsar(saga_id: str):
        duplicated = False
        cachear = True
        with database(path, write=True) as db:
            existing = prior_command(db, saga_id, "reembolsar", {})
            if existing is not None:
                result = existing
                duplicated = True
            else:
                row = db.execute("SELECT * FROM cobros WHERE saga_id=?", (saga_id,)).fetchone()
                try:
                    status = _compensar_cobro(db, row, saga_id)
                except ErrorPasarela as exc:
                    result, cachear = _error_pasarela(exc), False
                else:
                    result = (200, {"sagaId": saga_id, "estado": status})
            if not duplicated and cachear:
                record_command(db, saga_id, "reembolsar", {}, *result)
        log_event(saga_id, SERVICE, "reembolsar", "OK" if result[0] == 200 else "FALLO", codigo=result[0],
                  idempotente=duplicated, **({"estado": result[1].get("estado")} if pasarela.asincrona else {}))
        return out(result)

    def _compensar_cobro(db, row, saga_id: str) -> str:
        """Deshace el cobro según su estado. Lanza ErrorPasarela si el proveedor no responde."""
        if row is None:
            return "SIN_COBRO"  # el registro en `comandos` queda como lápida
        estado = row["estado"]
        if estado == "PENDIENTE":
            # El usuario pudo haber pagado aunque el commit no se hiciera (retorno perdido,
            # timeout del commit). Se consulta al proveedor antes de anular.
            try:
                estado_proveedor = pasarela.consultar(row["tbk_token"]) if row["tbk_token"] else None
            except ErrorPasarela as exc:
                if exc.codigo is None or not 400 <= exc.codigo < 500:
                    raise
                estado_proveedor = None  # la API respondió: no existe autorización que devolver
            if estado_proveedor == "AUTHORIZED":
                estado = "COBRADO"
            else:
                db.execute("UPDATE cobros SET estado='ANULADO', actualizado_en=? WHERE saga_id=?", (now(), saga_id))
                return "ANULADO"
        if estado == "COBRADO":
            pasarela.reembolsar(row["tbk_token"], row["monto_centavos"])
            db.execute("UPDATE cobros SET estado='REEMBOLSADO', actualizado_en=? WHERE saga_id=?", (now(), saga_id))
            return "REEMBOLSADO"
        return estado  # RECHAZADO, ANULADO o REEMBOLSADO: nada que devolver

    @app.get("/cobros/{saga_id}")
    def consultar_cobro(saga_id: str):
        with database(path) as db:
            row = db.execute("SELECT * FROM cobros WHERE saga_id=?", (saga_id,)).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "COBRO_NO_ENCONTRADO"})
        cuerpo = {"sagaId": row["saga_id"], "monto": _monto(row["monto_centavos"]),
                  "moneda": row["moneda"], "estado": row["estado"],
                  "referenciaPasarela": row["referencia_pasarela"],
                  "creadoEn": row["creado_en"], "actualizadoEn": row["actualizado_en"]}
        if row["pasarela"] != "SIMULADA":
            cuerpo.update({"pasarela": row["pasarela"], "buyOrder": row["buy_order"],
                           "authorizationCode": row["authorization_code"], "responseCode": row["response_code"]})
        return cuerpo

    return app


app = create_app()
