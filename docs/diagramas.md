# Diagramas de Persona A para informe / defensa

## Contexto de servicios y bases de datos

```mermaid
flowchart LR
    B[Orquestador Saga - Persona B]
    E[Espacios :5001] --> DE[(SQLite Espacios)]
    P[Pagos :5002] --> DP[(SQLite Pagos)]
    A[Acceso :5003] --> DA[(SQLite Acceso)]
    B --> E
    B --> P
    B --> A
    C[Gateway / BFF - Persona C] -. consultas .-> E
    C -. consultas .-> P
    C -. consultas .-> A
```

El orquestador controla la secuencia; cada microservicio conserva su estado local. No existe una transacción ACID global entre ellos.

## Camino feliz (secuencia de las llamadas que recibirá A)

```mermaid
sequenceDiagram
    participant B as Orquestador (B)
    participant E as Espacios (A)
    participant P as Pagos (A)
    participant A as Acceso (A)
    B->>E: POST /reservas {sagaId, espacioId}
    E-->>B: 201 RESERVADA
    B->>P: POST /cobros {sagaId, monto}
    P-->>B: 201 COBRADO
    B->>A: POST /habilitaciones {sagaId, patente, espacioId}
    A-->>B: 201 HABILITADA
```

## Error inducido en la barrera y compensaciones

```mermaid
sequenceDiagram
    participant B as Orquestador (B)
    participant E as Espacios (A)
    participant P as Pagos (A)
    participant A as Acceso (A)
    B->>E: POST /reservas
    E-->>B: 201 RESERVADA
    B->>P: POST /cobros
    P-->>B: 201 COBRADO
    B->>A: POST /habilitaciones
    A-->>B: 502/503 o timeout
    Note over B: Compensación inversa, definida por Persona B
    B->>A: POST /habilitaciones/{sagaId}/revocar
    A-->>B: 200 REVOCADA
    B->>P: POST /cobros/{sagaId}/reembolso
    P-->>B: 200 REEMBOLSADO
    B->>E: POST /reservas/{sagaId}/liberar
    E-->>B: 200 LIBERADA
```

## Cómo la idempotencia detiene efectos tardíos

```mermaid
sequenceDiagram
    participant B as Orquestador
    participant A as Acceso
    B->>A: POST /habilitaciones (lento)
    Note over A: Espera simulada delayMs
    Note over B: Timeout de la llamada
    B->>A: POST /habilitaciones/{sagaId}/revocar
    A-->>B: 200 REVOCADA (tombstone)
    Note over A: Termina espera de la petición original
    A-->>B: 409 SAGA_COMPENSADA (no habilita)
```

Nota: con una barrera real que pudiera habilitar después de una caída, habría que reconciliar estados y confirmar revocaciones remotas; el ejemplo garantiza coherencia dentro del simulador.
