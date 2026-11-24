"""Saga con pasarela asíncrona (Webpay) contra dobles de A, sin red."""
import pytest

from .test_saga import esperar_estado

pytestmark = pytest.mark.anyio

RESERVA = {"espacioId": "E-001", "patente": "abcd12", "monto": 4000}


def acciones(entorno, saga_id):
    return [accion.split()[0] for _, accion in entorno.registro.llamadas if accion.endswith(saga_id)]


def entorno_webpay(entorno, **config):
    env = entorno(**config)
    env.pagos.state.webpay = True
    return env


async def iniciar(c, saga_id="WP-1"):
    r = await c.post("/reservas-estacionamiento", json={**RESERVA, "sagaId": saga_id})
    assert r.status_code == 202, r.text
    return r.json()


async def test_reserva_queda_pago_pendiente_con_token_y_url(entorno):
    env = entorno_webpay(entorno)
    async with env.cliente() as c:
        cuerpo = await iniciar(c)
        assert cuerpo == {"sagaId": "WP-1", "estado": "PAGO_PENDIENTE", "motivo": None,
                          "pago": {"token": "tok-WP-1", "url": "https://webpay.test/init"}}
        # Repetir el POST con el mismo sagaId devuelve el mismo pago, sin cobrar otra vez.
        assert await iniciar(c) == cuerpo
        detalle = (await c.get("/reservas-estacionamiento/WP-1")).json()
        assert [t["estado"] for t in detalle["transiciones"]] == ["INICIADA", "ESPACIO_RESERVADO", "PAGO_PENDIENTE"]
        assert detalle["avance"] == "COBRAR"
    assert acciones(env, "WP-1") == ["reservar", "cobrar"]
    assert await env.estado_en_a("WP-1") == ("RESERVADA", "PENDIENTE", None)


async def test_pago_aprobado_continua_con_acceso(entorno):
    env = entorno_webpay(entorno)
    async with env.cliente() as c:
        await iniciar(c)
        r = await c.post("/pagos/retorno", json={"token_ws": "tok-WP-1"})
        assert r.status_code == 200
        assert r.json() == {"sagaId": "WP-1", "estado": "COMPLETADA", "motivo": None}
        detalle = (await c.get("/reservas-estacionamiento/WP-1")).json()
        estados = [t["estado"] for t in detalle["transiciones"]]
        assert estados[-3:] == ["PAGO_PENDIENTE", "PAGO_COBRADO", "COMPLETADA"]
    assert acciones(env, "WP-1") == ["reservar", "cobrar", "confirmar", "habilitar"]
    assert await env.estado_en_a("WP-1") == ("RESERVADA", "COBRADO", "HABILITADA")


async def test_retorno_repetido_es_idempotente(entorno):
    env = entorno_webpay(entorno)
    async with env.cliente() as c:
        await iniciar(c)
        r1 = await c.post("/pagos/retorno", json={"token_ws": "tok-WP-1"})
        r2 = await c.post("/pagos/retorno", json={"token_ws": "tok-WP-1"})
    assert r1.json() == r2.json()
    assert env.pagos.state.commits == 1
    assert acciones(env, "WP-1").count("habilitar") == 1


async def test_pago_rechazado_compensa_liberando(entorno):
    env = entorno_webpay(entorno)
    env.pagos.state.resultado_pago = "FAILED"
    async with env.cliente() as c:
        await iniciar(c)
        r = await c.post("/pagos/retorno", json={"token_ws": "tok-WP-1"})
    assert r.json() == {"sagaId": "WP-1", "estado": "COMPENSADA", "motivo": "PAGO_RECHAZADO"}
    assert acciones(env, "WP-1") == ["reservar", "cobrar", "confirmar", "reembolsar", "liberar"]
    assert await env.estado_en_a("WP-1") == ("LIBERADA", "RECHAZADO", None)


async def test_pago_anulado_por_usuario(entorno):
    env = entorno_webpay(entorno)
    async with env.cliente() as c:
        await iniciar(c)
        r = await c.post("/pagos/retorno", json={"TBK_TOKEN": "tok-WP-1", "TBK_ORDEN_COMPRA": "WP-1",
                                                  "TBK_ID_SESION": "WP-1"})
        assert r.json() == {"sagaId": "WP-1", "estado": "COMPENSADA", "motivo": "PAGO_ANULADO"}
        # Un token_ws tardío ya no confirma nada.
        tardio = await c.post("/pagos/retorno", json={"token_ws": "tok-WP-1"})
        assert tardio.json()["estado"] == "COMPENSADA"
    assert "confirmar" not in acciones(env, "WP-1")
    assert await env.estado_en_a("WP-1") == ("LIBERADA", "ANULADO", None)


async def test_formulario_vencido_en_webpay_solo_orden_de_compra(entorno):
    env = entorno_webpay(entorno)
    async with env.cliente() as c:
        await iniciar(c)
        r = await c.post("/pagos/retorno", json={"TBK_ORDEN_COMPRA": "WP-1", "TBK_ID_SESION": "WP-1"})
    assert r.json()["estado"] == "COMPENSADA" and r.json()["motivo"] == "PAGO_EXPIRADO_WEBPAY"


async def test_timeout_de_pago_compensa_en_recuperacion(entorno):
    env = entorno_webpay(entorno, pago_timeout_s=0)
    async with env.cliente() as c:
        await iniciar(c)
    async with env.cliente() as c:  # la recuperación al arrancar detecta el pago vencido
        detalle = await esperar_estado(c, "WP-1", "COMPENSADA")
    assert detalle["estado"] == "COMPENSADA" and detalle["motivo"] == "PAGO_TIMEOUT"
    assert await env.estado_en_a("WP-1") == ("LIBERADA", "ANULADO", None)


async def test_pago_pendiente_vigente_no_se_compensa_en_recuperacion(entorno):
    env = entorno_webpay(entorno, pago_timeout_s=300)
    async with env.cliente() as c:
        await iniciar(c)
    async with env.cliente() as c:
        assert (await c.get("/reservas-estacionamiento/WP-1")).json()["estado"] == "PAGO_PENDIENTE"
        r = await c.post("/pagos/retorno", json={"token_ws": "tok-WP-1"})
        assert r.json()["estado"] == "COMPLETADA"


async def test_timeout_al_confirmar_compensa(entorno):
    env = entorno_webpay(entorno, timeout_s=0.3)
    async with env.cliente() as c:
        await iniciar(c)
        env.falla("pagos", "lento", 800)
        r = await c.post("/pagos/retorno", json={"token_ws": "tok-WP-1"})
        assert r.json()["motivo"] == "PAGOS_TIMEOUT"
        env.falla("pagos", "ok")
    assert "habilitar" not in acciones(env, "WP-1")


async def test_retorno_desconocido_o_vacio(entorno):
    env = entorno_webpay(entorno)
    async with env.cliente() as c:
        assert (await c.post("/pagos/retorno", json={"token_ws": "no-existe"})).status_code == 404
        assert (await c.post("/pagos/retorno", json={})).status_code == 422
