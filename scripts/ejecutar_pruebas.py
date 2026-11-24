"""Ejecuta pytest y exporta evidencia individual, sin imprimir resultados."""
import argparse
import contextlib
import io
import os
import platform
import time
from collections import Counter
from importlib.metadata import version
from pathlib import Path

import pytest
from reportes import RAIZ, ahora, guardar_json, guardar_csv


class Reporte:
    def __init__(self):
        self.casos = {}
        self.errores_coleccion = []

    def pytest_collection_finish(self, session):
        for item in session.items:
            self.casos[item.nodeid] = []

    def pytest_collectreport(self, report):
        if report.failed:
            self.errores_coleccion.append({'archivo': report.nodeid, 'error': report.longreprtext})

    def pytest_runtest_logreport(self, report):
        fase = {'fase': report.when, 'resultado': report.outcome,
                'duracion_s': round(report.duration, 6)}
        if report.failed or report.skipped:
            fase['detalle'] = report.longreprtext
        if hasattr(report, 'wasxfail'):
            fase['xfail'] = report.wasxfail
        self.casos.setdefault(report.nodeid, []).append(fase)

    def filas(self):
        for nombre, fases in self.casos.items():
            call = next((f for f in fases if f['fase'] == 'call'), {})
            fallos = [f for f in fases if f['resultado'] == 'failed']
            if fallos:
                estado = 'ERROR' if any(f['fase'] != 'call' for f in fallos) else 'FALLIDA'
            elif any(f.get('xfail') for f in fases):
                estado = 'XFAIL' if any(f['resultado'] == 'skipped' for f in fases) else 'XPASS'
            elif any(f['resultado'] == 'skipped' for f in fases):
                estado = 'OMITIDA'
            elif call.get('resultado') == 'passed' and any(f['fase'] == 'teardown' for f in fases):
                estado = 'APROBADA'
            else:
                estado = 'NO_EJECUTADA'
            yield {'prueba': nombre, 'archivo': nombre.split('::')[0],
                   'estado': estado, 'duracion_ms': round(sum(f['duracion_s'] for f in fases)*1000, 3),
                   'detalle': '\n'.join(f['detalle'] for f in fases if f.get('detalle')),
                   'fases': fases}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--salida', type=Path, default=RAIZ/'resultados')
    args = p.parse_args()
    salida = args.salida.resolve()
    os.chdir(RAIZ)
    reporte = Reporte()
    inicio = ahora()
    reloj = time.perf_counter()
    captura = io.StringIO()
    with contextlib.redirect_stdout(captura), contextlib.redirect_stderr(captura):
        codigo = int(pytest.main(['-q', '--color=no'], plugins=[reporte]))
    filas = list(reporte.filas())
    conteo = Counter(f['estado'] for f in filas)
    datos = {'inicio_utc': inicio, 'fin_utc': ahora(), 'duracion_s': round(time.perf_counter()-reloj, 3),
             'python': platform.python_version(), 'dependencias': {n: version(n) for n in ('pytest','fastapi','pydantic','httpx','PyJWT','PyYAML')},
             'codigo_salida': codigo, 'total': len(filas), 'resumen': dict(conteo),
             'errores_coleccion': reporte.errores_coleccion, 'pruebas': filas,
             'salida_pytest': captura.getvalue()}
    guardar_json(salida/'pruebas.json', datos)
    guardar_csv(salida/'pruebas.csv', [{k:v for k,v in f.items() if k != 'fases'} for f in filas],
                ['prueba','archivo','estado','duracion_ms','detalle'])
    return codigo


if __name__ == '__main__':
    raise SystemExit(main())
