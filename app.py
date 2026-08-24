"""Headrouter — a stateless OpenAI-compatible LLM gateway."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from compression import CompressionService
from config import Settings
from middleware import AuthMiddleware, Metrics
from routes import api_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("headrouter")


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
            "headrouter ready: %d route(s), compression=%s strategy=%s",
            len(settings.routes),
            settings.compression_enabled,
            settings.compression_strategy,
        )
        yield
        if owns_client:
            await app.state.http_client.aclose()

    app = FastAPI(title="Headrouter", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.compression = CompressionService(
        enabled=settings.compression_enabled,
        threshold_tokens=settings.compression_threshold_tokens,
        strategy=settings.compression_strategy,
    )
    app.state.metrics = Metrics()

    if settings.effective_api_keys():
        app.add_middleware(AuthMiddleware, api_keys=settings.effective_api_keys())

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [
            {"type": error["type"], "loc": error["loc"], "msg": error["msg"]}
            for error in exc.errors()
        ]
        logger.warning(
            "request validation error method=%s path=%s status=422 details=%r",
            request.method,
            request.url.path,
            details,
        )
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"detail": exc.errors()}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        logger.warning(
            "http error method=%s path=%s status=%s detail=%r",
            request.method,
            request.url.path,
            exc.status_code,
            exc.detail,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    app.include_router(api_router)
    return app


app = create_app()
