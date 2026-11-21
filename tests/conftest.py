from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from espacios.main import create_app as create_espacios
from pagos.main import create_app as create_pagos
from acceso.main import create_app as create_acceso


@pytest.fixture
def clients(tmp_path: Path):
    with (TestClient(create_espacios(str(tmp_path / 'espacios.db'))) as espacios,
          TestClient(create_pagos(str(tmp_path / 'pagos.db'))) as pagos,
          TestClient(create_acceso(str(tmp_path / 'acceso.db'))) as acceso):
        yield espacios, pagos, acceso
