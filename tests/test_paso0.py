"""Verificación del contrato inicial y del scaffold del Paso 0."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {
    "gateway": 8080,
    "bff": 3000,
    "orquestador": 4000,
    "espacios": 5001,
    "pagos": 5002,
    "acceso": 5003,
}


def test_compose_seis_servicios_y_solo_gateway_publico():
    data = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(data["services"]) == set(SERVICES)
    for name, port in SERVICES.items():
        definition = data["services"][name]
        assert (ROOT / definition["build"]["dockerfile"]).is_file()
        assert int(definition["environment"]["PORT"]) == port
        assert definition["environment"]["SERVICE"] == name
        if name == "gateway":
            assert definition["ports"] == ["127.0.0.1:8080:8080"]
        else:
            assert not definition.get("ports")
    assert "JWT_SECRET" in data["services"]["gateway"]["environment"]
    assert "JWT_SECRET" not in data["services"]["espacios"]["environment"]
    assert "JWT_SECRET" not in data["services"]["pagos"]["environment"]
    assert "JWT_SECRET" not in data["services"]["acceso"]["environment"]
    assert set(data["volumes"]) == {"espacios_data", "pagos_data", "acceso_data", "orquestador_data"}
    # Cada servicio con estado usa su propio volumen (la Saga no reutiliza las bases de A).
    assert data["services"]["orquestador"]["volumes"] == ["orquestador_data:/data"]


def test_carpeta_y_contrato_paso0():
    contract = (ROOT / "CONTRATO.md").read_text(encoding="utf-8")
    for service, port in SERVICES.items():
        assert service in contract
        assert str(port) in contract
        assert (ROOT / service / "Dockerfile").exists()
    for phrase in ("HS256", "JWT_SECRET", "sagaId", "timestamp", "servicio", "paso", "resultado", "orquestada"):
        assert phrase.lower() in contract.lower()
    assert (ROOT / ".env.example").is_file()
    assert (ROOT / "README_PASO_0.md").is_file()
    assert (ROOT / "docs/ARQUITECTURA_PASO_0.md").is_file()


def test_stub_responde_salud_y_501_sin_fingir_gateway_funcional():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, SERVICE="gateway", PORT=str(port))
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scaffold" / "stub.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.2) as resp:
                    info = json.load(resp)
                    assert resp.status == 200
                    assert info["estado"] == "stub"
                    assert info["implementado"] is False
                    break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.05)
        else:
            pytest.fail("El stub no alcanzó a iniciar")

        request = urllib.request.Request(f"http://127.0.0.1:{port}/reservas-estacionamiento", method="POST",
                                         data=b'{"sagaId":"test-paso0"}', headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exception:
            urllib.request.urlopen(request, timeout=2)
        assert exception.value.code == 501
        assert json.load(exception.value)["error"] == "PENDIENTE_IMPLEMENTACION"
    finally:
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
