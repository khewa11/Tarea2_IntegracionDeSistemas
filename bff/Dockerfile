# BFF Web — Persona C. Contexto de build: raíz del repositorio.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY bff/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY bff /app/bff
EXPOSE 3000
CMD ["sh", "-c", "exec uvicorn bff.main:app --host 0.0.0.0 --port ${PORT:-3000} --no-access-log"]