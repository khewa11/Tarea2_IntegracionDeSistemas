# Gateway — Persona C (pendiente)

Actualmente solo hay un **stub HTTP de Paso 0**: `GET /health` devuelve 200 con `implementado:false` y cualquier otra ruta responde **501**. Por tanto **todavía no hay autenticación JWT, rate limiting ni proxy**. Reemplazar `Dockerfile` por la aplicación de C.

Contrato: `gateway:8080`, entrada única; validar Bearer JWT HS256 con `JWT_SECRET` cargado desde `.env` antes de permitir rutas protegidas. Enrutar al BFF (`bff:3000`) y al orquestador (`orquestador:4000`), sin lógica de negocio. Nunca publicar puertos de servicios internos ni exponer `/admin/falla` a clientes finales sin una restricción de administración.
