"""Shared adapter factory + cache, keyed by endpoint identity.

Kept out of routes/chat.py so both the chat handler and /v1/models resolve
adapters through the same cache: a config apply that changes a provider's
base_url/credentials produces a fresh adapter everywhere.
"""

from __future__ import annotations

from .base import BaseAdapter
from .openai_compat import OpenAICompatAdapter
from .anthropic import AnthropicAdapter
from .gemini import GeminiAdapter


def get_adapter(provider: str, settings, cache: dict) -> BaseAdapter:
    info = settings.endpoint(provider)
    # Cache keyed by the endpoint identity, not just the name: a config apply
    # that changes base_url/credentials must produce a fresh adapter.
    cache_key = (provider, info.base_url, info.api_key, info.type)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    if info.is_openai_compat:
        adapter: BaseAdapter = OpenAICompatAdapter(info.base_url, info.api_key)
    elif info.type == "anthropic":
        adapter = AnthropicAdapter(info.base_url, info.api_key)
    elif info.type == "gemini":
        adapter = GeminiAdapter(info.base_url, info.api_key)
    else:  # pragma: no cover - guarded by settings.endpoint
        raise KeyError(f"unknown provider type: {info.type}")
    if len(cache) > 64:  # bound after hot reloads
        cache.clear()
    cache[cache_key] = adapter
    return adapter
