# Diagramas de arquitectura — Paso 0 (Forma C)

## Entrada, dependencias y persistencia

```mermaid
flowchart LR
  X[Cliente] --> G[gateway:8080\nC · único público]
  G --> O[orquestador:4000\nB · Saga]
  G --> B[bff:3000\nC · agregación]
  O --> E[espacios:5001\nA]
  O --> P[pagos:5002\nA]
  O --> A[acceso:5003\nA · barrera externa simulada]
  B --> E
  B --> P
  B --> A
  E --> DE[(SQLite E)]
  P --> DP[(SQLite P)]
  A --> DA[(SQLite A)]
```

*Las conexiones son el **diseño objetivo**. En el Compose de Paso 0, gateway/BFF/orquestador son stubs y todavía no ejecutan esas llamadas.*

## Camino feliz (responsable de la secuencia: B)

```mermaid
sequenceDiagram
  actor Cliente
  participant Gateway
  participant Orquestador
  participant Espacios
  participant Pagos
  participant Acceso
  Cliente->>Gateway: POST /reservas-estacionamiento
  Gateway->>Orquestador: Enrutar solicitud validada
  Orquestador->>Espacios: POST /reservas {sagaId,espacioId}
  Espacios-->>Orquestador: 201 RESERVADA
  Orquestador->>Pagos: POST /cobros {sagaId,monto}
  Pagos-->>Orquestador: 201 COBRADO
  Orquestador->>Acceso: POST /habilitaciones {sagaId,patente,espacioId}
  Acceso-->>Orquestador: 201 HABILITADA
  Orquestador-->>Gateway: {sagaId,estado}
  Gateway-->>Cliente: Resultado
```

## Falla en la última dependencia con compensación inversa

```mermaid
sequenceDiagram
  participant Orquestador
  participant Espacios
  participant Pagos
  participant Acceso
  Orquestador->>Espacios: Reservar
  Espacios-->>Orquestador: RESERVADA
  Orquestador->>Pagos: Cobrar
  Pagos-->>Orquestador: COBRADO
  Orquestador->>Acceso: Habilitar
  Acceso--xOrquestador: 502 / 503 / timeout
  Note over Orquestador: Revocación preventiva (si existe una operación tardía)
  Orquestador->>Acceso: POST /habilitaciones/{sagaId}/revocar
  Acceso-->>Orquestador: REVOCADA (lógica)
  Orquestador->>Pagos: POST /cobros/{sagaId}/reembolso
  Pagos-->>Orquestador: REEMBOLSADO
  Orquestador->>Espacios: POST /reservas/{sagaId}/liberar
  Espacios-->>Orquestador: LIBERADA
```

**Limitación:** son diagramas del comportamiento esperado una vez integrado el equipo. Los servicios de A implementan las operaciones; el flujo automático de Saga y los patrones CB/Bulkhead corresponden a B.
