"""Logs JSON de una línea por evento (formato definido en CONTRATO.md)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any


def log(paso: str, resultado: str, saga_id: str | None = None, **extras: Any) -> None:
    evento = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "sagaId": saga_id,
        "servicio": "orquestador",
        "paso": paso,
        "resultado": resultado,
    }
    evento.update({k: v for k, v in extras.items() if v is not None})
    sys.stdout.write(json.dumps(evento, ensure_ascii=False) + "\n")
    sys.stdout.flush()
