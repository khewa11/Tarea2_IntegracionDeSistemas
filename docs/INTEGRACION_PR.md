# Integración y pull request

## Base verificada

SHA de los ZIP de GitHub (comentarios de archivo):

- main: `029ebfc6519db1c29ddc0d7d4afd113348998b52`
- feature-orquestador: `a45bd72fe39a29d8dba8961fbf3a046a6894f1b9`
- feature-gateway-bff: `92f832814faceeaaa929d17f3cfdb1d867065e21`

Las ramas B/C no incluyen carpetas de dominio; los archivos del orquestador son idénticos entre B y C. No conocemos la relación de ancestros a partir de los ZIP. Se conserva main para A, se incorporan carpetas reales de C (Gateway y BFF) y B (Orquestador, idéntico al de C), Compose con cuatro volúmenes, contrato ampliado y pruebas de todos. Se añaden `.env.example`, PyJWT en dependencias de pruebas, scripts de escenarios, documentación, evidencia y timeout gateway de 35 segundos para cubrir el flujo de compensaciones ante fallos. Se cuenta el rechazo preventivo del circuito en métricas. No se modifican los modelos ni la lógica de A.

## Procedimiento recomendado

1. Extraer el ZIP en un directorio separado del clon Git real. Nunca ejecutar `git init` dentro del ZIP para sustituir el historial del equipo.
2. Abrir PowerShell en el clon real y ejecutar `git status`. Debe estar limpio. Verificar `git remote -v`.
3. Ejecutar el script `scripts/preparar_pr.ps1` desde el clon, apuntando al directorio extraído. El script hace fetch, verifica que las tres ramas remotas coincidan exactamente con los SHA de los ZIP, crea una nueva rama y prepara una fusión manual con los padres originales. No hace commit ni push. Si cambió algún SHA, se detiene: es necesario revisar los nuevos cambios antes de integrarlos.
4. Revisar `git status`, `git diff --cached --stat`, `git diff --cached`. La estrategia `ours` se usa únicamente para preparar los padres de la fusión sin aceptar eliminaciones masivas; inmediatamente se aplica el árbol reconciliado del ZIP. Nunca confirmar la fusión sin copiar y revisar el resultado. Revisar que los archivos de A, B y C estén presentes.
5. Validar con Docker Desktop, pytest y los tres escenarios según README. El entorno local requiere `.env` ignorado por Git. No agregar secretos ni bases.
6. Confirmar y subir: `git commit -m "merge: integrar servicios de dominio, saga, gateway y BFF"` y `git push -u origin integracion/unidad3-forma-c`.
7. GitHub: New pull request, base `main`, compare `integracion/unidad3-forma-c`. Pedir revisión de B y C. Seleccionar **Create a merge commit**, no squash/rebase, para preservar los commits de cada persona. No fusionar antes de validar.

Si se necesita cancelar antes de confirmar: `git merge --abort` (si hay MERGE_HEAD). Los nuevos archivos no rastreados pueden quedar; revisar `git status` y no borrar archivos personales.
