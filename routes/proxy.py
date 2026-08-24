"""Transparent catch-all proxy.

Any request not handled by a dedicated route (e.g. /v1/chat/completions)
is forwarded verbatim to the resolved provider — same method, path, query,
body and response (including SSE streaming) — with only the base URL and
authentication rewritten. The target provider is resolved from, in order:

1. the `model` field of a JSON request body,
2. the authenticated gateway key's provider binding,
3. the default route.
"""

from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import Settings

logger = logging.getLogger("headrouter.proxy")

router = APIRouter()

HOP_BY_HOP = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "authorization",
    "x-api-key",
    "x-goog-api-key",
    "accept-encoding",
}

RESPONSE_HEADERS_TO_DROP = {"content-length", "transfer-encoding", "connection", "content-encoding"}


def _resolve_route(request: Request, body: bytes, settings: Settings):
    """Return (provider, target_model) or None."""
    gateway_key = getattr(request.state, "gateway_key", None)
    if body:
        try:
            model = json.loads(body).get("model")
            if isinstance(model, str):
                route = settings.resolve(model, gateway_key)
                if route is not None:
                    return route.provider, route.model
        except Exception:
            pass
    binding = settings.key_binding(gateway_key)
    if binding is not None:
        return binding.provider, None
    if settings.default_route is not None:
        return settings.default_route.provider, None
    return None


def _target_url(base_url: str, path: str, is_openai_compat: bool, query: str) -> str:
    target = "/" + path
    # OpenAI-compatible base URLs already include the /v1 prefix; avoid /v1/v1/...
    if is_openai_compat and (target == "/v1" or target.startswith("/v1/")):
        target = target[3:]
    url = base_url + target
    if query:
        url += "?" + query
    return url


def _upstream_headers(request: Request, provider_type: str, api_key: str) -> dict[str, str]:
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
    }
    if api_key:
        if provider_type == "anthropic":
            headers["x-api-key"] = api_key
            headers.setdefault("anthropic-version", "2023-06-01")
        elif provider_type == "gemini":
            headers["x-goog-api-key"] = api_key
        else:
            headers["authorization"] = f"Bearer {api_key}"
    return headers


@router.get("/")
async def root():
    return {"service": "headrouter", "docs": "/docs"}


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy_all(path: str, request: Request):
    state = request.app.state
    settings: Settings = state.settings

    body = b""
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()

    resolved = _resolve_route(request, body, settings)
    if resolved is None:
        return JSONResponse(
            {
                "error": {
                    "message": "No provider could be resolved for this request. "
                    "Provide a 'model' in the body, bind your API key to a provider, "
                    "or set GATEWAY_DEFAULT_ROUTE.",
                    "type": "invalid_request_error",
                    "code": "no_provider",
                }
            },
            status_code=404,
        )
    provider, target_model = resolved

    if target_model and body:
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                payload["model"] = target_model
                body = json.dumps(payload).encode()
        except Exception:
            pass

    try:
        info = settings.endpoint(provider)
    except KeyError:
        return JSONResponse(
            {"error": {"message": f"Unknown provider `{provider}`.", "type": "invalid_request_error"}},
            status_code=404,
        )

    url = _target_url(info.base_url, path, info.is_openai_compat, request.url.query)
    headers = _upstream_headers(request, info.type, info.api_key)

    started = time.perf_counter()
    upstream_request = state.http_client.build_request(
        request.method,
        url,
        content=body or None,
        headers=headers,
    )
    try:
        upstream = await state.http_client.send(upstream_request, stream=True)
    except Exception as exc:
        state.metrics.observe_request(provider, 502, time.perf_counter() - started)
        logger.warning("proxy upstream %s failed: %s", provider, exc)
        return JSONResponse(
            {"error": {"message": f"Upstream request failed: {exc}", "type": "upstream_error"}},
            status_code=502,
        )

    state.metrics.observe_request(provider, upstream.status_code, time.perf_counter() - started)

    async def stream_bytes() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    passthrough_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in RESPONSE_HEADERS_TO_DROP
    }
    return StreamingResponse(
        stream_bytes(),
        status_code=upstream.status_code,
        headers=passthrough_headers,
        background=None,
    )
