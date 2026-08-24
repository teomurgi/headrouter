"""Gateway API key authentication for /v1/* endpoints."""

from __future__ import annotations

import logging
from collections.abc import Container

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


PUBLIC_PATHS = {"/", "/health", "/metrics", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
logger = logging.getLogger("headrouter.auth")


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_keys: Container[str]):
        super().__init__(app)
        self.api_keys = api_keys

    async def dispatch(self, request: Request, call_next):
        if self.api_keys and request.url.path not in PUBLIC_PATHS:
            token = self._extract_token(request)
            if token is None or token not in self.api_keys:
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
