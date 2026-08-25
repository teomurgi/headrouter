"""Gateway API key authentication for /v1/* endpoints."""

from __future__ import annotations

import logging
from collections.abc import Container

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


PUBLIC_PATHS = {"/", "/health", "/metrics", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/admin"}
logger = logging.getLogger("headrouter.auth")


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_keys: Container[str] | None = None, app_ref=None):
        super().__init__(app)
        # With an app_ref the key set is read from live settings each request,
        # so an atomic config swap (Apply) takes effect without a restart.
        self._app_ref = app_ref
        self.api_keys = api_keys

    def _effective_keys(self) -> Container[str]:
        if self._app_ref is not None:
            return self._app_ref.state.settings.effective_api_keys()
        return self.api_keys or frozenset()

    async def dispatch(self, request: Request, call_next):
        api_keys = self._effective_keys()
        if api_keys and request.url.path not in PUBLIC_PATHS:
            token = self._extract_token(request)
            if token is None or token not in api_keys:
                logger.warning(
                    "authentication error method=%s path=%s status=401 reason=%s client=%s",
                    request.method,
                    request.url.path,
                    "missing_key" if token is None else "invalid_key",
                    request.client.host if request.client else "-",
                )
                return JSONResponse(
                    {
                        "error": {
                            "message": "Missing or invalid API key. "
                            "Provide it via 'Authorization: Bearer <key>' or 'x-api-key'.",
                            "type": "authentication_error",
                            "code": "invalid_api_key",
                        }
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.gateway_key = token
        return await call_next(request)

    @staticmethod
    def _extract_token(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        xkey = request.headers.get("x-api-key")
        if xkey:
            return xkey.strip()
        return None
