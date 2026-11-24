# Orquestador Saga — Persona B. Contexto de build: raíz del repositorio.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY orquestador/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY orquestador /app/orquestador
RUN rm -rf /app/orquestador/tests && mkdir -p /data
EXPOSE 4000
CMD ["sh", "-c", "exec uvicorn --factory orquestador.main:create_app --host 0.0.0.0 --port ${PORT:-4000} --no-access-log"]
