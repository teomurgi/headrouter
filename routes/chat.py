"""POST /v1/chat/completions — validate, compress, route, forward, stream."""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from adapters import AdapterError, BaseAdapter
from adapters.openai_compat import OpenAICompatAdapter
from adapters.anthropic import AnthropicAdapter
from adapters.gemini import GeminiAdapter
from config import ModelNotGranted, Settings
from schemas import ChatCompletionRequest
from .request_compression import compress_request_messages

logger = logging.getLogger("headrouter.chat")

router = APIRouter()

_ADAPTER_CACHE: dict[str, BaseAdapter] = {}


def get_adapter(provider: str, settings: Settings) -> BaseAdapter:
    if provider in _ADAPTER_CACHE:
        return _ADAPTER_CACHE[provider]
    info = settings.endpoint(provider)
    if info.is_openai_compat:
        adapter: BaseAdapter = OpenAICompatAdapter(info.base_url, info.api_key)
    elif info.type == "anthropic":
        adapter = AnthropicAdapter(info.base_url, info.api_key)
    elif info.type == "gemini":
        adapter = GeminiAdapter(info.base_url, info.api_key)
    else:  # pragma: no cover - guarded by settings.endpoint
        raise KeyError(f"unknown provider type: {info.type}")
    _ADAPTER_CACHE[provider] = adapter
    return adapter


def _error(status: int, message: str, err_type: str, code: str | None = None) -> JSONResponse:
    err = {"message": message, "type": err_type}
    if code:
        err["code"] = code
    return JSONResponse({"error": err}, status_code=status)


@router.post("/chat/completions")
@router.post("/v1/chat/completions")
async def chat_completions(payload: ChatCompletionRequest, request: Request):
    state = request.app.state
    settings: Settings = state.settings

    gateway_key = getattr(request.state, "gateway_key", None)
    try:
        route = settings.resolve(payload.model, gateway_key)
    except ModelNotGranted as exc:
        logger.warning(
            "chat routing error method=%s path=%s model=%s status=404 code=model_not_granted",
            request.method, request.url.path, payload.model,
        )
        return _error(
            404,
            f"Model '{exc.model}' is not available for this key. "
            f"Available models: {', '.join(exc.available)}. See GET /v1/models.",
            "invalid_request_error",
            "model_not_available_for_key",
        )
    if route is None:
        logger.warning(
            "chat routing error method=%s path=%s model=%s status=404 code=model_not_found",
            request.method,
            request.url.path,
            payload.model,
        )
        return _error(
            404,
            f"The model `{payload.model}` does not exist or is not routed. "
            f"Configure it via GATEWAY_ROUTES.",
            "invalid_request_error",
            "model_not_found",
        )

    try:
        adapter = get_adapter(route.provider, settings)
    except KeyError:
        logger.warning(
            "chat routing error method=%s path=%s provider=%s status=404 code=provider_not_found",
            request.method,
            request.url.path,
            route.provider,
        )
        return _error(404, f"Unknown provider `{route.provider}`.", "invalid_request_error", "provider_not_found")

    body = payload.model_dump(exclude_none=True)
    body["model"] = route.model
    body.pop("n", None)

    compression_result = compress_request_messages(state, body, route.model, logger)

    started = time.perf_counter()

    if payload.stream:
        return StreamingResponse(
            adapter.stream(state.http_client, body),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Compression-Applied": str(compression_result.applied).lower(),
            },
        )

    try:
        result = await adapter.complete(state.http_client, body)
    except AdapterError as exc:
        state.metrics.observe_request(route.provider, exc.status_code, time.perf_counter() - started)
        logger.warning(
            "chat upstream error method=%s path=%s provider=%s model=%s status=%s body=%r",
            request.method,
            request.url.path,
            route.provider,
            route.model,
            exc.status_code,
            exc.message,
        )
        return _error(exc.status_code, exc.message, "upstream_error")
    except Exception as exc:  # connection failures etc.
        state.metrics.observe_request(route.provider, 502, time.perf_counter() - started)
        logger.exception(
            "chat request failed method=%s path=%s provider=%s model=%s status=502 error=%s",
            request.method,
            request.url.path,
            route.provider,
            route.model,
            exc,
        )
        return _error(502, f"Upstream request failed: {exc}", "upstream_error")

    state.metrics.observe_request(route.provider, 200, time.perf_counter() - started)
    usage = result.get("usage") or {}
    state.metrics.observe_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

    headers = {"X-Compression-Applied": str(compression_result.applied).lower()}
    return JSONResponse(result, headers=headers)
