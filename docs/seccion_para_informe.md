# Persona A — Microservicios de dominio: Espacios, Pagos y Acceso

## Alcance y arquitectura
Se implementaron tres servicios HTTP independientes en FastAPI, cada uno con su propio archivo de base de datos SQLite persistido en un volumen Docker distinto. Espacios gestiona la disponibilidad y reserva, Pagos simula una pasarela de cobro y devolución, y Acceso simula un controlador externo de barrera con modos de falla configurables (`ok`, `lento`, `caido`, `error`). La Persona B conserva la responsabilidad de ejecutar y compensar la Saga; la Persona C es responsable de la autenticación, gateway y BFF. Los servicios A son internos y no deben exponerse públicamente en el Compose final.

## Modelo de datos

| Servicio | Tablas principales | Restricciones y garantías |
| --- | --- | --- |
| Espacios | `espacios(id PK)`; `reservas(saga_id PK, espacio_id FK, estado, creado_en, actualizado_en)`; `comandos(saga_id, accion, huella, codigo, respuesta)` | `UNIQUE INDEX` parcial sobre `reservas(espacio_id)` para filas `estado='RESERVADA'`; evita doble ocupación incluso ante concurrencia. |
| Pagos | `cobros(saga_id PK, monto_centavos, moneda, estado, referencia_pasarela, timestamps)`; `comandos` | Montos positivos guardados como centavos enteros. `saga_id` y referencia de pasarela son únicos, impidiendo cobros duplicados. |
| Acceso | `habilitaciones(saga_id PK, patente, espacio_id, estado, timestamps)`; `configuracion_falla(modo, delay_ms)`; `comandos` | Habilitaciones/revocaciones por Saga; `configuracion_falla` mantiene el modo actual sin reiniciar. |

Cada base usa transacciones SQLite con `BEGIN IMMEDIATE`, claves primarias, restricciones e índice de unicidad donde corresponde; se emplea WAL para lecturas mientras se realizan escrituras.

## Reglas de negocio e idempotencia

1. Un espacio no puede mantenerse reservado por dos `sagaId` simultáneos. La segunda operación se rechaza con HTTP `409 ESPACIO_OCUPADO`.
2. Los comandos de cada servicio se identifican por `(sagaId, acción)` y guardan una huella de sus argumentos junto con el código HTTP y la respuesta. Ante una repetición se retorna la misma respuesta sin repetir el efecto; si se reutiliza el identificador con argumentos distintos se devuelve HTTP `409 IDEMPOTENCY_KEY_REUSE`.
3. Las compensaciones (`liberar`, `reembolsar`, `revocar`) son idempotentes. Se registra un marcador persistente aunque lleguen antes de su operación inicial. Esto impide que una solicitud retrasada por timeout reactive la reserva, el cobro o la barrera después de una compensación.
4. Los pagos son ficticios: no se contacta una pasarela real. Una revocación con el controlador simulado caído registra la intención lógica local; no significa confirmar una acción física en hardware real.

## Simulación de fallas

El endpoint `POST /admin/falla` de Acceso recibe `{modo, delayMs}`. `lento` introduce una espera asíncrona configurable, `caido` retorna HTTP 503, `error` retorna HTTP 502 y `ok` restaura el funcionamiento. Se aplican a habilitación y consulta; la ruta de revocación se mantiene disponible para registrar la orden lógica de compensación. Esto permite a la Persona B inducir las fallas exigidas y demostrar el Circuit Breaker y Bulkhead que le corresponden.

## Validación y evidencia

Se ejecutaron **14 pruebas automatizadas (14 aprobadas)**, que cubren concurrencia, doble reserva, idempotencia, reembolsos únicos, validación de montos, persistencia tras reinicio, los cuatro modos de falla, prevención de habilitación tardía, camino feliz y compensación. Además, una prueba HTTP real con los tres servicios levantados confirmó: reserva 201, cobro 201, habilitación 201; y, al inducir error en Acceso, estado final `LIBERADA` en Espacios, `REEMBOLSADO` en Pagos y `REVOCADA` en Acceso. Los registros de ambas ejecuciones se adjuntan en `docs/resultado_pruebas.txt` y `docs/resultado_demo_http.txt`.

**Limitaciones de evidencia:** la prueba HTTP fue ejecutada localmente y no con Docker; Docker Compose aún debe verificarse en el equipo. Los gráficos del Circuit Breaker, rechazos del Bulkhead, rate limiting y BFF no se presentan como realizados, ya que corresponden a las Personas B y C. El informe grupal deberá añadir capturas y métricas reales después de integrar el sistema completo.
