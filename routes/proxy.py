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
from .request_compression import compress_request_messages

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

# Bodies up to this size may be buffered for JSON model inspection/rewriting;
# anything larger is streamed through without buffering.
MAX_PEEK_BYTES = 64 * 1024
MAX_ERROR_LOG_BYTES = 16 * 1024


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
    url = base_url.rstrip("/") + target
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

    # A body may accompany any method (DELETE with a body is legal); detect
    # its presence via headers rather than assuming method semantics.
    content_length = request.headers.get("content-length")
    has_body = (
        (content_length is not None and content_length != "" and int(content_length) > 0)
        or "transfer-encoding" in request.headers
    )
    json_content = "json" in (request.headers.get("content-type") or "").lower()
    bounded = content_length is not None and int(content_length) <= MAX_PEEK_BYTES
    messages_endpoint = request.method == "POST" and path.rstrip("/") == "v1/messages"

    body = b""
    request_stream = None
    if has_body and json_content and (bounded or messages_endpoint):
        # Native Messages bodies must be inspected regardless of size so they
        # can use the same context-compression path as chat completions.
        body = await request.body()
    elif has_body:
        # Large or unbounded body: stream it through without buffering.
        request_stream = request.stream()

    resolved = _resolve_route(request, body, settings)
    if resolved is None:
        if request_stream is not None:
            await request_stream.aclose()
        logger.warning(
            "proxy routing error method=%s path=%s status=404 code=no_provider",
            request.method,
            request.url.path,
        )
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

    payload = None
    if body:
        try:
            payload = json.loads(body)
            if target_model and isinstance(payload, dict):
                payload["model"] = target_model
        except Exception:
            payload = None

    try:
        info = settings.endpoint(provider)
    except KeyError:
        if request_stream is not None:
            await request_stream.aclose()
        logger.warning(
            "proxy routing error method=%s path=%s provider=%s status=404 code=unknown_provider",
            request.method,
            request.url.path,
            provider,
        )
        return JSONResponse(
            {"error": {"message": f"Unknown provider `{provider}`.", "type": "invalid_request_error"}},
            status_code=404,
        )

    compression_result = None
    if (
        messages_endpoint
        and info.type == "anthropic"
        and isinstance(payload, dict)
        and isinstance(payload.get("messages"), list)
    ):
        model = payload.get("model") if isinstance(payload.get("model"), str) else ""
        compression_result = compress_request_messages(state, payload, model, logger)

    if payload is not None:
        body = json.dumps(payload).encode()

    url = _target_url(info.base_url, path, info.is_openai_compat, request.url.query)
    headers = _upstream_headers(request, info.type, info.api_key)

    started = time.perf_counter()
    upstream_request = state.http_client.build_request(
        request.method,
        url,
        content=body if body else request_stream,
        headers=headers,
    )
    try:
        upstream = await state.http_client.send(upstream_request, stream=True)
    except Exception as exc:
        state.metrics.observe_request(provider, 502, time.perf_counter() - started)
        logger.exception(
            "proxy request failed method=%s path=%s provider=%s status=502 error=%s",
            request.method,
            request.url.path,
            provider,
            exc,
        )
        return JSONResponse(
            {"error": {"message": f"Upstream request failed: {exc}", "type": "upstream_error"}},
            status_code=502,
        )

    state.metrics.observe_request(provider, upstream.status_code, time.perf_counter() - started)

    error_preview = bytearray()

    async def stream_bytes() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                if upstream.status_code >= 400 and len(error_preview) < MAX_ERROR_LOG_BYTES:
                    remaining = MAX_ERROR_LOG_BYTES - len(error_preview)
                    error_preview.extend(chunk[:remaining])
                yield chunk
        except Exception:
            logger.exception(
                "proxy response stream failed method=%s path=%s provider=%s status=%s",
                request.method,
                request.url.path,
                provider,
                upstream.status_code,
            )
            raise
        finally:
            if upstream.status_code >= 400:
                request_id = upstream.headers.get("x-request-id") or upstream.headers.get("request-id")
                preview = error_preview.decode("utf-8", "replace")
                if len(error_preview) == MAX_ERROR_LOG_BYTES:
                    preview += "... [truncated]"
                logger.warning(
                    "proxy upstream error method=%s path=%s provider=%s status=%s "
                    "request_id=%s body=%r",
                    request.method,
                    request.url.path,
                    provider,
                    upstream.status_code,
                    request_id or "-",
                    preview,
                )
            # Also closes the upstream cleanly if the client disconnects mid-stream.
            await upstream.aclose()

    passthrough_headers = [
        (k, v)
        for k, v in upstream.headers.multi_items()
        if k.lower() not in RESPONSE_HEADERS_TO_DROP
        and (compression_result is None or k.lower() != "x-compression-applied")
    ]
    if compression_result is not None:
        passthrough_headers.append(
            ("x-compression-applied", str(compression_result.applied).lower())
        )
    response = StreamingResponse(
        stream_bytes(),
        status_code=upstream.status_code,
        background=None,
    )
    # Assign raw headers so repeated fields (e.g. Set-Cookie) stay distinct.
    response.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1")) for k, v in passthrough_headers
    ]
    return response
