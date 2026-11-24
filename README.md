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

## Demo web

Con el sistema levantado, abrir **http://127.0.0.1:8080/bff/demo**. La página pide sola un JWT de demostración, lista los espacios, permite reservar (espacio, patente y monto) y muestra el resultado consultando el BFF: estado de la saga, espacio, cobro y barrera, con un aviso visible cuando la degradación está activa y un botón **Refrescar**. Con la pasarela simulada (defecto) el resultado aparece de inmediato. Las fallas de Acceso se siguen induciendo por terminal (ver escenarios), nunca desde la web.

## Probar con Webpay (integración)

Por defecto Pagos usa la pasarela **simulada** (sin internet), para que los escenarios de fallas no dependan de la red. Opcionalmente se puede usar **Transbank Webpay Plus en ambiente de INTEGRACIÓN** (no produce cobros reales; el proyecto no admite producción: `TBK_ENV` solo acepta `integracion`). Se usa el SDK oficial `transbank-sdk` con las credenciales públicas de integración (código de comercio `597055555532`), que el SDK trae incluidas.

Webpay redirige el navegador del usuario de vuelta a `PAGOS_RETURN_URL`, por lo que esa URL debe ser alcanzable desde internet: se usa un túnel hacia el gateway (único puerto publicado).

1. Crear el túnel hacia el gateway (requiere cuenta gratuita de ngrok) y copiar la URL `https://…` que muestra:
   ```
   ngrok http 8080
   ```
2. En `.env` configurar:
   ```
   PASARELA=webpay
   PAGOS_RETURN_URL=https://<subdominio>.ngrok-free.app/pagos/retorno
   PAGOS_TIMEOUT_S=10
   ```
   `TBK_COMMERCE_CODE` y `TBK_API_KEY` pueden quedar vacías (credenciales de integración). `PAGO_TIMEOUT_S` (defecto 300) es el tiempo que la saga espera el pago antes de compensar.
3. Recrear los servicios afectados: `docker compose up --build -d --wait`.
4. Abrir la demo **por la URL del túnel**: `https://<subdominio>.ngrok-free.app/bff/demo` (así el retorno vuelve al mismo origen). Si ngrok muestra su página de aviso, aceptar.
5. Reservar: la saga queda en `PAGO_PENDIENTE` (HTTP 202) y el navegador va al formulario de Webpay. Pagar con una tarjeta de prueba (documentación oficial de Transbank, «Cómo empezar» → *Tarjetas de prueba*):

   | Tarjeta | Número | CVV | Expiración | Resultado |
   |---|---|---|---|---|
   | VISA | 4051 8856 0044 6623 | 123 | cualquiera futura | Aprobada |
   | AMEX | 3700 0000 0002 032 | 1234 | cualquiera futura | Aprobada |
   | MASTERCARD | 5186 0595 5959 0568 | 123 | cualquiera futura | Rechazada |
   | Redcompra (débito) | 4051 8842 3993 7763 | — | — | Aprobada |
   | Redcompra (débito) | 5186 0085 4123 3829 | — | — | Rechazada |

   En la autenticación bancaria usar RUT **11.111.111-1** y clave **123**.
6. Al volver, el gateway reenvía `token_ws` al orquestador, que confirma el pago (commit) y continúa con Acceso; la página de resultado muestra `COMPLETADA` con el código de autorización. Si se rechaza la tarjeta, se anula el pago en Webpay («Anular compra y volver al comercio») o no se paga en `PAGO_TIMEOUT_S`, la saga compensa liberando el espacio (`PAGO_RECHAZADO`, `PAGO_ANULADO`, `PAGO_TIMEOUT`).

Notas:
- `/pagos/retorno` y `/bff/demo` son las **únicas** rutas públicas sin JWT; el resto sigue exigiendo JWT y rate limiting, y `/admin/falla` sigue bloqueado.
- La compensación de un cobro `PENDIENTE` consulta el estado en Webpay: si ya estaba autorizado (por ejemplo, porque se perdió el retorno), se hace `refund` por el total; si no, se marca `ANULADO` y una confirmación posterior se rechaza. **Supuesto no confirmado en la documentación oficial:** la documentación de Webpay Plus no indica qué ocurre con un pago autorizado cuyo commit nunca se ejecuta; si el usuario paga después de vencido `PAGO_TIMEOUT_S`, esa autorización no se confirma ni se reembolsa automáticamente. Revisar en el portal de integración si se prueba ese caso.
- El tiempo de vida del token de Webpay es de 5 minutos según la documentación, coherente con `PAGO_TIMEOUT_S=300`.
- Nunca se registran la API key, el token de pago ni datos de tarjeta.

## Pruebas y experimentos

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts/escenarios.py todos
```

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
