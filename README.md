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

Por defecto Pagos usa la pasarela **simulada** (sin internet), para que los escenarios de fallas no dependan de la red. Esta sección explica cómo simular un pago real con **Transbank Webpay Plus en ambiente de INTEGRACIÓN**: no se cobra dinero real y el proyecto no admite producción (`TBK_ENV` solo acepta `integracion`). Se usa el SDK oficial `transbank-sdk` con las credenciales públicas de integración (código de comercio `597055555532`), que el SDK ya trae, así que no hay que configurar credenciales.

Webpay devuelve al usuario a `PAGOS_RETURN_URL` cuando termina de pagar. Para que esa dirección sea pública se usa **ngrok**, un túnel hacia el gateway (el único puerto publicado, 8080).

### Preparación (una sola vez)

1. Crear una cuenta gratuita en [ngrok.com](https://ngrok.com).
2. Instalar ngrok:
   - **Windows / macOS / Linux:** descargar desde [ngrok.com/download](https://ngrok.com/download), descomprimir y dejar el ejecutable en una carpeta del `PATH`.
   - **Arch / CachyOS:** `paru -S ngrok` (paquete AUR).

   Comprobar con `ngrok version`.
3. Registrar el token de la cuenta (panel de ngrok → **Your Authtoken**):
   ```
   ngrok config add-authtoken <TU_TOKEN_DE_NGROK>
   ```
4. Recomendado: en el panel de ngrok → **Domains**, revisar el dominio fijo gratuito de la cuenta (por ejemplo `nombre-al-azar.ngrok-free.dev`). Con un dominio fijo, `PAGOS_RETURN_URL` se configura una sola vez.

### Pasos

1. Levantar el sistema normalmente (`docker compose up --build -d --wait`).
2. En **otra terminal**, abrir el túnel hacia el gateway y dejarlo abierto:
   ```
   ngrok http 8080
   ```
   o, con el dominio fijo de la cuenta: `ngrok http --url=<tu-dominio>.ngrok-free.dev 8080` (en versiones antiguas de ngrok, `--domain=`).

   Copiar la URL `https://…` de la línea **Forwarding**, por ejemplo `https://abcd-1234.ngrok-free.app`. También se puede leer desde la API local de ngrok: `curl -s http://127.0.0.1:4040/api/tunnels`.

   Para comprobar que el túnel llega al gateway:
   ```
   curl -H "ngrok-skip-browser-warning: 1" https://abcd-1234.ngrok-free.app/health
   ```
   Debe responder `{"servicio":"gateway","estado":"ok",...}`. La cabecera evita la página de aviso de ngrok, que solo aparece en navegadores.
3. Agregar al final de `.env` (reemplazando la URL por la copiada):
   ```
   PASARELA=webpay
   PAGOS_RETURN_URL=https://abcd-1234.ngrok-free.app/pagos/retorno
   PAGOS_TIMEOUT_S=10
   ```
4. Reiniciar solo los servicios que cambian:
   ```
   docker compose up -d --wait pagos orquestador
   ```
   Para comprobar el modo: en `docker compose logs pagos`, la última línea con `"paso":"arranque"` debe decir `"pasarela":"WEBPAY_PLUS_INTEGRACION"`.
5. Abrir la demo **por la URL de ngrok**: `https://abcd-1234.ngrok-free.app/bff/demo`. La primera vez ngrok muestra una página de aviso: pulsar **Visit Site**.
6. Elegir espacio, patente y monto (entero, por ejemplo `4000`) y pulsar **Reservar**. El navegador va al formulario de Webpay.

### Casos para simular el pago

Tarjetas de prueba de la documentación oficial de Transbank («Cómo empezar» → *Tarjetas de prueba*). Cuando Webpay pida autenticación bancaria, usar RUT **11.111.111-1** y clave **123**.

| Caso | Qué hacer en Webpay | Resultado esperado en la demo |
|---|---|---|
| Pago aprobado | VISA `4051 8856 0044 6623`, CVV `123`, cualquier fecha futura | `COMPLETADA`; cobro `COBRADO` con código de autorización; barrera `HABILITADA` |
| Pago rechazado | MASTERCARD `5186 0595 5959 0568`, CVV `123`, cualquier fecha futura | `COMPENSADA` (`PAGO_RECHAZADO`); cobro `RECHAZADO`; espacio `LIBERADA` |
| Usuario anula | Pulsar «Anular compra y volver al comercio» | `COMPENSADA` (`PAGO_ANULADO`); cobro `ANULADO`; espacio `LIBERADA` |
| No paga a tiempo | Cerrar la pestaña de Webpay sin pagar y esperar `PAGO_TIMEOUT_S` (300 s por defecto; se revisa cada 15 s) | Consultar el sagaId en la demo: `COMPENSADA` (`PAGO_TIMEOUT`); espacio `LIBERADA` |

Otras tarjetas válidas: AMEX `3700 0000 0002 032` (CVV `1234`, aprobada) y Redcompra débito `4051 8842 3993 7763` (aprobada) o `5186 0085 4123 3829` (rechazada).

Mientras ngrok corre, **http://127.0.0.1:4040** muestra cada petición que pasa por el túnel, incluido el retorno de Webpay a `/pagos/retorno` con `token_ws` (pago) o `TBK_TOKEN` (anulación). Sirve para depurar y para mostrar el flujo en una demostración.

Para ver los pasos de la saga mientras se paga:
```
docker compose logs -f orquestador pagos gateway
```
Se ven `saga.transicion PAGO_PENDIENTE`, `RETORNO_PAGO`, `pagos.confirmar` y la continuación hacia Acceso o la compensación.

Para que el caso de timeout sea más rápido en una demostración, agregar `PAGO_TIMEOUT_S=60` en `.env` y repetir el paso 4.

### Volver al modo simulado

1. Borrar del `.env` las líneas `PASARELA`, `PAGOS_RETURN_URL` y `PAGOS_TIMEOUT_S` (o poner `PASARELA=simulada`).
2. `docker compose up -d --wait pagos orquestador`
3. Cerrar ngrok con `Ctrl+C`.

Hay que volver a la simulada antes de correr `scripts/escenarios.py`, porque esos escenarios esperan el cobro inmediato y no dependen de internet.

### Problemas comunes

- **La URL de ngrok cambió:** en el plan gratuito cambia cada vez que se reinicia ngrok. Actualizar `PAGOS_RETURN_URL` y repetir el paso 4.
- **Al volver de Webpay aparece la página de aviso de ngrok:** pulsar **Visit Site**; el pago se procesa igual.
- **La reserva queda `COMPENSADA` con motivo `MONTO_CLP_ENTERO`:** Webpay en CLP no acepta decimales; usar un monto entero.
- **La reserva responde `503` con motivo `PAGOS_TIMEOUT` o `PAGOS_HTTP_5XX_…`:** Pagos no pudo contactar a Webpay (sin internet o Transbank lento). Revisar `docker compose logs pagos`.
- **El espacio no aparece libre:** quedó ocupado por una reserva anterior; elegir otro.

### Notas técnicas

- `/pagos/retorno` y `/bff/demo` son las **únicas** rutas públicas sin JWT; el resto sigue exigiendo JWT y rate limiting, y `/admin/falla` sigue bloqueado.
- La compensación de un cobro `PENDIENTE` consulta el estado en Webpay: si ya estaba autorizado (por ejemplo, porque se perdió el retorno), se hace `refund` por el total; si no, se marca `ANULADO` y una confirmación posterior se rechaza. **Supuesto no confirmado en la documentación oficial:** la documentación de Webpay Plus no indica qué ocurre con un pago autorizado cuyo commit nunca se ejecuta; si el usuario paga después de vencido `PAGO_TIMEOUT_S`, esa autorización no se confirma ni se reembolsa automáticamente. Revisar en el portal de integración si se prueba ese caso.
- El token de Webpay dura 5 minutos según la documentación, coherente con `PAGO_TIMEOUT_S=300`.
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

**Rama `feature/webpay-demo` (pasarela adaptable, Webpay Plus de integración y demo web):** 76 pruebas pytest aprobadas con `PASARELA=simulada` (40 previas y 36 nuevas, con dobles del SDK de Transbank y sin red). En Docker Compose, con la pasarela simulada, se recorrió la demo por el gateway y se aprobaron los escenarios 1, 2 y 3 de `scripts/escenarios.py`; la base de Pagos existente se migró al arrancar. Con `PASARELA=webpay` y un túnel ngrok se creó una transacción real en el ambiente de integración de Transbank (cobro `PENDIENTE`, HTTP 202) y se anuló por `/pagos/retorno` (saga `COMPENSADA` con `PAGO_ANULADO`, espacio liberado y cobro `ANULADO`). El pago desde el navegador con las tarjetas de prueba se verificó manualmente.

**Integración inicial de las tres ramas:** 40 pruebas pytest aprobadas y tres escenarios HTTP reales aprobados con seis procesos Uvicorn y bases temporales independientes. Ver `docs/evidencia_integracion/`. No se ejecutó Docker Engine en el entorno de validación: repetir con Docker Desktop y guardar evidencia propia antes de aprobar el PR. Los archivos de Paso 0 y el README original de A (`docs/README_PERSONA_A.md`) son referencias históricas, no las instrucciones del sistema final. El scaffold no se ejecuta en Compose final.

## Integración Git

Ver `docs/INTEGRACION_PR.md` y `scripts/preparar_pr.ps1`. Los ZIP no contienen historial Git; el PR se debe crear en el clon real con las ramas remotas para conservar autoría y commits. No se ha publicado ningún PR remoto.

## Contrato y límites

Consultar `CONTRATO.md` para las rutas. La revocación de Acceso es lógica (simulación), no demuestra una revocación física de hardware. Circuit Breaker y Bulkhead son en memoria, en un proceso del orquestador; las sagas sí se persisten. El gateway valida JWT pero la emisión de tokens es de demostración; no se implementa autorización por dueño de reserva. El rate limit es en memoria. No es una arquitectura de producción.
