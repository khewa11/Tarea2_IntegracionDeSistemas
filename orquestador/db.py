"""Persistencia propia de la Saga (SQLite). No comparte base con los servicios de A."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

ESQUEMA = """
CREATE TABLE IF NOT EXISTS sagas (
    saga_id        TEXT PRIMARY KEY,
    espacio_id     TEXT NOT NULL,
    patente        TEXT NOT NULL,
    monto          TEXT NOT NULL,
    estado         TEXT NOT NULL,
    avance         TEXT NOT NULL,          -- último paso de avance INTENTADO
    motivo         TEXT,
    creado_en      TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transiciones (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    saga_id   TEXT NOT NULL REFERENCES sagas(saga_id),
    estado    TEXT NOT NULL,
    detalle   TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transiciones_saga ON transiciones(saga_id, id);
CREATE INDEX IF NOT EXISTS idx_sagas_estado ON sagas(estado);
"""


# Columnas agregadas para pagos asíncronos (Webpay). Se agregan con ALTER TABLE si la
# base viene de una versión anterior; en modo simulado quedan en NULL.
COLUMNAS_PAGO = {
    "pago_token": "TEXT",
    "pago_url": "TEXT",
    "pago_buy_order": "TEXT",
    "pago_limite": "REAL",   # epoch (segundos) a partir del cual el pago pendiente expira
}


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class BaseSagas:
    def __init__(self, ruta: str):
        self.ruta = ruta
        if ruta != ":memory:":
            Path(ruta).parent.mkdir(parents=True, exist_ok=True)
        with self._conexion() as con:
            con.executescript(ESQUEMA)
            existentes = {f["name"] for f in con.execute("PRAGMA table_info(sagas)")}
            for nombre, tipo in COLUMNAS_PAGO.items():
                if nombre not in existentes:
                    con.execute(f"ALTER TABLE sagas ADD COLUMN {nombre} {tipo}")
            con.execute("CREATE INDEX IF NOT EXISTS idx_sagas_pago_token ON sagas(pago_token)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_sagas_pago_buy_order ON sagas(pago_buy_order)")

    @contextmanager
    def _conexion(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.ruta, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            yield con
            con.commit()
        finally:
            con.close()

    def crear(self, saga_id: str, espacio_id: str, patente: str, monto: str, estado: str,
              avance: str, motivo: str | None = None) -> bool:
        """Inserta la saga y su primera transición. False si el sagaId ya existía."""
        ts = ahora()
        with self._conexion() as con:
            try:
                con.execute(
                    "INSERT INTO sagas (saga_id, espacio_id, patente, monto, estado, avance, motivo, creado_en,"
                    " actualizado_en) VALUES (?,?,?,?,?,?,?,?,?)",
                    (saga_id, espacio_id, patente, monto, estado, avance, motivo, ts, ts))
            except sqlite3.IntegrityError:
                return False
            con.execute("INSERT INTO transiciones (saga_id, estado, detalle, timestamp) VALUES (?,?,?,?)",
                        (saga_id, estado, motivo, ts))
        return True

    def transicion(self, saga_id: str, estado: str, *, avance: str | None = None,
                   motivo: str | None = None, detalle: str | None = None) -> None:
        ts = ahora()
        with self._conexion() as con:
            con.execute(
                "UPDATE sagas SET estado=?, avance=COALESCE(?, avance), motivo=COALESCE(?, motivo),"
                " actualizado_en=? WHERE saga_id=?",
                (estado, avance, motivo, ts, saga_id))
            con.execute("INSERT INTO transiciones (saga_id, estado, detalle, timestamp) VALUES (?,?,?,?)",
                        (saga_id, estado, detalle or motivo, ts))

    def registrar_pago(self, saga_id: str, token: str, url: str, buy_order: str | None, limite: float) -> None:
        with self._conexion() as con:
            con.execute("UPDATE sagas SET pago_token=?, pago_url=?, pago_buy_order=?, pago_limite=? WHERE saga_id=?",
                        (token, url, buy_order, limite, saga_id))

    def buscar_por_pago(self, token: str | None = None, buy_order: str | None = None) -> str | None:
        with self._conexion() as con:
            if token:
                fila = con.execute("SELECT saga_id FROM sagas WHERE pago_token=?", (token,)).fetchone()
            elif buy_order:
                fila = con.execute("SELECT saga_id FROM sagas WHERE pago_buy_order=?", (buy_order,)).fetchone()
            else:
                return None
        return fila["saga_id"] if fila else None

    def obtener(self, saga_id: str) -> dict | None:
        with self._conexion() as con:
            fila = con.execute("SELECT * FROM sagas WHERE saga_id=?", (saga_id,)).fetchone()
            if fila is None:
                return None
            pasos = con.execute(
                "SELECT estado, detalle, timestamp FROM transiciones WHERE saga_id=? ORDER BY id",
                (saga_id,)).fetchall()
        saga = {
            "sagaId": fila["saga_id"],
            "estado": fila["estado"],
            "espacioId": fila["espacio_id"],
            "patente": fila["patente"],
            "monto": fila["monto"],
            "avance": fila["avance"],
            "motivo": fila["motivo"],
            "creadoEn": fila["creado_en"],
            "actualizadoEn": fila["actualizado_en"],
            "transiciones": [dict(p) for p in pasos],
        }
        if fila["pago_token"]:
            # Solo sagas con pasarela asíncrona: en modo simulado la respuesta no cambia.
            saga["pago"] = {"token": fila["pago_token"], "url": fila["pago_url"],
                            "buyOrder": fila["pago_buy_order"], "limite": fila["pago_limite"]}
        return saga

    def pendientes(self, estados: tuple[str, ...]) -> list[dict]:
        marcas = ",".join("?" * len(estados))
        with self._conexion() as con:
            filas = con.execute(
                f"SELECT saga_id FROM sagas WHERE estado IN ({marcas}) ORDER BY creado_en", estados
            ).fetchall()
        return [self.obtener(f["saga_id"]) for f in filas]

    def conteo_por_estado(self) -> dict[str, int]:
        with self._conexion() as con:
            filas = con.execute("SELECT estado, COUNT(*) AS n FROM sagas GROUP BY estado").fetchall()
        return {f["estado"]: f["n"] for f in filas}
