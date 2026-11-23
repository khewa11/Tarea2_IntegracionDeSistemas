"""Funciones compartidas sin lógica de negocio (solo infraestructura)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from fastapi.responses import JSONResponse


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def database(db_path: str, write: bool = False) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.execute("COMMIT")
    except BaseException:
        if write and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def setup_journal(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS comandos (
        saga_id TEXT NOT NULL,
        accion TEXT NOT NULL,
        huella TEXT NOT NULL,
        codigo INTEGER NOT NULL,
        respuesta TEXT NOT NULL,
        creado_en TEXT NOT NULL,
        PRIMARY KEY (saga_id, accion)
    )""")


def fingerprint(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prior_command(conn: sqlite3.Connection, saga_id: str, action: str, data: dict[str, Any]):
    row = conn.execute(
        "SELECT huella, codigo, respuesta FROM comandos WHERE saga_id=? AND accion=?",
        (saga_id, action),
    ).fetchone()
    if row is None:
        return None
    if row["huella"] != fingerprint(data):
        return (409, {"error": "IDEMPOTENCY_KEY_REUSE", "detalle": "sagaId reutilizado con datos diferentes"})
    return (row["codigo"], json.loads(row["respuesta"]))


def record_command(conn: sqlite3.Connection, saga_id: str, action: str,
                   data: dict[str, Any], status: int, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO comandos (saga_id, accion, huella, codigo, respuesta, creado_en) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (saga_id, action, fingerprint(data), status, json.dumps(payload, ensure_ascii=False), now()),
    )


def out(result: tuple[int, dict[str, Any]]) -> JSONResponse:
    return JSONResponse(status_code=result[0], content=result[1])


def log_event(saga_id: str | None, servicio: str, paso: str, resultado: str, **extras: Any) -> None:
    event: dict[str, Any] = {
        "timestamp": now(), "sagaId": saga_id, "servicio": servicio,
        "paso": paso, "resultado": resultado,
    }
    event.update(extras)
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def db_location(explicit: str | None, fallback: str) -> str:
    return explicit if explicit is not None else os.getenv("DB_PATH", fallback)
