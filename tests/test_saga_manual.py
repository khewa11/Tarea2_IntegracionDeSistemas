"""Simula la secuencia hecha por Persona B; no sustituye su orquestador."""


def test_camino_feliz_sin_orquestador(clients):
    esp, pay, acc = clients
    saga = 'DEMO-OK'
    assert esp.post('/reservas', json={'sagaId':saga,'espacioId':'E-005'}).status_code == 201
    assert pay.post('/cobros', json={'sagaId':saga,'monto':4000}).status_code == 201
    assert acc.post('/habilitaciones', json={'sagaId':saga,'patente':'AAAA11','espacioId':'E-005'}).status_code == 201
    assert esp.get(f'/reservas/{saga}').json()['estado'] == 'RESERVADA'
    assert pay.get(f'/cobros/{saga}').json()['estado'] == 'COBRADO'
    assert acc.get(f'/habilitaciones/{saga}').json()['estado'] == 'HABILITADA'


def test_falla_ultimo_paso_se_compensa_inversamente(clients):
    esp, pay, acc = clients
    saga = 'DEMO-ERR'
    assert esp.post('/reservas', json={'sagaId':saga,'espacioId':'E-006'}).status_code == 201
    assert pay.post('/cobros', json={'sagaId':saga,'monto':4000}).status_code == 201
    acc.post('/admin/falla', json={'modo':'error'})
    assert acc.post('/habilitaciones', json={'sagaId':saga,'patente':'AAAA11','espacioId':'E-006'}).status_code == 502
    # Orden inverso de Saga: revocar (aun sin confirmación), reembolsar, liberar.
    assert acc.post(f'/habilitaciones/{saga}/revocar').status_code == 200
    assert pay.post(f'/cobros/{saga}/reembolso').status_code == 200
    assert esp.post(f'/reservas/{saga}/liberar').status_code == 200
    acc.post('/admin/falla', json={'modo':'ok'})
    assert esp.get(f'/reservas/{saga}').json()['estado'] == 'LIBERADA'
    assert pay.get(f'/cobros/{saga}').json()['estado'] == 'REEMBOLSADO'
    assert acc.get(f'/habilitaciones/{saga}').json()['estado'] == 'REVOCADA'
