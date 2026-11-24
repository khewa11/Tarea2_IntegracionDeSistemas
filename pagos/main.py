"""Microservicio Pagos: simulador de pasarela con cobro/reembolso exactly-once lógico."""
from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.models import CobroIn
from common.util import database, db_location, log_event, now, out, prior_command, record_command, setup_journal

SERVICE = "pagos"


def init_db(path: str) -> None:
    with database(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS cobros (
            saga_id TEXT PRIMARY KEY,
            monto_centavos INTEGER NOT NULL CHECK(monto_centavos>0),
            moneda TEXT NOT NULL DEFAULT 'CLP',
            estado TEXT NOT NULL CHECK(estado IN ('COBRADO','REEMBOLSADO')),
            referencia_pasarela TEXT NOT NULL UNIQUE,
            creado_en TEXT NOT NULL, actualizado_en TEXT NOT NULL
        )""")
        setup_journal(db)


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_location(db_path, "/data/pagos.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(path)
        yield

    app = FastAPI(title="Microservicio Pagos (simulado)", version="1.0.0", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"servicio": SERVICE, "estado": "ok", "pasarela": "SIMULADA"}

    @app.post("/cobros", status_code=201)
    def cobrar(request: CobroIn):
        centavos = int(request.monto * 100)
        amount = str(Decimal(centavos) / 100)
        data = {"montoCentavos": centavos}
        duplicated = False
        with database(path, write=True) as db:
            existing = prior_command(db, request.sagaId, "cobrar", data)
            if existing is not None:
                result = existing
                duplicated = True
            elif db.execute("SELECT 1 FROM comandos WHERE saga_id=? AND accion='reembolsar'", (request.sagaId,)).fetchone():
                result = (409, {"error": "SAGA_COMPENSADA", "detalle": "No es posible cobrar tras un reembolso"})
                record_command(db, request.sagaId, "cobrar", data, *result)
            else:
                reference = "SIM-" + request.sagaId
                instant = now()
                db.execute("INSERT INTO cobros VALUES(?,?,?,?,?,?,?)",
                           (request.sagaId, centavos, "CLP", "COBRADO", reference, instant, instant))
                result = (201, {"sagaId": request.sagaId, "monto": amount, "moneda": "CLP",
                                "estado": "COBRADO", "referenciaPasarela": reference})
                record_command(db, request.sagaId, "cobrar", data, *result)
        log_event(request.sagaId, SERVICE, "cobrar", "OK" if result[0] == 201 else "FALLO",
                  codigo=result[0], idempotente=duplicated)
        return out(result)

    @app.post("/cobros/{saga_id}/reembolso")
    def reembolsar(saga_id: str):
        duplicated = False
        with database(path, write=True) as db:
            existing = prior_command(db, saga_id, "reembolsar", {})
            if existing is not None:
                result = existing
                duplicated = True
            else:
                row = db.execute("SELECT estado, monto_centavos FROM cobros WHERE saga_id=?", (saga_id,)).fetchone()
                if row is None:
                    status = "SIN_COBRO"
                else:
                    if row["estado"] == "COBRADO":
                        db.execute("UPDATE cobros SET estado='REEMBOLSADO', actualizado_en=? WHERE saga_id=?", (now(), saga_id))
                    status = "REEMBOLSADO"
                result = (200, {"sagaId": saga_id, "estado": status})
                record_command(db, saga_id, "reembolsar", {}, *result)
        log_event(saga_id, SERVICE, "reembolsar", "OK", codigo=200, idempotente=duplicated)
        return out(result)

    @app.get("/cobros/{saga_id}")
    def consultar_cobro(saga_id: str):
        with database(path) as db:
            row = db.execute("SELECT * FROM cobros WHERE saga_id=?", (saga_id,)).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "COBRO_NO_ENCONTRADO"})
        return {"sagaId": row["saga_id"], "monto": str(Decimal(row["monto_centavos"]) / 100),
                "moneda": row["moneda"], "estado": row["estado"],
                "referenciaPasarela": row["referencia_pasarela"],
                "creadoEn": row["creado_en"], "actualizadoEn": row["actualizado_en"]}

    return app


app = create_app()
