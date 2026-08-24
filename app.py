"""Headroom Gateway — a stateless OpenAI-compatible LLM gateway."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from compression import CompressionService
from config import Settings
from middleware import AuthMiddleware, Metrics
from routes import api_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("headroom-gateway")


def create_app(settings: Settings | None = None, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    owns_client = http_client is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if http_client is None:
            app.state.http_client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        else:
            app.state.http_client = http_client
        logger.info(
            "headroom-gateway ready: %d route(s), compression=%s",
            len(settings.routes),
            settings.compression_enabled,
        )
        yield
        if owns_client:
            await app.state.http_client.aclose()

    app = FastAPI(title="Headroom Gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.compression = CompressionService(
        enabled=settings.compression_enabled,
        threshold_tokens=settings.compression_threshold_tokens,
    )
    app.state.metrics = Metrics()

    if settings.api_keys:
        app.add_middleware(AuthMiddleware, api_keys=settings.api_keys)

    app.include_router(api_router)
    return app


app = create_app()
