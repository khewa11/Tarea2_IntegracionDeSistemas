# Estacionamiento — Integración de Sistemas, Unidad 3 · Forma C

Integración de las ramas main (A: Espacios, Pagos, Acceso), feature-orquestador (B: Saga y resiliencia) y feature-gateway-bff (C: Gateway y BFF). Se conservan las implementaciones y pruebas de los tres integrantes.

## Inicio rápido (Windows PowerShell, Docker Desktop iniciado)

```powershell
Copy-Item .env.example .env
# Editar .env y reemplazar el secreto de demostración; no versionar .env.
docker compose config --quiet
docker compose up --build -d --wait
docker compose ps
Invoke-RestMethod http://127.0.0.1:8080/health
```

Solo el gateway publica puerto (127.0.0.1:8080). Cuatro bases SQLite se almacenan en volúmenes separados. Los otros servicios se comunican por DNS interno de Compose. No usar el compose de desarrollo de A simultáneamente con el integrado.

## Uso del gateway

```powershell
$token = (Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/auth/token -ContentType 'application/json' -Body '{"clienteId":"web-demo"}').access_token
$headers = @{ Authorization = "Bearer $token" }
$body = @{ espacioId='E-001'; patente='ABCD12'; monto=4000; sagaId=('S-'+[guid]::NewGuid().ToString('N')) } | ConvertTo-Json
$r = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/reservas-estacionamiento -Headers $headers -ContentType 'application/json' -Body $body
Invoke-RestMethod -Uri "http://127.0.0.1:8080/bff/mi-reserva/$($r.sagaId)" -Headers $headers
Invoke-RestMethod -Uri http://127.0.0.1:8080/metrics -Headers $headers
```

El endpoint de emisión de JWT es un simulador local para el encargo, no autenticación de usuarios real. No exponer el gateway a Internet. El endpoint interno `/admin/falla` está bloqueado en Gateway; se configura desde el contenedor Acceso. No es necesario exponer puertos internos.

## Pruebas y experimentos

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe scripts/ejecutar_pruebas.py
.venv\Scripts\python.exe scripts/escenarios.py todos
```

Los comandos anteriores guardan los resultados en `resultados/`, sin imprimirlos en la terminal. El código de salida es 0 si todo pasa y distinto de 0 si ocurre un fallo. Los reportes incluyen el error y la evidencia obtenida hasta ese momento. Los archivos de una misma selección se sobrescriben al repetirla; para conservar ejecuciones, usar `--salida resultados/demo-01` (en ambos comandos) o copiar los archivos antes de repetir.

| Archivo | Contenido |
|---|---|
| `pruebas.json` / `pruebas.csv` | Resultado y duración de cada prueba pytest (JSON incluye fases, versiones y errores). |
| `escenarios.json` / `escenarios.csv` | Evidencia completa de los experimentos, incluso si fallan. |
| `escenarios_resumen.csv` | Estado de cada escenario. |
| `escenarios_solicitudes.csv` | HTTP, sagaId, latencia, estado y motivo de cada solicitud de reserva registrada. |
| `escenarios_metricas.csv` | Circuit Breaker, Bulkhead y latencias antes, después y al final. |
| `escenarios_transiciones.csv` | Orden y marcas temporales de las transiciones de compensación. |

Para un escenario individual, los archivos comienzan con `escenario_1`, `escenario_2` o `escenario_3` en lugar de `escenarios`. Los CSV se guardan en UTF-8 con BOM y separador coma; en Excel en español, importar desde Datos > Desde texto/CSV y elegir coma. Los archivos incluidos en `resultados/` son una ejecución real de validación local sin Docker, no evidencia Docker. Los informes en `docs/evidencia_integracion/` se conservan como referencia histórica.

Ejecutar los escenarios en una instancia de demostración limpia con al menos ocho espacios libres y circuito cerrado. El escenario 1 ocupa un espacio; los escenarios 2 y 3 compensan. `todos` corre los tres escenarios consecutivos. También se pueden ejecutar individualmente con `1`, `2` o `3` en lugar de `todos`; si el circuito de una prueba anterior quedó abierto, `docker compose restart orquestador` reinicia sus métricas y circuito, manteniendo su base de datos. No reiniciar durante una saga activa. Las envolturas `.sh` permiten ejecutar desde Git Bash con Python disponible. El script restaura el modo de Acceso a `ok` al finalizar.

- 1: éxito, BFF completo, repetición idempotente y doble reserva rechazada.
- 2: error 502 en Acceso, compensación Revocar → Reembolsar → Liberar, BFF degradado y verificación de la revocación lógica.
- 3: siete solicitudes concurrentes a espacios distintos con Acceso lento (3500 ms), timeout de 2 s, límite 5 en Bulkhead, apertura del circuito, caída simulada, fail-fast y dashboard operativo.

```powershell
docker compose logs --no-color > evidencia-docker.log
docker compose down
```

`docker compose down -v` borra todas las bases de esta instancia: usar solo si son datos desechables de prueba y con autorización del equipo. Para repetir desde cero puede usarse otro nombre de proyecto (variable `COMPOSE_PROJECT_NAME`) después de detener el anterior, sin borrar sus volúmenes.

## Validación realizada

40 pruebas pytest aprobadas y tres escenarios HTTP reales aprobados con seis procesos Uvicorn y bases temporales independientes. Ver `docs/evidencia_integracion/`. No se ejecutó Docker Engine en el entorno de validación: repetir con Docker Desktop y guardar evidencia propia antes de aprobar el PR. Los archivos de Paso 0 y el README original de A (`docs/README_PERSONA_A.md`) son referencias históricas, no las instrucciones del sistema final. El scaffold no se ejecuta en Compose final.

## Integración Git

Ver `docs/INTEGRACION_PR.md` y `scripts/preparar_pr.ps1`. Los ZIP no contienen historial Git; el PR se debe crear en el clon real con las ramas remotas para conservar autoría y commits. No se ha publicado ningún PR remoto.

## Contrato y límites

Consultar `CONTRATO.md` para las rutas. La revocación de Acceso es lógica (simulación), no demuestra una revocación física de hardware. Circuit Breaker y Bulkhead son en memoria, en un proceso del orquestador; las sagas sí se persisten. El gateway valida JWT pero la emisión de tokens es de demostración; no se implementa autorización por dueño de reserva. El rate limit es en memoria. No es una arquitectura de producción.
