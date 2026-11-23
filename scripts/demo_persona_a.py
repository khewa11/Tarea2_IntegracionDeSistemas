"""Demo HTTP de Persona A. Requiere los tres servicios en localhost 5001-5003.

No ejecuta el orquestador: emula sus llamadas de manera secuencial.
"""
from __future__ import annotations
import json
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = {'espacios': 'http://127.0.0.1:5001',
        'pagos': 'http://127.0.0.1:5002',
        'acceso': 'http://127.0.0.1:5003'}


def call(service, method, path, data=None):
    body = json.dumps(data).encode('utf-8') if data is not None else None
    req = Request(BASE[service] + path, method=method, data=body,
                  headers={'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=15) as response:
            status, result = response.status, json.loads(response.read())
    except HTTPError as exc:
        status, result = exc.code, json.loads(exc.read())
    print(f'{service:8} {method:4} {path:40} HTTP {status}: {json.dumps(result, ensure_ascii=False)}')
    return status, result


def check(status, expected):
    assert status == expected, f'Se esperaba HTTP {expected}, obtenido {status}'


def main():
    for name in BASE:
        check(call(name, 'GET', '/health')[0], 200)
    ok_id = 'demo-ok-' + uuid.uuid4().hex[:8]
    bad_id = 'demo-fail-' + uuid.uuid4().hex[:8]
    print(f'\n=== CAMINO FELIZ {ok_id} ===')
    check(call('espacios', 'POST', '/reservas', {'sagaId': ok_id, 'espacioId': 'E-001'})[0], 201)
    check(call('pagos', 'POST', '/cobros', {'sagaId':ok_id, 'monto':4500})[0], 201)
    check(call('acceso', 'POST', '/habilitaciones', {'sagaId':ok_id,'patente':'ABCD12','espacioId':'E-001'})[0], 201)
    print('\n=== FALLO ULTIMO PASO + COMPENSACION ===')
    check(call('espacios', 'POST', '/reservas', {'sagaId':bad_id, 'espacioId':'E-002'})[0], 201)
    check(call('pagos', 'POST', '/cobros', {'sagaId':bad_id, 'monto':2500})[0], 201)
    try:
        call('acceso', 'POST', '/admin/falla', {'modo':'error','delayMs':0})
        check(call('acceso', 'POST', '/habilitaciones', {'sagaId':bad_id,'patente':'XYZZ99','espacioId':'E-002'})[0], 502)
        # Las compensaciones deben ir en el orden inverso de los pasos.
        check(call('acceso', 'POST', f'/habilitaciones/{bad_id}/revocar')[0], 200)
        check(call('pagos', 'POST', f'/cobros/{bad_id}/reembolso')[0], 200)
        check(call('espacios', 'POST', f'/reservas/{bad_id}/liberar')[0], 200)
    finally:
        call('acceso', 'POST', '/admin/falla', {'modo':'ok','delayMs':0})
    assert call('espacios', 'GET', f'/reservas/{bad_id}')[1]['estado'] == 'LIBERADA'
    assert call('pagos', 'GET', f'/cobros/{bad_id}')[1]['estado'] == 'REEMBOLSADO'
    assert call('acceso', 'GET', f'/habilitaciones/{bad_id}')[1]['estado'] == 'REVOCADA'
    print('\n✔ DEMO A: éxito y compensación verificados. Para CB/Bulkhead integrar Persona B.')


if __name__ == '__main__':
    main()
