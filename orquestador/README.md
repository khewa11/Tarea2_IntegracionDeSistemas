# Orquestador Saga — Persona B (pendiente)

Stub de Paso 0: `GET /health` 200; resto **501**. Implementar:

- `POST /reservas-estacionamiento` con `{espacioId, patente, monto}` -> `{sagaId, estado}`.
- `GET /reservas-estacionamiento/{sagaId}` para estado persistente.
- `GET /metrics` para circuito, bulkhead, latencias, sagas.
- Flujo 1 reservar Espacios -> 2 cobrar Pagos -> 3 habilitar Acceso; compensar en orden inverso hasta deshacer todos los pasos confirmados. Si falla o vence el tiempo de habilitación, ejecutar revocación preventiva antes de reembolso y liberación.
- Timeout para toda llamada interna; Circuit Breaker y Bulkhead sobre Acceso.

URLs ya configuradas en las variables de entorno `ESPACIOS_URL`, `PAGOS_URL` y `ACCESO_URL`. Persistir sagas en almacén **propio** separado del de Persona A. `orquestador:4000`, sin puertos públicos.
