import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orquestador.config import Config  # noqa: E402
from orquestador.main import create_app  # noqa: E402

from .fakes import Enrutador, Registro, fake_acceso, fake_espacios, fake_pagos  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Reloj:
    """Reloj manipulable para avanzar el tiempo del Circuit Breaker sin esperar."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Entorno:
    def __init__(self, tmp_path: Path, **config):
        self.registro = Registro()
        self.espacios = fake_espacios(self.registro)
        self.pagos = fake_pagos(self.registro)
        self.acceso = fake_acceso(self.registro)
        self.red = Enrutador({"espacios": self.espacios, "pagos": self.pagos, "acceso": self.acceso})
        self.reloj = Reloj()
        base = dict(db_path=str(tmp_path / "orquestador.db"), timeout_s=0.3, cb_umbral_fallos=3,
                    cb_espera_s=10, bulkhead_max=5, compensacion_reintentos=2,
                    compensacion_pausa_s=0.01, recuperacion_intervalo_s=0)
        base.update(config)
        self.config = Config(**base)

    def falla(self, servicio: str, modo: str, delay_ms: int = 0):
        f = getattr(self, servicio).state.falla
        f.modo, f.delay_ms = modo, delay_ms

    @asynccontextmanager
    async def cliente(self):
        app = create_app(self.config, transport=self.red, reloj=self.reloj)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://orquestador") as c:
                yield c

    async def estado_en_a(self, saga_id: str) -> tuple:
        async with httpx.AsyncClient(transport=self.red) as c:
            e = (await c.get(f"http://espacios/reservas/{saga_id}")).json().get("estado")
            p = (await c.get(f"http://pagos/cobros/{saga_id}")).json().get("estado")
            a = (await c.get(f"http://acceso/habilitaciones/{saga_id}")).json().get("estado")
        return e, p, a


@pytest.fixture
def entorno(tmp_path):
    return lambda **config: Entorno(tmp_path, **config)
