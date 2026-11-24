"""Pruebas del orquestador contra dobles de los servicios de A."""
import asyncio
import time

import pytest

from orquestador.db import BaseSagas

pytestmark = pytest.mark.anyio

RESERVA = {"espacioId": "E-001", "patente": "abcd12", "monto": 4000}


async def esperar_estado(c, saga_id, esperado, limite_s=3.0):
    """La recuperación corre en segundo plano al arrancar: se espera a que termine."""
    fin = time.perf_counter() + limite_s
    while True:
        saga = (await c.get(f"/reservas-estacionamiento/{saga_id}")).json()
        if saga["estado"] == esperado or time.perf_counter() > fin:
            return saga
        await asyncio.sleep(0.02)


def acciones(entorno, saga_id):
    return [accion.split()[0] for _, accion in entorno.registro.llamadas if accion.endswith(saga_id)]


async def test_camino_feliz(entorno):
    env = entorno()
    async with env.cliente() as c:
        r = await c.post("/reservas-estacionamiento", json=RESERVA)
        assert r.status_code == 201
        saga_id = r.json()["sagaId"]
        assert r.json()["estado"] == "COMPLETADA"

        detalle = (await c.get(f"/reservas-estacionamiento/{saga_id}")).json()
        estados = [t["estado"] for t in detalle["transiciones"]]
        assert estados == ["INICIADA", "ESPACIO_RESERVADO", "PAGO_COBRADO", "COMPLETADA"]
        assert detalle["patente"] == "ABCD12"
    assert acciones(env, saga_id) == ["reservar", "cobrar", "habilitar"]
    assert await env.estado_en_a(saga_id) == ("RESERVADA", "COBRADO", "HABILITADA")


async def test_espacio_ocupado_falla_sin_compensar(entorno):
    env = entorno()
    async with env.cliente() as c:
        assert (await c.post("/reservas-estacionamiento", json=RESERVA)).status_code == 201
        r = await c.post("/reservas-estacionamiento", json={**RESERVA, "patente": "ZZZZ99"})
        assert r.status_code == 409
        assert r.json()["estado"] == "FALLIDA"
        assert r.json()["motivo"] == "ESPACIO_OCUPADO"
    assert acciones(env, r.json()["sagaId"]) == ["reservar"]  # no se cobró nada


@pytest.mark.parametrize("modo", ["error", "caido"])
async def test_falla_acceso_compensa_en_orden_inverso(entorno, modo):
    env = entorno()
    env.falla("acceso", modo)
    async with env.cliente() as c:
        r = await c.post("/reservas-estacionamiento", json=RESERVA)
        assert r.status_code == 503
        saga_id = r.json()["sagaId"]
        assert r.json()["estado"] == "COMPENSADA"
        assert r.json()["motivo"] == f"ACCESO_HTTP_5XX_{502 if modo == 'error' else 503}"
    assert acciones(env, saga_id) == ["reservar", "cobrar", "habilitar", "revocar", "reembolsar", "liberar"]
    assert await env.estado_en_a(saga_id) == ("LIBERADA", "REEMBOLSADO", "REVOCADA")


async def test_acceso_lento_dispara_timeout_y_compensa(entorno):
    env = entorno(timeout_s=0.3)
    env.falla("acceso", "lento", 800)
    async with env.cliente() as c:
        inicio = time.perf_counter()
        r = await c.post("/reservas-estacionamiento", json=RESERVA)
        duracion = time.perf_counter() - inicio
    assert r.json()["estado"] == "COMPENSADA"
    assert r.json()["motivo"] == "ACCESO_TIMEOUT"
    assert duracion < 0.75, "el timeout debe cortar antes de que Acceso responda"
    # La habilitación tardía, si llega, es rechazada porque ya se revocó.
    await asyncio.sleep(0.6)
    assert (await env.estado_en_a(r.json()["sagaId"]))[2] == "REVOCADA"


async def test_falla_en_pagos_solo_libera_espacio(entorno):
    env = entorno()
    env.falla("pagos", "error")
    async with env.cliente() as c:
        r = await c.post("/reservas-estacionamiento", json=RESERVA)
    saga_id = r.json()["sagaId"]
    assert r.json()["estado"] == "COMPENSACION_PENDIENTE"  # pagos sigue caído para el reembolso
    env.falla("pagos", "ok")
    async with env.cliente() as c:  # al reiniciar, la recuperación termina la compensación
        detalle = await esperar_estado(c, saga_id, "COMPENSADA")
    assert detalle["estado"] == "COMPENSADA"
    assert "habilitar" not in acciones(env, saga_id)
    assert (await env.estado_en_a(saga_id))[0] == "LIBERADA"


async def test_circuit_breaker_abre_falla_rapido_y_se_recupera(entorno):
    env = entorno(cb_umbral_fallos=3, cb_espera_s=10)
    env.falla("acceso", "caido")
    async with env.cliente() as c:
        for i in range(3):
            espacio = f"E-00{i + 1}"
            r = await c.post("/reservas-estacionamiento", json={**RESERVA, "espacioId": espacio})
            assert r.json()["estado"] == "COMPENSADA"
        m = (await c.get("/metrics")).json()["dependencias"]["acceso"]["circuitBreaker"]
        assert m["estado"] == "ABIERTO" and m["aperturas"] == 1

        # Con el circuito abierto: rechazo inmediato, sin reservar ni cobrar.
        llamadas_antes = len(env.registro.llamadas)
        r = await c.post("/reservas-estacionamiento", json={**RESERVA, "espacioId": "E-009"})
        assert r.status_code == 503
        assert r.json() == {"sagaId": r.json()["sagaId"], "estado": "FALLIDA", "motivo": "CIRCUITO_ABIERTO_ACCESO"}
        assert len(env.registro.llamadas) == llamadas_antes

        # Pasa el tiempo de espera y Acceso se recupera: SEMI_ABIERTO -> prueba ok -> CERRADO.
        env.falla("acceso", "ok")
        env.reloj.t += 10
        assert (await c.get("/health")).json()["circuitos"]["acceso"] == "SEMI_ABIERTO"
        r = await c.post("/reservas-estacionamiento", json={**RESERVA, "espacioId": "E-010"})
        assert r.json()["estado"] == "COMPLETADA"
        assert (await c.get("/health")).json()["circuitos"]["acceso"] == "CERRADO"


async def test_semi_abierto_que_falla_vuelve_a_abrir(entorno):
    env = entorno(cb_umbral_fallos=1)
    env.falla("acceso", "error")
    async with env.cliente() as c:
        await c.post("/reservas-estacionamiento", json=RESERVA)
        env.reloj.t += 10
        r = await c.post("/reservas-estacionamiento", json={**RESERVA, "espacioId": "E-002"})
        assert r.json()["estado"] == "COMPENSADA"  # la llamada de prueba se hizo y falló
        cb = (await c.get("/metrics")).json()["dependencias"]["acceso"]["circuitBreaker"]
        assert cb["estado"] == "ABIERTO" and cb["aperturas"] == 2


async def test_bulkhead_limita_llamadas_concurrentes_a_acceso(entorno):
    env = entorno(bulkhead_max=2, timeout_s=2.0)
    env.falla("acceso", "lento", 400)
    async with env.cliente() as c:
        respuestas = await asyncio.gather(*[
            c.post("/reservas-estacionamiento", json={**RESERVA, "espacioId": f"E-00{i}"}) for i in range(1, 7)])
        estados = sorted((r.json()["estado"], r.json()["motivo"] or "") for r in respuestas)
        metrics = (await c.get("/metrics")).json()["dependencias"]["acceso"]
    assert estados.count(("COMPLETADA", "")) == 2
    assert estados.count(("COMPENSADA", "BULKHEAD_LLENO_ACCESO")) == 4
    assert metrics["bulkhead"]["pico"] == 2
    assert metrics["bulkhead"]["rechazos"] == 4
    # Los rechazos del bulkhead no son fallos del servicio: el circuito sigue cerrado.
    assert metrics["circuitBreaker"]["estado"] == "CERRADO"


async def test_saga_id_repetido_es_idempotente(entorno):
    env = entorno()
    async with env.cliente() as c:
        r1 = await c.post("/reservas-estacionamiento", json={**RESERVA, "sagaId": "CLIENTE-1"})
        r2 = await c.post("/reservas-estacionamiento", json={**RESERVA, "sagaId": "CLIENTE-1"})
    assert r1.json() == r2.json() and r1.status_code == r2.status_code == 201
    assert acciones(env, "CLIENTE-1") == ["reservar", "cobrar", "habilitar"]


async def test_recuperacion_tras_caida_del_orquestador(entorno):
    env = entorno()
    # Simula que el orquestador murió justo mientras llamaba a Acceso.
    db = BaseSagas(env.config.db_path)
    db.crear("CAIDA-1", "E-003", "XYZ123", "4000", "INICIADA", "RESERVAR")
    db.transicion("CAIDA-1", "ESPACIO_RESERVADO", avance="COBRAR")
    db.transicion("CAIDA-1", "PAGO_COBRADO", avance="HABILITAR")
    async with env.cliente() as c:
        saga = await esperar_estado(c, "CAIDA-1", "COMPENSADA")
    assert saga["estado"] == "COMPENSADA"
    assert acciones(env, "CAIDA-1") == ["revocar", "reembolsar", "liberar"]


async def test_servicio_inalcanzable_cuenta_como_fallo(entorno):
    env = entorno()
    env.red.caidos.add("pagos")
    async with env.cliente() as c:
        r = await c.post("/reservas-estacionamiento", json=RESERVA)
    assert r.json()["motivo"] == "PAGOS_CONEXION"
    assert r.json()["estado"] == "COMPENSACION_PENDIENTE"


async def test_validacion_y_404(entorno):
    env = entorno()
    async with env.cliente() as c:
        assert (await c.post("/reservas-estacionamiento", json={**RESERVA, "monto": -1})).status_code == 422
        assert (await c.post("/reservas-estacionamiento", json={**RESERVA, "monto": 1.234})).status_code == 422
        assert (await c.post("/reservas-estacionamiento", json={"patente": "A"})).status_code == 422
        r = await c.get("/reservas-estacionamiento/NO-EXISTE")
        assert r.status_code == 404 and r.json()["error"] == "SAGA_NO_ENCONTRADA"


async def test_metrics_expone_latencias_y_conteos(entorno):
    env = entorno()
    async with env.cliente() as c:
        await c.post("/reservas-estacionamiento", json=RESERVA)
        await c.post("/reservas-estacionamiento", json=RESERVA)  # ocupado -> FALLIDA
        m = (await c.get("/metrics")).json()
    assert m["sagas"]["porEstado"] == {"COMPLETADA": 1, "FALLIDA": 1}
    esp = m["dependencias"]["espacios"]
    assert esp["resultados"] == {"ok": 1, "rechazo_4xx": 1}
    assert esp["latenciaMs"]["p95"] is not None
    assert m["dependencias"]["acceso"]["bulkhead"]["maximo"] == 5
