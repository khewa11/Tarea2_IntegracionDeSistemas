"""Microservicio Acceso: barrera simulada, modos de fallo y revocación segura."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.models import FallaIn, HabilitacionIn
from common.util import database, db_location, log_event, now, out, prior_command, record_command, setup_journal

SERVICE = "acceso"


def init_db(path: str) -> None:
    with database(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS habilitaciones (
            saga_id TEXT PRIMARY KEY,
            patente TEXT,
            espacio_id TEXT,
            estado TEXT NOT NULL CHECK(estado IN ('HABILITADA','REVOCADA')),
            creado_en TEXT NOT NULL,
            actualizado_en TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS configuracion_falla (
            id INTEGER PRIMARY KEY CHECK(id=1),
            modo TEXT NOT NULL CHECK(modo IN ('ok','lento','caido','error')),
            delay_ms INTEGER NOT NULL CHECK(delay_ms>=0)
        )""")
        db.execute("INSERT OR IGNORE INTO configuracion_falla(id,modo,delay_ms) VALUES(1,'ok',0)")
        setup_journal(db)


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_location(db_path, "/data/acceso.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(path)
        yield

    app = FastAPI(title="Microservicio Acceso (barrera inestable)", version="1.0.0", lifespan=lifespan)

    def get_fault():
        with database(path) as db:
            return dict(db.execute("SELECT modo, delay_ms AS delayMs FROM configuracion_falla WHERE id=1").fetchone())

    async def emulate_fault(saga_id: str | None, step: str):
        fault = get_fault()
        if fault["modo"] == "lento":
            log_event(saga_id, SERVICE, step, "LENTITUD_INDUCIDA", delayMs=fault["delayMs"])
            await asyncio.sleep(fault["delayMs"] / 1000)
        elif fault["modo"] in {"caido", "error"}:
            status = 503 if fault["modo"] == "caido" else 502
            log_event(saga_id, SERVICE, step, "FALLO_INDUCIDO", modo=fault["modo"], codigo=status)
            return JSONResponse(status_code=status, content={
                "error": "BARRERA_NO_DISPONIBLE" if status == 503 else "ERROR_CONTROLADOR_EXTERNO",
                "modo": fault["modo"], "servicio": SERVICE})
        return None

    @app.get("/health")
    def health():
        # Salud del microservicio, NO del controlador simulado.
        return {"servicio": SERVICE, "estado": "ok", "controlador": get_fault()["modo"]}

    @app.post("/admin/falla")
    def definir_falla(request: FallaIn):
        with database(path, write=True) as db:
            db.execute("UPDATE configuracion_falla SET modo=?, delay_ms=? WHERE id=1",
                       (request.modo, request.delayMs))
        log_event(None, SERVICE, "configurar_falla", "OK", modo=request.modo, delayMs=request.delayMs)
        return {"modo": request.modo, "delayMs": request.delayMs}

    @app.get("/admin/falla")
    def consultar_falla():
        return get_fault()

    @app.post("/habilitaciones", status_code=201)
    async def habilitar(request: HabilitacionIn):
        data = {"patente": request.patente, "espacioId": request.espacioId}
        duplicated = False
        # Primero comprobar el registro ya confirmado: los reintentos no vuelven a tocar la barrera.
        with database(path) as db:
            old = prior_command(db, request.sagaId, "habilitar", data)
        if old is not None:
            log_event(request.sagaId, SERVICE, "habilitar", "IDEMPOTENTE", codigo=old[0])
            return out(old)

        failure = await emulate_fault(request.sagaId, "habilitar")
        if failure is not None:
            return failure

        # La comprobación tras la espera es crucial: impide la habilitación tardía
        # si el orquestador ya compensó por timeout mientras la barrera simulada dormía.
        with database(path, write=True) as db:
            existing = prior_command(db, request.sagaId, "habilitar", data)
            if existing is not None:
                result = existing
                duplicated = True
            elif db.execute("SELECT 1 FROM comandos WHERE saga_id=? AND accion='revocar'", (request.sagaId,)).fetchone():
                result = (409, {"error": "SAGA_COMPENSADA", "detalle": "Habilitación impedida por revocación previa"})
                record_command(db, request.sagaId, "habilitar", data, *result)
            else:
                instant = now()
                db.execute("INSERT INTO habilitaciones VALUES(?,?,?,?,?,?)",
                           (request.sagaId, request.patente, request.espacioId, "HABILITADA", instant, instant))
                result = (201, {"sagaId": request.sagaId, "patente": request.patente,
                                "espacioId": request.espacioId, "estado": "HABILITADA"})
                record_command(db, request.sagaId, "habilitar", data, *result)
        log_event(request.sagaId, SERVICE, "habilitar", "OK" if result[0] == 201 else "FALLO",
                  codigo=result[0], idempotente=duplicated)
        return out(result)

    @app.post("/habilitaciones/{saga_id}/revocar")
    def revocar(saga_id: str):
        # La compensación persiste una revocación lógica/tombstone aunque el controlador
        # externo simulado esté caído; no pretende confirmar una operación en hardware real.
        duplicated = False
        with database(path, write=True) as db:
            existing = prior_command(db, saga_id, "revocar", {})
            if existing is not None:
                result = existing
                duplicated = True
            else:
                row = db.execute("SELECT estado FROM habilitaciones WHERE saga_id=?", (saga_id,)).fetchone()
                if row is None:
                    instant = now()
                    db.execute("INSERT INTO habilitaciones VALUES(?,?,?,?,?,?)",
                               (saga_id, None, None, "REVOCADA", instant, instant))
                    state = "REVOCADA"
                else:
                    db.execute("UPDATE habilitaciones SET estado='REVOCADA', actualizado_en=? WHERE saga_id=?", (now(), saga_id))
                    state = "REVOCADA"
                result = (200, {"sagaId": saga_id, "estado": state})
                record_command(db, saga_id, "revocar", {}, *result)
        log_event(saga_id, SERVICE, "revocar", "OK", codigo=200, idempotente=duplicated)
        return out(result)

    @app.get("/habilitaciones/{saga_id}")
    async def consultar_habilitacion(saga_id: str):
        failure = await emulate_fault(saga_id, "consultar_habilitacion")
        if failure is not None:
            return failure
        with database(path) as db:
            row = db.execute("""SELECT saga_id AS sagaId, patente, espacio_id AS espacioId,
                          estado, creado_en AS creadoEn, actualizado_en AS actualizadoEn
                          FROM habilitaciones WHERE saga_id=?""", (saga_id,)).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "HABILITACION_NO_ENCONTRADA"})
        return dict(row)

    return app


app = create_app()
