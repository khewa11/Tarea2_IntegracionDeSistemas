# Orquestador Saga — Persona B

Saga orquestada: reservar espacio → cobrar → habilitar acceso, con compensación en
orden inverso. Incluye timeout, Circuit Breaker, Bulkhead sobre Acceso, persistencia
propia en SQLite, recuperación de sagas a medias, `/metrics` y logs JSON.
Contrato HTTP y estados: `CONTRATO.md` §4.

| Archivo | Contenido |
|---|---|
| `main.py` | Rutas FastAPI, códigos HTTP y bucle de recuperación |
| `saga.py` | Pasos, compensaciones y recuperación |
| `resiliencia.py` | `CircuitBreaker` y `Bulkhead` |
| `clientes.py` | Llamadas HTTP con timeout y métricas por dependencia |
| `db.py` | Tablas `sagas` y `transiciones` |
| `tests/` | Pruebas con dobles de Espacios/Pagos/Acceso (no requieren el código de A) |

## Pruebas

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r orquestador/requirements.txt pytest anyio
.venv/bin/python -m pytest orquestador/tests -q
```

## Demostración con el sistema completo (cuando estén los servicios de A)

```bash
docker compose up --build -d
# Desde dentro de la red (el orquestador no publica puerto):
docker compose exec orquestador python -c "import httpx;print(httpx.post('http://localhost:4000/reservas-estacionamiento',json={'espacioId':'E-001','patente':'ABCD12','monto':4000}).text)"
# Inducir fallas en Acceso:
docker compose exec orquestador python -c "import httpx;print(httpx.post('http://acceso:5003/admin/falla',json={'modo':'caido'}).text)"
docker compose exec orquestador python -c "import httpx;print(httpx.get('http://localhost:4000/metrics').text)"
docker compose logs -f orquestador
```

| Escenario | Cómo provocarlo | Resultado esperado |
|---|---|---|
| Camino feliz | Acceso `ok` | `201 COMPLETADA` |
| Compensación | Acceso `error` o `caido` | `503 COMPENSADA`; GET en A: `LIBERADA`, `REEMBOLSADO`, `REVOCADA` |
| Timeout | Acceso `lento`, `delayMs` 2500 | `COMPENSADA`, motivo `ACCESO_TIMEOUT`, en unos 2 s |
| Circuit Breaker | 3 fallos seguidos | `/metrics` → `ABIERTO`; siguiente POST: `503 FALLIDA CIRCUITO_ABIERTO_ACCESO` al instante |
| Bulkhead | Acceso `lento` 1500 y más de 5 POST simultáneos | Excedentes `COMPENSADA` `BULKHEAD_LLENO_ACCESO`; `bulkhead.rechazos` sube |
