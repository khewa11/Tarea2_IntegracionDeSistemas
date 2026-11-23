# BFF web — Persona C (pendiente)

Stub de Paso 0: `GET /health` 200; resto **501**. Debe implementarse `GET /bff/mi-reserva/{sagaId}`, consultando **dos o más servicios** (idealmente los tres: `espacios`, `pagos`, `acceso`), con timeout propio en cada consulta y campo de Acceso **no disponible** cuando haya fallas. Puerto interno `bff:3000`, sin `ports` públicos. Los clientes entran por el gateway.
