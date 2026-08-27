"""Transparent catch-all proxy.

Any request not handled by a dedicated route (e.g. /v1/chat/completions)
is forwarded verbatim to the resolved provider — same method, path, query,
body and response (including SSE streaming) — with only the base URL and
authentication rewritten. The target provider is resolved from, in order:

1. the `model` field of a JSON request body (closed against the key's grants),
2. the authenticated gateway key's provider binding (single-provider keys only),
3. the default route.
"""

from __future__ import annotations

import json
import logging
import posixpath
import time
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import ModelNotGranted, Settings
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


class _Denied:
    """Sentinel wrapper for a closed-resolution miss; distinct from any provider name."""

    __slots__ = ("exc",)

    def __init__(self, exc):
        self.exc = exc


def _is_scoped_key(binding) -> bool:
    """True for v3 grant-based keys, which must resolve closed (INV-1)."""
    return binding is not None and binding.legacy_provider_grant is None and bool(binding.grants)


def _resolve_route(request: Request, body: bytes, body_uninspected: bool, settings: Settings):
    """Return (provider, target_model), a _Denied wrapper for a closed-resolution
    miss, or None."""
    gateway_key = getattr(request.state, "gateway_key", None)
    binding = settings.key_binding(gateway_key)
    if body:
        try:
            model = json.loads(body).get("model")
        except (json.JSONDecodeError, ValueError):
            model = None
        if isinstance(model, str):
            try:
                route = settings.resolve(model, gateway_key)
            except ModelNotGranted as exc:
                return _Denied(exc)
            if route is not None:
                return route.provider, route.model

    if body_uninspected and _is_scoped_key(binding):
        # A scoped (grant-based) key must have its requested model inspected
        # to enforce INV-1; a body too large/chunked/non-JSON to inspect must
        # not silently fall through to a provider the key wasn't granted.
        return _Denied(ModelNotGranted("<uninspected body>", settings.models_for_key(gateway_key)))

    if binding is not None and binding.provider:
        # Single-provider bindings (legacy grants, single-grant v3 keys) can
        # still route model-less requests; multi-provider keys cannot — they
        # need a 'model' in the body to pick a provider.
        return binding.provider, None
    if settings.default_route is not None:
        return settings.default_route.provider, None
    return None


def _denied_response(exc) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": f"Model '{exc.model}' is not available for this key. "
                f"Available models: {', '.join(exc.available)}. See GET /v1/models.",
                "type": "invalid_request_error",
                "code": "model_not_available_for_key",
            }
        },
        status_code=404,
    )


def _target_url(base_url: str, path: str, is_openai_compat: bool, query: str) -> str:
    # Normalize away '..'/'.' segments so a crafted path cannot escape the
    # credentialed base URL (e.g. '../../admin/secret').
    target = posixpath.normpath("/" + path)
    if target != "/" and path.endswith("/"):
        target += "/"
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
    try:
        content_length_int = int(content_length) if content_length not in (None, "") else None
    except ValueError:
        content_length_int = None  # malformed header: treat as unbounded
    has_body = (
        (content_length_int is not None and content_length_int > 0)
        or "transfer-encoding" in request.headers
    )
    json_content = "json" in (request.headers.get("content-type") or "").lower()
    bounded = content_length_int is not None and content_length_int <= MAX_PEEK_BYTES
    messages_endpoint = request.method == "POST" and path.rstrip("/") == "v1/messages"

    body = b""
    request_stream = None
    body_uninspected = False
    if has_body and json_content and (bounded or messages_endpoint):
        # Native Messages bodies must be inspected regardless of size so they
        # can use the same context-compression path as chat completions.
        body = await request.body()
    elif has_body:
        # Large, chunked, or non-JSON body: stream it through without
        # buffering — the model field cannot be inspected.
        request_stream = request.stream()
        body_uninspected = True

    resolved = _resolve_route(request, body, body_uninspected, settings)
    if isinstance(resolved, _Denied):
        if request_stream is not None:
            await request_stream.aclose()
        return _denied_response(resolved.exc)
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
        compression_result = await compress_request_messages(state, payload, model, logger)

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

    error_preview = bytearray()

    async def stream_bytes() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                if upstream.status_code >= 400 and len(error_preview) < MAX_ERROR_LOG_BYTES:
                    remaining = MAX_ERROR_LOG_BYTES - len(error_preview)
                    error_preview.extend(chunk[:remaining])
                yield chunk
        except Exception:
            # Upstream dropped the connection mid-stream (e.g. httpcore.ReadError) or the
            # client disconnected; the response already started, so just end it here
            # instead of re-raising and surfacing an unhandled-exception traceback.
            logger.exception(
                "proxy response stream failed method=%s path=%s provider=%s status=%s",
                request.method,
                request.url.path,
                provider,
                upstream.status_code,
            )
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
            state.metrics.observe_request(provider, upstream.status_code, time.perf_counter() - started)
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
    raw_headers = []
    for k, v in passthrough_headers:
        try:
            raw_headers.append((k.encode("latin-1"), v.encode("latin-1")))
        except UnicodeEncodeError:
            logger.warning("dropping non-latin-1 upstream header %r", k)
    response.raw_headers = raw_headers
    return response
