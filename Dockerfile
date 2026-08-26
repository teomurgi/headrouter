FROM python:3.12-slim AS base

WORKDIR /app

COPY pyproject.toml ./
COPY app.py config.py config_store.py schemas.py compression_service.py request_log.py gateway_launcher.py tray_app.py ./
COPY adapters/ adapters/
COPY middleware/ middleware/
COPY routes/ routes/
COPY static/ static/

RUN pip install --no-cache-dir . \
    && useradd --system --create-home --uid 1000 headrouter \
    && chown -R headrouter:headrouter /app
USER headrouter

ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=2)" || exit 1

CMD ["sh", "-c", "uvicorn app:app --host ${HOST} --port ${PORT}"]
