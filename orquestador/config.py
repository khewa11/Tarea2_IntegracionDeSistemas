"""Configuración del orquestador leída desde variables de entorno."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _float(nombre: str, defecto: float) -> float:
    return float(os.environ.get(nombre, defecto))


def _int(nombre: str, defecto: int) -> int:
    return int(os.environ.get(nombre, defecto))


@dataclass(frozen=True)
class Config:
    db_path: str = "orquestador.db"
    espacios_url: str = "http://espacios:5001"
    pagos_url: str = "http://pagos:5002"
    acceso_url: str = "http://acceso:5003"
    # Timeout por llamada HTTP (segundos). Acceso en modo "lento" con 2500 ms lo supera.
    timeout_s: float = 2.0
    # Circuit Breaker: fallos consecutivos para abrir y segundos antes de SEMI_ABIERTO.
    cb_umbral_fallos: int = 3
    cb_espera_s: float = 10.0
    # Bulkhead sobre Acceso: máximo de llamadas simultáneas (sin cola).
    bulkhead_max: int = 5
    # Reintentos de cada compensación y pausa base entre ellos (backoff lineal).
    compensacion_reintentos: int = 3
    compensacion_pausa_s: float = 0.3
    # Cada cuántos segundos se reintentan compensaciones pendientes (0 = desactivado).
    recuperacion_intervalo_s: float = 15.0

    @classmethod
    def desde_entorno(cls) -> "Config":
        return cls(
            db_path=os.environ.get("DB_PATH", cls.db_path),
            espacios_url=os.environ.get("ESPACIOS_URL", cls.espacios_url),
            pagos_url=os.environ.get("PAGOS_URL", cls.pagos_url),
            acceso_url=os.environ.get("ACCESO_URL", cls.acceso_url),
            timeout_s=_float("TIMEOUT_S", cls.timeout_s),
            cb_umbral_fallos=_int("CB_UMBRAL_FALLOS", cls.cb_umbral_fallos),
            cb_espera_s=_float("CB_ESPERA_S", cls.cb_espera_s),
            bulkhead_max=_int("BULKHEAD_MAX", cls.bulkhead_max),
            compensacion_reintentos=_int("COMPENSACION_REINTENTOS", cls.compensacion_reintentos),
            compensacion_pausa_s=_float("COMPENSACION_PAUSA_S", cls.compensacion_pausa_s),
            recuperacion_intervalo_s=_float("RECUPERACION_INTERVALO_S", cls.recuperacion_intervalo_s),
        )
