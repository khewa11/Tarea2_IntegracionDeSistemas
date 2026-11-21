
def test_cobro_reembolso_exactamente_una_vez(clients):
    _, pay, _ = clients
    payload = {'sagaId':'P-1','monto':1500.75}
    first = pay.post('/cobros', json=payload)
    assert first.status_code == 201
    assert first.json()['estado'] == 'COBRADO'
    assert pay.post('/cobros', json=payload).json() == first.json()
    assert pay.post('/cobros', json={'sagaId':'P-1','monto':9999}).status_code == 409
    assert pay.get('/cobros/P-1').json()['monto'] == '1500.75'
    refund = pay.post('/cobros/P-1/reembolso')
    assert refund.status_code == 200
    assert refund.json()['estado'] == 'REEMBOLSADO'
    assert pay.post('/cobros/P-1/reembolso').json() == refund.json()
    assert pay.get('/cobros/P-1').json()['estado'] == 'REEMBOLSADO'
    pay.post('/cobros', json=payload)
    assert pay.get('/cobros/P-1').json()['estado'] == 'REEMBOLSADO'


def test_adelantar_reembolso_impide_cobro_tardio(clients):
    _, pay, _ = clients
    assert pay.post('/cobros/P-2/reembolso').json()['estado'] == 'SIN_COBRO'
    assert pay.post('/cobros', json={'sagaId':'P-2','monto':1000}).status_code == 409
    assert pay.get('/cobros/P-2').status_code == 404


def test_validacion_montos(clients):
    _, pay, _ = clients
    for monto in (0, -1, 1.234):
        assert pay.post('/cobros', json={'sagaId':'P-invalid','monto':monto}).status_code == 422


def test_persistencia_tras_reinicio(tmp_path):
    """Los datos no dependen de la memoria RAM del proceso FastAPI."""
    from fastapi.testclient import TestClient
    from pagos.main import create_app
    location = str(tmp_path / 'persistente.db')
    with TestClient(create_app(location)) as client:
        assert client.post('/cobros',json={'sagaId':'PERSIST','monto':123}).status_code == 201
        client.post('/cobros/PERSIST/reembolso')
    with TestClient(create_app(location)) as client:
        assert client.get('/cobros/PERSIST').json()['estado'] == 'REEMBOLSADO'
        assert client.post('/cobros',json={'sagaId':'PERSIST','monto':123}).status_code == 201
        assert client.get('/cobros/PERSIST').json()['estado'] == 'REEMBOLSADO'
