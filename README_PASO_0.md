> Referencia histórica del Paso 0. Para el sistema integrado, usar [README.md](README.md).

# Paso 0 — Contrato común y repositorio inicial

Este entregable corresponde al **Paso 0** del documento «Separación de responsabilidades — Forma C». 

## Qué está efectivamente preparado

1. **`CONTRATO.md`**: puertos, DNS Docker, servicios responsables, JWT HS256, idempotencia por `sagaId`, logs JSON, elección de Saga orquestada, rutas, errores y compensaciones. 
2. **`docker-compose.yml` raíz**: levanta 6 contenedores con `docker compose up --build`. **Solo `gateway:8080` publica puerto** (atado a `127.0.0.1` para evitar exposición de desarrollo accidental). Espacios, Pagos y Acceso tienen volúmenes independientes SQLite y healthchecks.
3. **Carpetas y Dockerfiles**: `gateway/`, `bff/`, `orquestador/`, `espacios/`, `pagos/`, `acceso/`. Los tres primeros contienen **stubs mínimos** con GET `/health` que responde 200 y **501 para todas las rutas de negocio**. 
4. **`.env.example`**: plantilla para secreto HS256. Crear un `.env` local con secreto aleatorio; `.gitignore` lo excluye de Git.
5. **`docs/ARQUITECTURA_PASO_0.md`**: diagramas de exposición, saga y dependencias.

## Comandos (Windows 11, PowerShell / Docker Desktop)

Desde la carpeta de este repositorio:

```powershell
Copy-Item .env.example .env
# Edita .env y reemplaza JWT_SECRET por un secreto aleatorio, por ejemplo:
python -c "import secrets; print(secrets.token_urlsafe(48))"
docker compose up --build -d
docker compose ps
```

**Nota:** el placeholder `gateway` **NO** valida JWT ni enruta solicitudes; responde `501` a toda operación de negocio. No es apto para producción ni cumple todavía los requisitos funcionales de C. El requisito `docker compose up` solo queda completamente satisfecho cuando B y C reemplacen sus placeholders.

Comprobar el stub público:

```powershell
curl.exe -i http://127.0.0.1:8080/health
curl.exe -i http://127.0.0.1:8080/reservas-estacionamiento
```

El primero devuelve `200 {"estado":"stub","implementado":false,...}`; el segundo, `501`. 

Revisar logs y finalizar:

```powershell
docker compose logs --tail 20 acceso pagos espacios gateway bff orquestador
docker compose down
```

`down -v` borra las bases de datos del entorno de prueba. Evitarlo salvo que se quiera reiniciar datos.

## Probar el trabajo de A sin pasar por el gateway (solo desarrollo)

La configuración principal **no** expone sus puertos. Para probar los 3 servicios con Postman desde el host:

```powershell
docker compose -f compose.persona-a.yml -f compose.persona-a.dev.yml up --build -d
python scripts/demo_persona_a.py
```

## Integración y Git

```powershell
git add .
git commit -m "chore: contrato comun y esqueleto docker compose paso 0"
```