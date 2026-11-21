from concurrent.futures import ThreadPoolExecutor


def test_espacio_no_se_reserva_dos_veces_y_se_libera(clients):
    esp, _, _ = clients
    first = esp.post('/reservas', json={'sagaId':'A-1','espacioId':'E-001'})
    assert first.status_code == 201
    assert first.json()['estado'] == 'RESERVADA'
    assert esp.post('/reservas', json={'sagaId':'A-1','espacioId':'E-001'}).json() == first.json()
    assert esp.post('/reservas', json={'sagaId':'A-2','espacioId':'E-001'}).status_code == 409
    assert esp.get('/reservas/A-1').json()['estado'] == 'RESERVADA'
    released = esp.post('/reservas/A-1/liberar')
    assert released.status_code == 200
    assert released.json()['estado'] == 'LIBERADA'
    assert esp.post('/reservas/A-1/liberar').json() == released.json()
    # Repetición tardía no puede revertir la compensación.
    assert esp.post('/reservas', json={'sagaId':'A-1','espacioId':'E-001'}).status_code == 201
    assert esp.get('/reservas/A-1').json()['estado'] == 'LIBERADA'
    assert esp.post('/reservas', json={'sagaId':'A-3','espacioId':'E-001'}).status_code == 201
    assert esp.post('/reservas', json={'sagaId':'A-2','espacioId':'E-001'}).status_code == 409


def test_reserva_identificador_no_se_reutiliza_con_otros_parametros(clients):
    esp, _, _ = clients
    assert esp.post('/reservas', json={'sagaId':'X','espacioId':'E-001'}).status_code == 201
    alt = esp.post('/reservas', json={'sagaId':'X','espacioId':'E-002'})
    assert alt.status_code == 409
    assert alt.json()['error'] == 'IDEMPOTENCY_KEY_REUSE'
    assert esp.post('/reservas', json={'sagaId':'Y','espacioId':'E-404'}).status_code == 404


def test_una_sola_reserva_en_competencia(clients):
    esp, _, _ = clients

    def reserve(i):
        return esp.post('/reservas', json={'sagaId':f'CON-{i}', 'espacioId':'E-002'}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(reserve, range(8)))
    assert codes.count(201) == 1
    assert codes.count(409) == 7


def test_tombstone_evitar_reserva_que_llega_tarde(clients):
    esp, _, _ = clients
    assert esp.post('/reservas/LATE/liberar').json()['estado'] == 'SIN_RESERVA'
    assert esp.post('/reservas', json={'sagaId':'LATE','espacioId':'E-004'}).status_code == 409
    assert next(x for x in esp.get('/espacios').json()['espacios'] if x['espacioId']=='E-004')['estado'] == 'DISPONIBLE'
