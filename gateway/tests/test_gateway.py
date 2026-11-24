import os
import pytest
from fastapi.testclient import TestClient

os.environ["JWT_SECRET"] = "test-secret-key-for-pytest-32-bytes"
os.environ["RATE_LIMIT_MAX"] = "3"

from gateway.main import app, request_history  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def limpiar_rate_limit():
    request_history.clear()
    yield
    request_history.clear()


def test_health_publico():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["servicio"] == "gateway"
    assert body["jwtConfigurado"] is True


def test_rechaza_sin_token():
    resp = client.get("/metrics")
    assert resp.status_code == 401
    assert resp.json()["error"] == "TOKEN_FALTANTE_O_INVALIDO"


def test_rechaza_token_invalido():
    resp = client.get("/metrics", headers={"Authorization": "Bearer falso.token.jwt"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "TOKEN_INVALIDO"


def test_bloquea_ruta_admin_con_403():
    tok = client.post("/auth/token", json={"clienteId": "user-1"}).json()["access_token"]
    resp = client.post(
        "/admin/falla",
        json={"modo": "caido"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "FORBIDDEN_ADMIN_ENDPOINT"


def test_rate_limiting_por_cliente():
    tok = client.post("/auth/token", json={"clienteId": "spammer-test"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}

    # Las primeras 3 pasan el rate limiter (responden 404 por ruta inexistente)
    for _ in range(3):
        r = client.get("/ruta-inexistente", headers=headers)
        assert r.status_code == 404

    # La 4ta dispara 429
    r4 = client.get("/ruta-inexistente", headers=headers)
    assert r4.status_code == 429
    assert r4.json()["error"] == "RATE_LIMIT_EXCEEDED"