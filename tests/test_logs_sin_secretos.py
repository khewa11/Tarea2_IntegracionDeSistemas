"""Los logs JSON de los pasos nuevos respetan el formato y nunca exponen secretos.

Se usa el SDK real (Transaction + WebpayOptions) con una API key ficticia y se
reemplaza RequestService para no salir a la red.
"""
import json

import pytest
from fastapi.testclient import TestClient

from pagos.main import create_app
from pagos.pasarelas import PasarelaWebpay

API_KEY = "CLAVE-SECRETA-DE-PRUEBA-NO-DEBE-APARECER"
TARJETA = "4051885600446623"
TOKEN = "e" * 64
CAMPOS = {"timestamp", "sagaId", "servicio", "paso", "resultado"}


@pytest.fixture
def sdk_sin_red(monkeypatch):
    from transbank.common.request_service import RequestService
    from transbank.error.transbank_error import TransbankError

    estado = {"fallar_refund": True}

    def post(endpoint, request, options):
        assert options.api_key == API_KEY  # la key sí llega al SDK...
        if endpoint.endswith("/refunds"):
            if estado["fallar_refund"]:
                estado["fallar_refund"] = False
                raise TransbankError(message="Invalid api key " + API_KEY, code=401)
            return {"type": "NULLIFIED", "response_code": 0, "authorization_code": "123456"}
        return {"token": TOKEN, "url": "https://webpay3gint.transbank.cl/webpayserver/initTransaction"}

    def put(endpoint, request, options):
        return {"status": "AUTHORIZED", "response_code": 0, "authorization_code": "1213",
                "card_detail": {"card_number": TARJETA[-4:], "card_full": TARJETA}, "amount": 4000,
                "buy_order": "LOG-1"}

    monkeypatch.setattr(RequestService, "post", classmethod(lambda cls, e, r, o: post(e, r, o)))
    monkeypatch.setattr(RequestService, "put", classmethod(lambda cls, e, r, o: put(e, r, o)))


def lineas_json(salida: str) -> list[dict]:
    return [json.loads(linea) for linea in salida.splitlines() if linea.startswith("{")]


def test_pagos_webpay_no_registra_api_key_token_ni_tarjeta(tmp_path, capsys, sdk_sin_red):
    pasarela = PasarelaWebpay(return_url="https://demo.ngrok.app/pagos/retorno", api_key=API_KEY)
    respuestas = []
    with TestClient(create_app(str(tmp_path / "pagos.db"), pasarela=pasarela)) as c:
        respuestas.append(c.post("/cobros", json={"sagaId": "LOG-1", "monto": 4000}))
        respuestas.append(c.post("/cobros/LOG-1/confirmar", json={"token": TOKEN}))
        respuestas.append(c.post("/cobros/LOG-1/reembolso"))   # falla: la key va en el mensaje del SDK
        respuestas.append(c.post("/cobros/LOG-1/reembolso"))   # reintento ok
        respuestas.append(c.get("/cobros/LOG-1"))
    salida = capsys.readouterr().out

    assert [r.status_code for r in respuestas] == [202, 200, 502, 200, 200]
    eventos = lineas_json(salida)
    pasos = [e["paso"] for e in eventos if e["servicio"] == "pagos"]
    assert {"cobrar", "confirmar", "reembolsar"} <= set(pasos)
    for evento in eventos:
        assert CAMPOS <= evento.keys()
    for texto in [salida, *(r.text for r in respuestas[1:])]:
        assert API_KEY not in texto
        assert TARJETA not in texto and "card_detail" not in texto
    assert TOKEN not in salida  # el token de pago tampoco se registra


def test_gateway_retorno_no_registra_token(monkeypatch, capsys):
    import httpx
    import gateway.main as gw

    async def upstream(method, url, body, headers):
        return httpx.Response(200, json={"sagaId": "S-log", "estado": "COMPLETADA", "motivo": None},
                              request=httpx.Request(method, url))

    monkeypatch.setattr(gw, "llamar_upstream", upstream)
    gw.request_history.clear()
    with TestClient(gw.app) as c:
        assert c.get(f"/pagos/retorno?token_ws={TOKEN}", follow_redirects=False).status_code == 303
    salida = capsys.readouterr().out
    eventos = [e for e in lineas_json(salida) if e["paso"] == "RETORNO_PAGO"]
    assert eventos and CAMPOS <= eventos[0].keys() and eventos[0]["sagaId"] == "S-log"
    assert TOKEN not in salida
