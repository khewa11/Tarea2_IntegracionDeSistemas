from concurrent.futures import ThreadPoolExecutor
import time


def payload(saga):
    return {'sagaId':saga, 'patente':'ABCD12', 'espacioId':'E-001'}


def test_habilitar_y_revocar_idempotentes(clients):
    _, _, access = clients
    first = access.post('/habilitaciones', json=payload('H-1'))
    assert first.status_code == 201
    assert access.post('/habilitaciones', json=payload('H-1')).json() == first.json()
    assert access.post('/habilitaciones', json={'sagaId':'H-1','patente':'OTRA12','espacioId':'E-001'}).status_code == 409
    assert access.get('/habilitaciones/H-1').json()['estado'] == 'HABILITADA'
    revoke = access.post('/habilitaciones/H-1/revocar')
    assert revoke.status_code == 200
    assert access.post('/habilitaciones/H-1/revocar').json() == revoke.json()
    access.post('/habilitaciones', json=payload('H-1'))
    assert access.get('/habilitaciones/H-1').json()['estado'] == 'REVOCADA'


def test_modos_falla_y_reactivacion(clients):
    _, _, access = clients
    assert access.post('/admin/falla', json={'modo':'caido','delayMs':0}).status_code == 200
    assert access.post('/habilitaciones', json=payload('F-1')).status_code == 503
    assert access.get('/habilitaciones/F-1').status_code == 503
    assert access.get('/health').json()['controlador'] == 'caido'
    assert access.post('/admin/falla', json={'modo':'error'}).json()['modo'] == 'error'
    assert access.post('/habilitaciones', json=payload('F-1')).status_code == 502
    access.post('/admin/falla', json={'modo':'ok'})
    assert access.post('/habilitaciones', json=payload('F-1')).status_code == 201


def test_lentitud_es_real_y_revocar_evitar_habilitacion_tardia(clients):
    _, _, access = clients
    access.post('/admin/falla', json={'modo':'lento','delayMs':250})
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as executor:
        task = executor.submit(lambda: access.post('/habilitaciones', json=payload('H-LATE')))
        time.sleep(0.05)
        assert access.post('/habilitaciones/H-LATE/revocar').status_code == 200
        result = task.result(timeout=5)
    assert time.monotonic() - start >= 0.20
    assert result.status_code == 409
    access.post('/admin/falla', json={'modo':'ok'})
    assert access.get('/habilitaciones/H-LATE').json()['estado'] == 'REVOCADA'


def test_compensar_antes_de_habilitar(clients):
    _, _, access = clients
    assert access.post('/habilitaciones/X-2/revocar').json()['estado'] == 'REVOCADA'
    assert access.post('/habilitaciones', json=payload('X-2')).status_code == 409
