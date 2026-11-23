"""Microservicio Espacios: exclusión de reservas y compensación idempotente."""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
import sqlite3
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.models import ReservaIn
from common.util import database, db_location, log_event, now, out, prior_command, record_command, setup_journal

SERVICE = "espacios"
DEFAULT_SPACES = [f"E-{i:03d}" for i in range(1, 11)]


def init_db(path: str) -> None:
    with database(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS espacios (
            id TEXT PRIMARY KEY,
            creado_en TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS reservas (
            saga_id TEXT PRIMARY KEY,
            espacio_id TEXT NOT NULL REFERENCES espacios(id),
            estado TEXT NOT NULL CHECK(estado IN ('RESERVADA','LIBERADA')),
            creado_en TEXT NOT NULL,
            actualizado_en TEXT NOT NULL
        )""")
        # La restricción parcial también protege de condiciones de carrera entre solicitudes.
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS una_reserva_activa_por_espacio
                      ON reservas(espacio_id) WHERE estado='RESERVADA'""")
        setup_journal(db)
        db.executemany("INSERT OR IGNORE INTO espacios(id,creado_en) VALUES(?,?)",
                       [(space, now()) for space in DEFAULT_SPACES])


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_location(db_path, "/data/espacios.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(path)
        yield

    app = FastAPI(title="Microservicio Espacios", version="1.0.0", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"servicio": SERVICE, "estado": "ok"}

    @app.get("/espacios")
    def listar_espacios():
        with database(path) as db:
            rows = db.execute("""SELECT e.id AS espacioId,
                  CASE WHEN r.saga_id IS NULL THEN 'DISPONIBLE' ELSE 'RESERVADO' END AS estado,
                  r.saga_id AS sagaId
                  FROM espacios e LEFT JOIN reservas r
                  ON r.espacio_id=e.id AND r.estado='RESERVADA'
                  ORDER BY e.id""").fetchall()
            return {"espacios": [dict(row) for row in rows]}

    @app.get("/reservas/{saga_id}")
    def consultar_reserva(saga_id: str):
        with database(path) as db:
            row = db.execute("""SELECT saga_id AS sagaId, espacio_id AS espacioId,
                             estado, creado_en AS creadoEn, actualizado_en AS actualizadoEn
                             FROM reservas WHERE saga_id=?""", (saga_id,)).fetchone()
        return dict(row) if row else JSONResponse(status_code=404, content={"error": "RESERVA_NO_ENCONTRADA"})

    @app.post("/reservas", status_code=201)
    def reservar(request: ReservaIn):
        data = {"espacioId": request.espacioId}
        duplicated = False
        with database(path, write=True) as db:
            existing = prior_command(db, request.sagaId, "reservar", data)
            if existing is not None:
                result = existing
                duplicated = True
            elif db.execute("SELECT 1 FROM comandos WHERE saga_id=? AND accion='liberar'", (request.sagaId,)).fetchone():
                result = (409, {"error": "SAGA_COMPENSADA", "detalle": "No es posible reservar tras la compensación"})
                record_command(db, request.sagaId, "reservar", data, *result)
            elif not db.execute("SELECT 1 FROM espacios WHERE id=?", (request.espacioId,)).fetchone():
                result = (404, {"error": "ESPACIO_NO_ENCONTRADO", "espacioId": request.espacioId})
                record_command(db, request.sagaId, "reservar", data, *result)
            elif db.execute("SELECT 1 FROM reservas WHERE espacio_id=? AND estado='RESERVADA'", (request.espacioId,)).fetchone():
                result = (409, {"error": "ESPACIO_OCUPADO", "espacioId": request.espacioId})
                record_command(db, request.sagaId, "reservar", data, *result)
            else:
                instant = now()
                db.execute("INSERT INTO reservas VALUES(?,?,?,?,?)",
                           (request.sagaId, request.espacioId, "RESERVADA", instant, instant))
                result = (201, {"sagaId": request.sagaId, "espacioId": request.espacioId, "estado": "RESERVADA"})
                record_command(db, request.sagaId, "reservar", data, *result)
        log_event(request.sagaId, SERVICE, "reservar", "OK" if result[0] == 201 else "FALLO",
                  codigo=result[0], idempotente=duplicated)
        return out(result)

    @app.post("/reservas/{saga_id}/liberar")
    def liberar(saga_id: str):
        duplicated = False
        with database(path, write=True) as db:
            existing = prior_command(db, saga_id, "liberar", {})
            if existing is not None:
                result = existing
                duplicated = True
            else:
                reservation = db.execute("SELECT estado, espacio_id FROM reservas WHERE saga_id=?", (saga_id,)).fetchone()
                if reservation is not None and reservation["estado"] == "RESERVADA":
                    db.execute("UPDATE reservas SET estado='LIBERADA', actualizado_en=? WHERE saga_id=?", (now(), saga_id))
                    status = "LIBERADA"
                elif reservation is not None:
                    status = "LIBERADA"
                else:
                    # Una compensación adelantada deja evidencia persistente (tombstone).
                    status = "SIN_RESERVA"
                result = (200, {"sagaId": saga_id, "estado": status})
                record_command(db, saga_id, "liberar", {}, *result)
        log_event(saga_id, SERVICE, "liberar", "OK", codigo=200, idempotente=duplicated)
        return out(result)

    return app


app = create_app()
