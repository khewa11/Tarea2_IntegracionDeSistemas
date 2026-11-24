"""Escenarios de integración sobre servicios reales. Ejecutar desde raíz del repo."""
import argparse
import concurrent.futures
import json
import subprocess
import time
import uuid
import traceback
from pathlib import Path
from reportes import RAIZ, ahora, guardar_json, guardar_csv, aplanar
import httpx


class Reporte:
    def __init__(self, args):
        self.args = args
        self.inicio = ahora()
        self.reloj = time.perf_counter()
        self.eventos = []
        self.activo = None
        self.estados = {str(n): 'NO_EJECUTADO' for n in range(1, 4)
                        if args.escenario in (str(n), 'todos')}

    def iniciar(self, n):
        self.activo = str(n)
        self.estados[self.activo] = 'EN_CURSO'
        self.mostrar('inicio_escenario', {})

    def aprobar(self):
        self.estados[self.activo] = 'APROBADO'
        self.mostrar('fin_escenario', {'estado': 'APROBADO'})
        self.activo = None

    def mostrar(self, etapa, data):
        self.eventos.append({'timestamp_utc': ahora(), 'escenario': self.activo,
                             'etapa': etapa, 'datos': data})

    def guardar(self, error):
        if error and self.activo:
            self.estados[self.activo] = 'FALLIDO'
        datos = {'inicio_utc': self.inicio, 'fin_utc': ahora(),
                 'duracion_s': round(time.perf_counter()-self.reloj, 3),
                 'seleccion': self.args.escenario, 'gateway': self.args.gateway,
                 'entorno': 'HTTP local sin Docker' if self.args.acceso_local else 'Docker Compose',
                 'resultado': 'ERROR' if error else 'OK', 'error': error,
                 'escenarios': self.estados, 'eventos': self.eventos}
        salida = self.args.salida.resolve()
        nombre = 'escenarios' if self.args.escenario == 'todos' else 'escenario_' + self.args.escenario
        guardar_json(salida/(nombre+'.json'), datos)
        filas = []
        for evento in self.eventos:
            base = {k:v for k,v in evento.items() if k != 'datos'}
            d = evento['datos']
            # Una fila por solicitud concurrente o transición de la saga.
            for i, item in enumerate(d if isinstance(d, list) else [d], 1):
                filas.append({**base, 'item': i, **aplanar(item, 'datos')})
        guardar_csv(salida/(nombre+'.csv'), filas, ['timestamp_utc','escenario','etapa','item'])
        guardar_csv(salida/(nombre+'_resumen.csv'),
                    [{'escenario':n,'estado':e} for n,e in self.estados.items()], ['escenario','estado'])
        solicitudes, metricas, transiciones = [], [], []
        for ev in self.eventos:
            base = {k:v for k,v in ev.items() if k != 'datos'}
            d = ev['datos']
            if ev['etapa'] == '3_pico' or (isinstance(d, dict) and 'http' in d and 'ms' in d):
                solicitudes.extend({**base, **r} for r in (d if isinstance(d, list) else [d]))
            if ev['etapa'] in ('3_metricas_antes','3_metricas_despues','3_metricas_finales'):
                metricas.append({**base, **aplanar(d)})
            if ev['etapa'] == '2_transiciones':
                transiciones.extend({**base, 'orden':i, **t} for i,t in enumerate(d, 1))
        guardar_csv(salida/(nombre+'_solicitudes.csv'), solicitudes,
                    ['timestamp_utc','escenario','etapa','sagaId','http','ms','estado','motivo'])
        guardar_csv(salida/(nombre+'_metricas.csv'), metricas,
                    ['timestamp_utc','escenario','etapa'])
        guardar_csv(salida/(nombre+'_transiciones.csv'), transiciones,
                    ['timestamp_utc','escenario','etapa','orden','estado','detalle','timestamp'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('escenario', choices=['1','2','3','todos'])
    parser.add_argument('--gateway', default='http://127.0.0.1:8080')
    parser.add_argument('--acceso-local', help='Solo para pruebas sin Docker: URL interna de Acceso')
    parser.add_argument('--salida', type=Path, default=RAIZ/'resultados')
    args = parser.parse_args()
    reporte = Reporte(args)
    error = None
    try:
        ejecutar(args, reporte)
    except (Exception, KeyboardInterrupt):
        error = traceback.format_exc()
        reporte.mostrar('error', {'detalle': error})
    finally:
        reporte.guardar(error)
    return 1 if error else 0


def ejecutar(args, reporte):
    mostrar = reporte.mostrar
    with httpx.Client(base_url=args.gateway, timeout=45, trust_env=False) as c:
        assert c.get('/health').json()['jwtConfigurado'], 'Configura JWT_SECRET'
        assert c.get('/metrics').status_code == 401
        token = c.post('/auth/token', json={'clienteId': 'demo-'+uuid.uuid4().hex}).json()['access_token']
        c.headers['Authorization'] = 'Bearer '+token
        assert c.post('/admin/falla', json={'modo':'ok'}).status_code == 403

        def falla(modo, delay=0):
            data = {'modo':modo,'delayMs':delay}
            if args.acceso_local:
                httpx.post(args.acceso_local+'/admin/falla', json=data, timeout=5, trust_env=False).raise_for_status()
            else:
                code = ('import urllib.request; r=urllib.request.Request("http://localhost:5003/admin/falla", '
                        'data='+repr(json.dumps(data).encode())+', headers={"Content-Type":"application/json"}); '
                        'print(urllib.request.urlopen(r, timeout=5).read().decode())')
                proceso = subprocess.run(['docker','compose','exec','-T','acceso','python','-c',code], cwd=RAIZ, capture_output=True, text=True)
                if proceso.returncode:
                    raise RuntimeError('No se pudo configurar Acceso: '+proceso.stderr.strip())
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
                reporte.iniciar(1)
                sp=libres(1)[0]; r=reserva(sp); mostrar('1_exito',r)
                assert r['http']==201 and r['estado']=='COMPLETADA'
                x=bff(r['sagaId']); mostrar('1_bff',x)
                assert [x[k]['estado'] for k in ('espacio','pago','acceso')]==['RESERVADA','COBRADO','HABILITADA']
                repeat=reserva(sp,r['sagaId']); assert repeat['http']==201 and repeat['sagaId']==r['sagaId']
                mostrar('1_repeticion_idempotente',repeat)
                dupe=reserva(sp); mostrar('1_doble_reserva',dupe); assert dupe['http']==409
                reporte.aprobar()
            if args.escenario in ('2','todos'):
                reporte.iniciar(2)
                sp=libres(1)[0]; falla('error'); r=reserva(sp); mostrar('2_compensacion',r)
                assert r['http']==503 and r['estado']=='COMPENSADA'
                x=bff(r['sagaId']); mostrar('2_bff_degradado',x)
                assert x['degradacionActiva'] and x['espacio']['estado']=='LIBERADA' and x['pago']['estado']=='REEMBOLSADO'
                falla('ok'); x=bff(r['sagaId']); mostrar('2_bff_recuperado',x); assert x['acceso']['estado']=='REVOCADA'
                trans=saga(r['sagaId'])['transiciones']; mostrar('2_transiciones',trans)
                acciones=[t['detalle'].split()[0] for t in trans if t['detalle'] and ' ok (' in t['detalle']]
                assert acciones==['REVOCAR','REEMBOLSAR','LIBERAR'], acciones
                reporte.aprobar()
            if args.escenario in ('3','todos'):
                reporte.iniciar(3)
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
                mostrar('3_dashboard_despues',{'espacios':len(d['espacios']), 'metricasSistema':d['metricasSistema']})
                mostrar('3_metricas_finales',metrics())
                reporte.aprobar()
        finally:
            falla('ok')
        mostrar('resultado','OK')

if __name__=='__main__':
    raise SystemExit(main())
