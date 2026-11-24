# Proyecto de Integración de Sistemas — Unidad 3 · Forma C
## Entrega completa de Persona A: Espacios, Pagos y Acceso

Esta carpeta contiene **la Parte A implementada y el Paso 0 preparado para todo el equipo**. Los tres microservicios A tienen persistencia por servicio en SQLite, API HTTP, idempotencia, compensaciones y simulador de barrera. Se añaden carpetas y Dockerfiles de inicio de B/C, pero **gateway, BFF y orquestador todavía son stubs y no implementan sus responsabilidades funcionales**.

**Para arrancar el esqueleto compartido con un único Compose y preparar el acuerdo del equipo, comienza en [`README_PASO_0.md`](README_PASO_0.md). El detalle técnico está en [`CONTRATO.md`](CONTRATO.md).**

## 1. Estructura

```text
Persona_A_Estacionamiento/
├── gateway/, bff/, orquestador/ # Stubs para B/C (pendientes)
├── scaffold/                 # Servidor HTTP mínimo compartido para stubs
├── common/                   # Infraestructura y modelos compartidos
├── espacios/                 # Servicio y Dockerfile
├── pagos/                    # Servicio y Dockerfile
├── acceso/                   # Servicio y Dockerfile
├── tests/                    # Pruebas automatizadas
├── scripts/demo_persona_a.py # Escenarios A sin orquestador
├── examples/                 # Colección Postman
├── docs/                     # Informe y resultados de las pruebas
├── docker-compose.yml        # Paso 0: 6 servicios, solo gateway público
├── README_PASO_0.md          # Cómo iniciar y coordinar el equipo
├── compose.persona-a.yml     # 3 contenedores internos, sin puertos públicos
├── compose.persona-a.dev.yml # Puertos SOLO en localhost para probar A
├── CONTRATO.md               # Acuerdo de rutas / semántica para B y C
├── requirements.txt
└── requirements-dev.txt
```

**Tecnologías:** Python 3.12 en Docker, FastAPI, SQLite3 + WAL, transacciones SQLite con `BEGIN IMMEDIATE`, Pydantic, Uvicorn, pytest.

## 2. Ejecutar con Docker (recomendado)

Desde esta carpeta:

```bash
docker compose -f compose.persona-a.yml -f compose.persona-a.dev.yml up --build -d
docker compose -f compose.persona-a.yml -f compose.persona-a.dev.yml ps
```

El segundo Compose es **solo para la Persona A en desarrollo local**: publica puertos ligados a `127.0.0.1`, permitiendo Postman desde el computador. El Compose compartido `docker-compose.yml` **NO debe incluir** `compose.persona-a.dev.yml` y **solo el gateway** debe tener puertos publicados. El Compose de Persona A se mantiene para pruebas directas antes de integrar B/C.

URLs para desarrollar: `http://127.0.0.1:5001`, `http://127.0.0.1:5002`, `http://127.0.0.1:5003`. Documentación interactiva FastAPI (local): `/docs` en cada puerto.

Para apagar (conservando las bases):

```bash
docker compose -f compose.persona-a.yml -f compose.persona-a.dev.yml down
```

**Atención:** `down -v` también elimina los volúmenes y todos los datos de prueba.

## 3. Ejecutar sin Docker (alternativa)

Instala Python 3.11+ y desde esta carpeta:

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux/macOS:
# source .venv/bin/activate
pip install -r requirements-dev.txt
```

Abre **tres terminales independientes**. En PowerShell, desde la raíz del proyecto:

```powershell
$env:DB_PATH = "./data/espacios.db"; python -m uvicorn espacios.main:app --port 5001
$env:DB_PATH = "./data/pagos.db"; python -m uvicorn pagos.main:app --port 5002
$env:DB_PATH = "./data/acceso.db"; python -m uvicorn acceso.main:app --port 5003
```

Ejecuta **una línea en cada terminal**, no las tres en la misma. Las tres rutas SQLite deben ser distintas. En producción los contenedores usan `/data/espacios.db`, `/data/pagos.db`, `/data/acceso.db`, respectivamente.

## 4. Pruebas automáticas

```bash
python -m pytest tests/ -v
```

Incluyen: exclusión mutua en reservas simultáneas, respuesta idempotente, reutilización indebida de `sagaId`, reembolso único, restricción de montos, modos de falla `ok/lento/caido/error`, compensación anticipada, habilitación tardía tras revocar, camino feliz y falla en el paso final con compensación. Se ejecutan **sin red ni Docker**, con SQLite de prueba separado para cada servicio.

Para repetir una demostración vía HTTP (tener los tres servicios levantados):

```bash
python scripts/demo_persona_a.py
```

El script prepara un camino feliz, otro con falla de Acceso, revoca/reembolsa/libera y consulta los estados persistidos. Imprime los códigos HTTP y `sagaId` usados. También restaura Acceso a modo `ok`.

## 5. Uso de Postman / curl

Importa `examples/Persona_A.postman_collection.json` en Postman y modifica variables de entorno si es necesario. Ejemplos de uso local:

```bash
# A. Reserva
curl -i -X POST http://127.0.0.1:5001/reservas \
  -H "Content-Type: application/json" \
  -d '{"sagaId":"demo-100","espacioId":"E-001"}'

# B. Cobro simulado
curl -i -X POST http://127.0.0.1:5002/cobros \
  -H "Content-Type: application/json" \
  -d '{"sagaId":"demo-100","monto":4000}'

# C. Habilitación
curl -i -X POST http://127.0.0.1:5003/habilitaciones \
  -H "Content-Type: application/json" \
  -d '{"sagaId":"demo-100","patente":"ABCD12","espacioId":"E-001"}'

# Consultar el estado actual
curl http://127.0.0.1:5001/reservas/demo-100
curl http://127.0.0.1:5002/cobros/demo-100
curl http://127.0.0.1:5003/habilitaciones/demo-100
```

**Nueva prueba = nuevo `sagaId`**. Para probar `409 ESPACIO_OCUPADO`, utiliza otro `sagaId` pero el mismo espacio mientras sigue reservado.

### Forzar fallo en el tercer paso

```bash
curl -X POST http://127.0.0.1:5003/admin/falla \
  -H "Content-Type: application/json" -d '{"modo":"error","delayMs":0}'
# Repetir una nueva habilitación con sagaId nuevo: devuelve 502.
# Compensar siempre en orden inverso:
curl -X POST http://127.0.0.1:5003/habilitaciones/demo-100/revocar
curl -X POST http://127.0.0.1:5002/cobros/demo-100/reembolso
curl -X POST http://127.0.0.1:5001/reservas/demo-100/liberar
curl -X POST http://127.0.0.1:5003/admin/falla \
  -H "Content-Type: application/json" -d '{"modo":"ok","delayMs":0}'
```

### Forzar lentitud y caída de la barrera

```bash
curl -X POST http://127.0.0.1:5003/admin/falla \
  -H "Content-Type: application/json" -d '{"modo":"lento","delayMs":2500}'
# Una NUEVA habilitación demorará ~2,5 s; los timeouts/CB/Bulkhead los implementa Persona B.
curl -X POST http://127.0.0.1:5003/admin/falla \
  -H "Content-Type: application/json" -d '{"modo":"caido","delayMs":0}'
# Una NUEVA habilitación devolverá 503; el BFF de C podrá observar la falla en GET.
curl -X POST http://127.0.0.1:5003/admin/falla \
  -H "Content-Type: application/json" -d '{"modo":"ok","delayMs":0}'
```

## 6. Modelo de datos y decisiones

**Espacios:** catálogo `espacios(id)`, `reservas(saga_id PK, espacio_id FK, estado, timestamps)` y `comandos(saga_id,accion PK compuesta,huella,codigo,respuesta,creado_en)`. Índice único parcial `una_reserva_activa_por_espacio` impide dos `RESERVADA` simultáneas sobre el mismo espacio, incluso frente a carreras concurrentes.

**Pagos:** `cobros(saga_id PK,monto_centavos,moneda,estado,referencia_pasarela,timestamps)` y el mismo esquema local `comandos`. Simula pasarela: genera un cobro lógico sin contactar una entidad financiera. Una repetición de `POST /cobros` no duplica el cobro ni crea una referencia nueva.

**Acceso:** `habilitaciones(saga_id PK,patente,espacio_id,estado,timestamps)`, `configuracion_falla(modo,delay_ms)` y `comandos`. Su base registra la habilitación/revocación y la configuración de inestabilidad sin reiniciar el proceso.

**Idempotencia:** la combinación `(sagaId,acción)` se guarda junto con una huella de los parámetros y la respuesta HTTP inicial en la misma transacción que el efecto. Un comando repetido usa el resultado almacenado; la misma clave con parámetros distintos produce 409. Las compensaciones guardan también un marcador cuando llegan antes de la operación original, evitando **efectos tardíos** por timeouts.

**Límites de la simulación:** los reembolsos y revocaciones son lógicos en SQLite. En producción, una pasarela externa debe reconocer su propia clave de idempotencia, y la barrera real requeriría reconciliación si el hardware queda inaccesible. El simulador no finge haber ejecutado operaciones físicas de un dispositivo caído.

## 7. Integración con B y C

El repositorio ya incluye `docker-compose.yml` raíz para los seis servicios y sus carpetas de responsabilidad. **No** copies los mapeos `ports` del Compose de desarrollo al Compose compartido. B/C deben **sustituir** sus stubs y Dockerfiles antes de considerar el sistema finalizado; las rutas se detallan en `CONTRATO.md`.

- Persona B deberá manejar Saga y conservar su estado; ante fallo de último paso: `POST /habilitaciones/{sagaId}/revocar`, `POST /cobros/{sagaId}/reembolso`, `POST /reservas/{sagaId}/liberar` (es seguro repetir cualquiera).
- Persona B implementará timeout, circuit breaker, bulkhead y fallbacks (esto **no** está implementado en A, pues se le asignó a B).
- Persona C es responsable del gateway público, JWT HS256, rate limiting, agregación del BFF y experimentos de carga.
- Para el informe grupal (máximo 8 páginas), se entrega una sección lista para copiar sobre Persona A, sin atribuirle resultados de experimentos no ejecutados.

## 8. Observabilidad, evidencias y limitaciones de ejecución

Cada acción escribe una sola línea JSON en stdout con `sagaId`, `servicio`, `paso` y `resultado`; se pueden capturar con `docker compose logs -f espacios pagos acceso` al integrar el equipo.

Las pruebas automáticas sí se ejecutaron en el entorno de preparación y se entregan sus resultados en `docs/resultado_pruebas.txt`. **Docker Compose no pudo ejecutarse aquí porque este entorno no tiene la CLI de Docker**: verifica `docker compose up --build` y graba capturas en tu PC antes de la defensa. No se incluyen métricas ni capturas ficticias del Circuit Breaker o Bulkhead, ya que corresponden a Persona B/C.
