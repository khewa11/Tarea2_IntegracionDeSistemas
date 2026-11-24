"""Escenarios de integración sobre servicios reales. Ejecutar desde raíz del repo."""
import argparse
import concurrent.futures
import json
import subprocess
import time
import uuid
import httpx


def mostrar(etapa, data):
    print(json.dumps({'etapa': etapa, 'datos': data}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('escenario', choices=['1','2','3','todos'])
    parser.add_argument('--gateway', default='http://127.0.0.1:8080')
    parser.add_argument('--acceso-local', help='Solo para pruebas sin Docker: URL interna de Acceso')
    args = parser.parse_args()
    with httpx.Client(base_url=args.gateway, timeout=45, trust_env=False) as c:
        assert c.get('/health').json()['jwtConfigurado'], 'Configura JWT_SECRET'
        assert c.get('/metrics').status_code == 401
        token = c.post('/auth/token', json={'clienteId': 'demo-'+uuid.uuid4().hex}).json()['access_token']
        c.headers['Authorization'] = 'Bearer '+token
        assert c.post('/admin/falla', json={'modo':'ok'}).status_code == 403

        def falla(modo, delay=0):
            data = {'modo':modo,'delayMs':delay}
            if args.acceso_local:
                httpx.post(args.acceso_local+'/admin/falla', json=data, timeout=5).raise_for_status()
            else:
                code = ('import urllib.request; r=urllib.request.Request("http://localhost:5003/admin/falla", '
                        'data='+repr(json.dumps(data).encode())+', headers={"Content-Type":"application/json"}); '
                        'print(urllib.request.urlopen(r, timeout=5).read().decode())')
                subprocess.run(['docker','compose','exec','-T','acceso','python','-c',code],check=True)
            mostrar('falla',data)

        def dashboard():
            r=c.get('/bff/dashboard'); r.raise_for_status(); return r.json()

        def libres(n):
            spaces=[e['espacioId'] for e in dashboard()['espacios'] if e['estado']=='DISPONIBLE']
            assert len(spaces)>=n, f'Se requieren {n} espacios libres; usa una instancia de prueba limpia'
            return spaces[:n]

        def metrics():
            r=c.get('/metrics'); r.raise_for_status(); return r.json()['dependencias']['acceso']

        def esperar_circuito():
            # SEMI_ABIERTO permite una llamada de prueba; escenario 3 requiere CERRADO para el pico.
            m=metrics()['circuitBreaker']
            if m['estado']!='CERRADO':
                time.sleep(m['esperaSegundos']+0.2)
                raise RuntimeError('Circuito previo abierto: reinicia solo el orquestador antes del escenario (docker compose restart orquestador)')

        def reserva(space, sid=None):
            body={'espacioId':space,'patente':'TEST12','monto':4000,'sagaId':sid or 'S-'+uuid.uuid4().hex}
            start=time.perf_counter(); r=c.post('/reservas-estacionamiento',json=body)
            result={'http':r.status_code,'ms':round((time.perf_counter()-start)*1000,1),**r.json()}
            return result

        def bff(sid):
            r=c.get('/bff/mi-reserva/'+sid); r.raise_for_status(); return r.json()

        def saga(sid):
            r=c.get('/reservas-estacionamiento/'+sid); r.raise_for_status(); return r.json()

        try:
            falla('ok')
            esperar_circuito()
            if args.escenario in ('1','todos'):
                sp=libres(1)[0]; r=reserva(sp); mostrar('1_exito',r)
                assert r['http']==201 and r['estado']=='COMPLETADA'
                x=bff(r['sagaId']); mostrar('1_bff',x)
                assert [x[k]['estado'] for k in ('espacio','pago','acceso')]==['RESERVADA','COBRADO','HABILITADA']
                repeat=reserva(sp,r['sagaId']); assert repeat['http']==201 and repeat['sagaId']==r['sagaId']
                dupe=reserva(sp); assert dupe['http']==409
            if args.escenario in ('2','todos'):
                sp=libres(1)[0]; falla('error'); r=reserva(sp); mostrar('2_compensacion',r)
                assert r['http']==503 and r['estado']=='COMPENSADA'
                x=bff(r['sagaId']); mostrar('2_bff_degradado',x)
                assert x['degradacionActiva'] and x['espacio']['estado']=='LIBERADA' and x['pago']['estado']=='REEMBOLSADO'
                falla('ok'); x=bff(r['sagaId']); assert x['acceso']['estado']=='REVOCADA'
                trans=saga(r['sagaId'])['transiciones']; mostrar('2_transiciones',trans)
                acciones=[t['detalle'].split()[0] for t in trans if t['detalle'] and ' ok (' in t['detalle']]
                assert acciones==['REVOCAR','REEMBOLSAR','LIBERAR'], acciones
            if args.escenario in ('3','todos'):
                spaces=libres(7); before=metrics(); mostrar('3_metricas_antes',before)
                falla('lento',3500)
                with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
                    futures=[pool.submit(reserva,s) for s in spaces]
                    time.sleep(.2); d=dashboard(); mostrar('3_dashboard_durante_pico',{'espacios':len(d['espacios'])})
                    resultados=[f.result() for f in futures]
                mostrar('3_pico',resultados)
                after=metrics(); mostrar('3_metricas_despues',after)
                assert after['bulkhead']['rechazos']>before['bulkhead']['rechazos']
                assert after['bulkhead']['pico']<=after['bulkhead']['maximo']
                assert after['circuitBreaker']['estado']=='ABIERTO'
                falla('caido'); r=reserva(libres(1)[0]); mostrar('3_fail_fast',r)
                assert r['http']==503 and r['motivo']=='CIRCUITO_ABIERTO_ACCESO' and r['ms']<2000
                # El BFF continúa leyendo Espacios y métricas; no depende de Acceso.
                d=dashboard(); assert isinstance(d['espacios'],list) and isinstance(d['metricasSistema'],dict)
                mostrar('3_metricas_finales',metrics())
            mostrar('resultado','OK')
        finally:
            falla('ok')

if __name__=='__main__':
    main()
