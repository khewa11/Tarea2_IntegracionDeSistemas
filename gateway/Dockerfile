# Dockerfile provisional del Paso 0. Reemplazar por la implementación final del integrante.
FROM python:3.12-alpine
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY scaffold/stub.py /app/stub.py
RUN addgroup -S app && adduser -S -G app app
USER app
CMD ["python", "-u", "/app/stub.py"]
