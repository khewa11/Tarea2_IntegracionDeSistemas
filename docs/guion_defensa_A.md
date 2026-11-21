# Guion sugerido de defensa oral — Persona A

**Apertura (15 segundos).** «Me encargué de implementar los tres servicios de dominio. Espacios reserva y libera, Pagos simula el cobro y reembolso, y Acceso controla una barrera simulada. Cada servicio tiene su propia base de datos SQLite y solo será accesible dentro de la red de Docker».

**Reglas e idempotencia (25 segundos).** «Evité dobles reservas con un índice único parcial que solo permite una reserva activa por espacio. Para cada operación guardé su `sagaId`, su tipo de acción, una huella de los argumentos y la respuesta inicial. Si llega repetida, se reproduce esa respuesta sin volver a cobrar o reservar. Una compensación adelantada también bloquea operaciones atrasadas».

**Evidencia (25 segundos).** «Induje un error en la barrera usando `POST /admin/falla`. Tras la compensación en orden inverso comprobé con GET que la reserva quedó `LIBERADA`, el cobro `REEMBOLSADO` y el acceso `REVOCADA`. También ejecuté 14 pruebas automatizadas y un ensayo HTTP real».

**Cierre (10 segundos).** «Mis servicios proporcionan endpoints idempotentes y estados verificables. El orquestador de B se encarga de los pasos Saga, timeout, Circuit Breaker y Bulkhead; C implementa Gateway, BFF y carga».

## Preguntas frecuentes

**¿Por qué cada servicio tiene su propia base?** Porque Espacios, Pagos y Acceso deben administrar su estado de forma independiente. No se comparte una transacción ACID entre ellos; la Saga coordina compensaciones.

**¿Cómo impides dos reservas en una carrera?** `BEGIN IMMEDIATE` serializa escritores SQLite y un índice único parcial sobre reservas activas hace cumplir la restricción en la base incluso si la aplicación falla al comprobar.

**¿Qué ocurre si se repite el POST de cobro?** Se consulta la tabla `comandos` antes de cambiar el estado. La misma clave y los mismos argumentos reproducen el HTTP original; no se genera otra referencia ni fila de cobro.

**¿Qué ocurre si un POST de habilitación termina después del timeout?** El orquestador puede haber enviado `revocar`. Acceso guarda esa revocación y verifica nuevamente el registro después de cualquier espera simulada: la habilitación tardía recibe `409 SAGA_COMPENSADA`.

**¿Por qué el resultado repetido puede indicar COBRADO si el GET indica REEMBOLSADO?** La respuesta repetida reproduce la primera ejecución de ese *comando*, mientras que el GET muestra el estado *actual* después de una compensación; es lo esperado en idempotencia.

**¿Acceso funciona aunque el controlador esté caído?** `/health` indica que el microservicio está vivo. Las operaciones de habilitación/consulta fallan con 503 cuando el simulador está caído. La revocación registra una intención local, no confirma una acción física externa.

**¿Dónde están las métricas del Circuit Breaker?** Son responsabilidad de Persona B y se integran en su `/metrics`. A aporta la dependencia lenta/caída y logs JSON para evidenciar qué recibió.
