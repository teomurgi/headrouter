FROM python:3.12-slim AS base

WORKDIR /app

COPY pyproject.toml ./
COPY app.py config.py schemas.py compression.py ./
COPY adapters/ adapters/
COPY middleware/ middleware/
COPY routes/ routes/

RUN pip install --no-cache-dir .

# Optional: enable headroom compression engine
# RUN pip install --no-cache-dir "headroom-ai>=0.36"

ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host ${HOST} --port ${PORT}"]
