import json
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

# --- Excepciones JWT: retorno de Webpay y demo estática -------------------------
import httpx  # noqa: E402

import gateway.main as gw  # noqa: E402


class Upstream:
    """Reemplaza llamar_upstream: registra las llamadas y responde lo configurado."""

    def __init__(self, status=200, body=None, content_type="application/json"):
        self.llamadas = []
        self.status, self.body, self.content_type = status, body, content_type

    async def __call__(self, method, url, body, headers):
        self.llamadas.append({"method": method, "url": url, "body": body, "headers": headers})
        contenido = self.body if isinstance(self.body, bytes) else json.dumps(self.body or {}).encode()
        return httpx.Response(self.status, content=contenido, headers={"content-type": self.content_type},
                              request=httpx.Request(method, url))



def test_retorno_webpay_get_sin_jwt_reenvia_y_redirige(monkeypatch):
    up = Upstream(body={"sagaId": "S-abc", "estado": "COMPLETADA", "motivo": None})
    monkeypatch.setattr(gw, "llamar_upstream", up)
    resp = client.get("/pagos/retorno?token_ws=tok123&otro=x", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/bff/demo?sagaId=S-abc"
    assert up.llamadas[0]["url"] == "http://orquestador:4000/pagos/retorno"
    assert json.loads(up.llamadas[0]["body"]) == {"token_ws": "tok123"}  # solo parámetros de Webpay


def test_retorno_webpay_post_formulario_anulacion(monkeypatch):
    up = Upstream(body={"sagaId": "S-anul", "estado": "COMPENSADA", "motivo": "PAGO_ANULADO"})
    monkeypatch.setattr(gw, "llamar_upstream", up)
    resp = client.post("/pagos/retorno", data={"TBK_TOKEN": "t1", "TBK_ORDEN_COMPRA": "S-anul",
                                               "TBK_ID_SESION": "S-anul"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/bff/demo?sagaId=S-anul"
    assert json.loads(up.llamadas[0]["body"]) == {"TBK_TOKEN": "t1", "TBK_ORDEN_COMPRA": "S-anul",
                                                  "TBK_ID_SESION": "S-anul"}


def test_retorno_desconocido_redirige_con_error(monkeypatch):
    monkeypatch.setattr(gw, "llamar_upstream", Upstream(status=404, body={"error": "SAGA_NO_ENCONTRADA"}))
    resp = client.get("/pagos/retorno?token_ws=nada", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/bff/demo?error=PAGO_NO_ENCONTRADO"


def test_demo_estatica_sin_jwt(monkeypatch):
    up = Upstream(body=b"<!doctype html><title>Demo</title>", content_type="text/html; charset=utf-8")
    monkeypatch.setattr(gw, "llamar_upstream", up)
    for ruta in ("/bff/demo", "/bff/demo/app.js"):
        resp = client.get(ruta)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
    assert [c["url"] for c in up.llamadas] == ["http://bff:3000/bff/demo", "http://bff:3000/bff/demo/app.js"]


def test_resto_de_rutas_siguen_exigiendo_jwt(monkeypatch):
    up = Upstream()
    monkeypatch.setattr(gw, "llamar_upstream", up)
    for metodo, ruta in [("GET", "/bff/mi-reserva/S-1"), ("GET", "/bff/dashboard"), ("GET", "/metrics"),
                         ("POST", "/reservas-estacionamiento"), ("GET", "/reservas-estacionamiento/S-1"),
                         ("POST", "/bff/demo"), ("GET", "/bff/demo/../mi-reserva/S-1"),
                         ("GET", "/bff/demonio"), ("GET", "/pagos/retorno/extra")]:
        resp = client.request(metodo, ruta)
        assert resp.status_code == 401, (metodo, ruta, resp.status_code)
    assert up.llamadas == []


def test_admin_falla_sigue_bloqueado_sin_y_con_prefijo_demo():
    assert client.post("/admin/falla", json={"modo": "caido"}).status_code == 403
    assert client.get("/bff/demo/admin/falla").status_code == 403
