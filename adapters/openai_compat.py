"""OpenAI-compatible adapter: works for OpenAI, OpenRouter, Ollama and any compatible endpoint."""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from .base import AdapterError, BaseAdapter, error_body


class OpenAICompatAdapter(BaseAdapter):
    name = "openai-compat"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def complete(self, client: httpx.AsyncClient, body: dict) -> dict:
        payload = {**body, "stream": False}
        resp = await client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers(),
        )
        if resp.status_code >= 400:
            raise AdapterError(resp.status_code, error_body(resp), safe_json(resp))
        return resp.json()

    async def stream(self, client: httpx.AsyncClient, body: dict) -> AsyncIterator[bytes]:
        payload = {**body, "stream": True}
        async with client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers(),
        ) as resp:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode("utf-8", "replace")
                raise AdapterError(resp.status_code, text)
            async for chunk in resp.aiter_bytes():
                yield chunk

    async def models(self, client: httpx.AsyncClient) -> list[str]:
        resp = await client.get(f"{self.base_url}/models", headers=self._headers())
        if resp.status_code >= 400:
            raise AdapterError(resp.status_code, error_body(resp), safe_json(resp))
        data = resp.json()
        return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]


def safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except Exception:
        return None
