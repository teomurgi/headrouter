FROM python:3.12-slim AS base

WORKDIR /app

COPY pyproject.toml ./
COPY app.py config.py config_store.py schemas.py compression_service.py request_log.py ./
COPY adapters/ adapters/
COPY middleware/ middleware/
COPY routes/ routes/
COPY static/ static/

RUN pip install --no-cache-dir .

ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host ${HOST} --port ${PORT}"]
