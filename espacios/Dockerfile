FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY common /app/common
ARG SERVICE
COPY ${SERVICE} /app/${SERVICE}
RUN mkdir /data && chown -R 10001:10001 /data /app
USER 10001:10001
ENV DB_PATH=/data/service.db
CMD ["sh", "-c", "exec uvicorn ${SERVICE}.main:app --host 0.0.0.0 --port ${PORT}"]
