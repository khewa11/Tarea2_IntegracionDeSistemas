"""Escritura de evidencia en JSON y CSV (UTF-8, compatible con Excel)."""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def ahora():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def guardar_json(ruta, datos):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def guardar_csv(ruta, filas, columnas=()):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    campos = list(dict.fromkeys([*columnas, *(k for fila in filas for k in fila)]))
    with ruta.open('w', encoding='utf-8-sig', newline='') as f:
        escritor = csv.DictWriter(f, fieldnames=campos)
        escritor.writeheader()
        escritor.writerows(filas)


def aplanar(dato, prefijo=''):
    if isinstance(dato, dict):
        resultado = {}
        for k, v in dato.items():
            resultado.update(aplanar(v, f'{prefijo}.{k}' if prefijo else k))
        return resultado
    if isinstance(dato, list):
        dato = json.dumps(dato, ensure_ascii=False)
    return {prefijo or 'valor': dato}
