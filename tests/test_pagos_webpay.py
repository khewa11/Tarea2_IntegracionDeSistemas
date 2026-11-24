"""Pagos con PASARELA=webpay usando un doble del SDK (sin red).

FakeTransaction imita la interfaz de transbank.webpay.webpay_plus.transaction.Transaction
(create/commit/status/refund devuelven dicts como el SDK 6.x) y cuenta las llamadas.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from pagos.main import create_app
from pagos.pasarelas import PasarelaWebpay, buy_order_de


class TransbankErrorFalso(Exception):
    """Mismos atributos que transbank.error.transbank_error.TransbankError."""

    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


class Timeout(Exception):
    """Nombre análogo a requests.exceptions.Timeout."""


class FakeTransaction:
    def __init__(self):
        self.llamadas = {"create": 0, "commit": 0, "status": 0, "refund": 0}
        self.resultado_commit = "AUTHORIZED"
        self.estado_status = "INITIALIZED"
        self.falla: dict[str, Exception] = {}
        self.creadas = {}
        self.reembolsos = []

    def _quizas_fallar(self, op):
        self.llamadas[op] += 1
        if op in self.falla:
            raise self.falla[op]

    def create(self, buy_order, session_id, amount, return_url):
        self._quizas_fallar("create")
        assert len(buy_order) <= 26 and len(session_id) <= 61
        token = f"tok{len(self.creadas):061d}"
        self.creadas[token] = {"buy_order": buy_order, "amount": amount, "return_url": return_url}
        return {"token": token, "url": "https://webpay3gint.transbank.cl/webpayserver/initTransaction"}

    def commit(self, token):
        self._quizas_fallar("commit")
        aprobado = self.resultado_commit == "AUTHORIZED"
        return {"vci": "TSY", "amount": self.creadas[token]["amount"], "status": self.resultado_commit,
                "buy_order": self.creadas[token]["buy_order"], "session_id": "x",
                "card_detail": {"card_number": "6623"}, "authorization_code": "1213" if aprobado else "000000",
                "payment_type_code": "VN", "response_code": 0 if aprobado else -1, "installments_number": 0}

    def status(self, token):
        self._quizas_fallar("status")
        return {"status": self.estado_status}

    def refund(self, token, amount):
        self._quizas_fallar("refund")
        self.reembolsos.append((token, amount))
        return {"type": "NULLIFIED", "authorization_code": "123456", "nullified_amount": amount,
                "balance": 0, "response_code": 0}


@pytest.fixture
def webpay(tmp_path):
    tx = FakeTransaction()
    pasarela = PasarelaWebpay(return_url="https://demo.ngrok.app/pagos/retorno", transaccion=tx)
    with TestClient(create_app(str(tmp_path / "pagos.db"), pasarela=pasarela)) as client:
        yield client, tx


def iniciar(client, saga="W-1", monto=4000):
    r = client.post("/cobros", json={"sagaId": saga, "monto": monto})
    assert r.status_code == 202, r.text
    return r.json()


def test_iniciar_devuelve_pendiente_con_token_y_url(webpay):
    client, tx = webpay
    cuerpo = iniciar(client)
    assert cuerpo["estado"] == "PENDIENTE"
    assert cuerpo["token"] and cuerpo["url"].startswith("https://webpay3gint")
    assert cuerpo["buyOrder"] == "W-1"
    assert tx.creadas[cuerpo["token"]]["amount"] == 4000
    assert tx.creadas[cuerpo["token"]]["return_url"] == "https://demo.ngrok.app/pagos/retorno"
    # Repetir el inicio no crea otra transacción en Webpay.
    assert client.post("/cobros", json={"sagaId": "W-1", "monto": 4000}).json() == cuerpo
    assert tx.llamadas["create"] == 1
    assert client.get("/cobros/W-1").json()["estado"] == "PENDIENTE"
    assert client.get("/health").json()["pasarela"] == "WEBPAY_PLUS_INTEGRACION"


def test_buy_order_derivado_maximo_26():
    largo = "S-" + "f" * 32
    assert len(buy_order_de(largo)) == 26
    assert buy_order_de(largo) == buy_order_de(largo)
    assert buy_order_de("S-corto") == "S-corto"


def test_monto_clp_con_decimales_se_rechaza(webpay):
    client, tx = webpay
    r = client.post("/cobros", json={"sagaId": "W-dec", "monto": 1000.5})
    assert r.status_code == 422 and r.json()["error"] == "MONTO_CLP_ENTERO"
    assert tx.llamadas["create"] == 0


def test_pago_aprobado(webpay):
    client, tx = webpay
    token = iniciar(client)["token"]
    r = client.post("/cobros/W-1/confirmar", json={"token": token})
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["estado"] == "COBRADO" and cuerpo["responseCode"] == 0 and cuerpo["authorizationCode"] == "1213"
    assert "card_detail" not in r.text and "6623" not in r.text
    detalle = client.get("/cobros/W-1").json()
    assert detalle["estado"] == "COBRADO" and detalle["authorizationCode"] == "1213"


def test_pago_rechazado(webpay):
    client, tx = webpay
    tx.resultado_commit = "FAILED"
    token = iniciar(client)["token"]
    r = client.post("/cobros/W-1/confirmar", json={"token": token})
    assert r.status_code == 200 and r.json()["estado"] == "RECHAZADO" and r.json()["responseCode"] == -1
    # Compensar un cobro rechazado no devuelve dinero.
    assert client.post("/cobros/W-1/reembolso").json()["estado"] == "RECHAZADO"
    assert tx.llamadas["refund"] == 0


def test_commit_repetido_es_idempotente(webpay):
    client, tx = webpay
    token = iniciar(client)["token"]
    r1 = client.post("/cobros/W-1/confirmar", json={"token": token})
    r2 = client.post("/cobros/W-1/confirmar", json={"token": token})
    assert r1.json() == r2.json() and r1.status_code == r2.status_code == 200
    assert tx.llamadas["commit"] == 1


def test_token_ajeno_no_confirma(webpay):
    client, tx = webpay
    iniciar(client)
    r = client.post("/cobros/W-1/confirmar", json={"token": "otro"})
    assert r.status_code == 409 and r.json()["error"] == "TOKEN_NO_CORRESPONDE"
    assert tx.llamadas["commit"] == 0


def test_reembolso_total_y_repetido(webpay):
    client, tx = webpay
    token = iniciar(client, monto=4500)["token"]
    client.post("/cobros/W-1/confirmar", json={"token": token})
    r1 = client.post("/cobros/W-1/reembolso")
    r2 = client.post("/cobros/W-1/reembolso")
    assert r1.status_code == 200 and r1.json()["estado"] == "REEMBOLSADO"
    assert r1.json() == r2.json()
    assert tx.reembolsos == [(token, 4500)]  # monto total, una sola vez
    assert client.get("/cobros/W-1").json()["estado"] == "REEMBOLSADO"


def test_anulado_por_usuario_no_llama_refund_y_bloquea_commit_tardio(webpay):
    client, tx = webpay
    token = iniciar(client)["token"]
    r = client.post("/cobros/W-1/reembolso")  # compensación de la saga tras TBK_TOKEN
    assert r.json()["estado"] == "ANULADO"
    assert tx.llamadas["status"] == 1 and tx.llamadas["refund"] == 0
    tardio = client.post("/cobros/W-1/confirmar", json={"token": token})
    assert tardio.status_code == 409 and tardio.json()["error"] == "SAGA_COMPENSADA"
    assert tx.llamadas["commit"] == 0
    assert client.get("/cobros/W-1").json()["estado"] == "ANULADO"


def test_compensar_pendiente_ya_autorizado_reembolsa(webpay):
    """El commit se perdió (timeout) pero Webpay autorizó: la compensación devuelve el dinero."""
    client, tx = webpay
    token = iniciar(client)["token"]
    tx.estado_status = "AUTHORIZED"
    assert client.post("/cobros/W-1/reembolso").json()["estado"] == "REEMBOLSADO"
    assert tx.reembolsos == [(token, 4000)]


def test_compensacion_antes_del_inicio_impide_cobro_tardio(webpay):
    client, tx = webpay
    assert client.post("/cobros/W-2/reembolso").json()["estado"] == "SIN_COBRO"
    r = client.post("/cobros", json={"sagaId": "W-2", "monto": 4000})
    assert r.status_code == 409 and r.json()["error"] == "SAGA_COMPENSADA"
    assert tx.llamadas["create"] == 0


def test_fallos_de_webpay_no_se_cachean(webpay):
    client, tx = webpay
    tx.falla["create"] = Timeout("read timed out")
    r = client.post("/cobros", json={"sagaId": "W-1", "monto": 4000})
    assert r.status_code == 504 and r.json()["error"] == "PASARELA_NO_DISPONIBLE"
    del tx.falla["create"]
    token = iniciar(client)["token"]  # el reintento sí crea la transacción

    tx.falla["commit"] = TransbankErrorFalso("Internal error", 500)
    assert client.post("/cobros/W-1/confirmar", json={"token": token}).status_code == 502
    del tx.falla["commit"]
    assert client.post("/cobros/W-1/confirmar", json={"token": token}).json()["estado"] == "COBRADO"

    tx.falla["refund"] = Timeout("read timed out")
    assert client.post("/cobros/W-1/reembolso").status_code == 504
    del tx.falla["refund"]
    assert client.post("/cobros/W-1/reembolso").json()["estado"] == "REEMBOLSADO"


def test_status_4xx_al_compensar_pendiente_anula(webpay):
    client, tx = webpay
    iniciar(client)
    tx.falla["status"] = TransbankErrorFalso("Transaction not found", 422)
    assert client.post("/cobros/W-1/reembolso").json()["estado"] == "ANULADO"


def test_migracion_desde_esquema_v1_conserva_datos(tmp_path):
    ruta = tmp_path / "v1.db"
    con = sqlite3.connect(ruta)
    con.execute("""CREATE TABLE cobros (
        saga_id TEXT PRIMARY KEY,
        monto_centavos INTEGER NOT NULL CHECK(monto_centavos>0),
        moneda TEXT NOT NULL DEFAULT 'CLP',
        estado TEXT NOT NULL CHECK(estado IN ('COBRADO','REEMBOLSADO')),
        referencia_pasarela TEXT NOT NULL UNIQUE,
        creado_en TEXT NOT NULL, actualizado_en TEXT NOT NULL)""")
    con.execute("INSERT INTO cobros VALUES ('OLD-1', 150075, 'CLP', 'COBRADO', 'SIM-OLD-1', 't', 't')")
    con.commit()
    con.close()
    with TestClient(create_app(str(ruta))) as client:
        assert client.get("/cobros/OLD-1").json()["monto"] == "1500.75"
        assert client.post("/cobros/OLD-1/reembolso").json()["estado"] == "REEMBOLSADO"
        assert client.post("/cobros", json={"sagaId": "NEW-1", "monto": 10}).status_code == 201
    with TestClient(create_app(str(ruta))) as client:  # segunda apertura: sin migrar otra vez
        assert client.get("/cobros/OLD-1").json()["estado"] == "REEMBOLSADO"


def test_configuracion_webpay(monkeypatch):
    from pagos.pasarelas import crear_pasarela
    monkeypatch.setenv("TBK_ENV", "produccion")
    with pytest.raises(ValueError):
        crear_pasarela("webpay", transaccion=FakeTransaction())
    monkeypatch.setenv("TBK_ENV", "integracion")
    monkeypatch.delenv("PAGOS_RETURN_URL", raising=False)
    with pytest.raises(ValueError):
        crear_pasarela("webpay", transaccion=FakeTransaction())
    monkeypatch.setenv("PAGOS_RETURN_URL", "https://x/pagos/retorno")
    assert crear_pasarela("webpay", transaccion=FakeTransaction()).asincrona
