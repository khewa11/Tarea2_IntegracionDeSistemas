from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from bff.main import app

client = TestClient(app)


def test_bff_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"servicio": "bff", "estado": "ok", "implementado": True}


def test_mi_reserva_completa_sin_degradacion():
    async def fake_consultar(client_http, url, timeout_s=2.0):
        if "/reservas-estacionamiento/" in url:
            return 200, {"sagaId": "S-100", "estado": "COMPLETADA", "motivo": None}
        if "/reservas/" in url:
            return 200, {"sagaId": "S-100", "espacioId": "E-001", "estado": "RESERVADA"}
        if "/cobros/" in url:
            return 200, {"sagaId": "S-100", "monto": "4000", "estado": "COBRADO"}
        if "/habilitaciones/" in url:
            return 200, {"sagaId": "S-100", "patente": "ABCD12", "estado": "HABILITADA"}
        return 404, {}

    with patch("bff.main.consultar_servicio", side_effect=AsyncMock(side_effect=fake_consultar)):
        resp = client.get("/bff/mi-reserva/S-100")

    assert resp.status_code == 200
    data = resp.json()
    assert data["sagaId"] == "S-100"
    assert data["estadoGeneral"] == "COMPLETADA"
    assert data["degradacionActiva"] is False
    assert data["acceso"]["estado"] == "HABILITADA"


def test_mi_reserva_degradacion_elegante_cuando_acceso_falla():
    async def fake_consultar(client_http, url, timeout_s=2.0):
        if "/reservas-estacionamiento/" in url:
            return 200, {"sagaId": "S-200", "estado": "COMPLETADA", "motivo": None}
        if "/reservas/" in url:
            return 200, {"sagaId": "S-200", "espacioId": "E-002", "estado": "RESERVADA"}
        if "/cobros/" in url:
            return 200, {"sagaId": "S-200", "monto": "4000", "estado": "COBRADO"}
        if "/habilitaciones/" in url:
            return 504, {"error": "TIMEOUT"}
        return 404, {}

    with patch("bff.main.consultar_servicio", side_effect=AsyncMock(side_effect=fake_consultar)):
        resp = client.get("/bff/mi-reserva/S-200")

    assert resp.status_code == 200
    data = resp.json()
    assert data["degradacionActiva"] is True
    assert data["espacio"]["estado"] == "RESERVADA"
    assert data["pago"]["estado"] == "COBRADO"
    assert data["acceso"]["estado"] == "no_disponible"


def test_dashboard_agregado():
    async def fake_consultar(client_http, url, timeout_s=2.0):
        if url.endswith("/espacios"):
            return 200, {"espacios": [{"espacioId": "E-001", "estado": "DISPONIBLE"}]}
        if url.endswith("/metrics"):
            return 200, {"totalSagas": 5, "completadas": 4}
        return 404, {}

    with patch("bff.main.consultar_servicio", side_effect=AsyncMock(side_effect=fake_consultar)):
        resp = client.get("/bff/dashboard")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["espacios"]) == 1
    assert data["espacios"][0]["espacioId"] == "E-001"
    assert data["metricasSistema"]["totalSagas"] == 5

def test_mi_reserva_pago_pendiente_expone_url_y_mantiene_degradacion():
    async def fake_consultar(client_http, url, timeout_s=2.0):
        if "/reservas-estacionamiento/" in url:
            return 200, {"sagaId": "S-300", "estado": "PAGO_PENDIENTE", "motivo": None,
                         "pago": {"token": "tok-300", "url": "https://webpay3gint.transbank.cl/x",
                                  "buyOrder": "S-300", "limite": 1.0}}
        if "/reservas/" in url:
            return 200, {"sagaId": "S-300", "espacioId": "E-003", "estado": "RESERVADA"}
        if "/cobros/" in url:
            return 200, {"sagaId": "S-300", "monto": "4000", "estado": "PENDIENTE"}
        if "/habilitaciones/" in url:
            return 503, {"error": "CONNECTION_ERROR"}
        return 404, {}

    with patch("bff.main.consultar_servicio", side_effect=AsyncMock(side_effect=fake_consultar)):
        resp = client.get("/bff/mi-reserva/S-300")

    data = resp.json()
    assert resp.status_code == 200
    assert data["estadoGeneral"] == "PAGO_PENDIENTE"
    assert data["pago"]["estado"] == "PENDIENTE"
    assert data["pagoPendiente"] == {"url": "https://webpay3gint.transbank.cl/x", "token": "tok-300"}
    assert data["degradacionActiva"] is True and data["acceso"]["estado"] == "no_disponible"


def test_mi_reserva_completada_no_incluye_pago_pendiente():
    async def fake_consultar(client_http, url, timeout_s=2.0):
        if "/reservas-estacionamiento/" in url:
            return 200, {"sagaId": "S-400", "estado": "COMPLETADA", "motivo": None}
        return 200, {"estado": "OK"}

    with patch("bff.main.consultar_servicio", side_effect=AsyncMock(side_effect=fake_consultar)):
        data = client.get("/bff/mi-reserva/S-400").json()
    assert "pagoPendiente" not in data


def test_demo_estatica_servida_con_lista_blanca():
    index = client.get("/bff/demo")
    assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
    assert "/bff/demo/app.js" in index.text
    js = client.get("/bff/demo/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "token_ws" in js.text and "/auth/token" in js.text
    assert client.get("/bff/demo/styles.css").headers["content-type"].startswith("text/css")
    for prohibido in ("main.py", "..%2Fmain.py", "README.md"):
        assert client.get(f"/bff/demo/{prohibido}").status_code == 404
