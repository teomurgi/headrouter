"""Adapter base: each adapter speaks OpenAI-format in, OpenAI-format out."""

from __future__ import annotations

import abc
from typing import Any, AsyncIterator

import httpx


class AdapterError(Exception):
    """Upstream provider returned an error."""

    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.body = body


class BaseAdapter(abc.ABC):
    name: str = "base"

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    @abc.abstractmethod
    async def complete(self, client: httpx.AsyncClient, body: dict) -> dict:
        """Send a non-streaming request; return an OpenAI-format response dict."""

    @abc.abstractmethod
    def stream(self, client: httpx.AsyncClient, body: dict) -> AsyncIterator[bytes]:
        """Send a streaming request; yield OpenAI-format SSE chunk bytes."""

    async def models(self, client: httpx.AsyncClient) -> list[str]:
        """List the provider's model ids. Default: unsupported (empty list).

        Used by GET /v1/models for keys holding a '*' wildcard grant, where
        the gateway has no finite local model set to report.
        """
        return []


def error_body(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return resp.text
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("message") or str(data)
    return str(data)
