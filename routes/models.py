"""GET /v1/models — list the models available to the authenticated key (INV-2)."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request

from adapters import get_adapter

logger = logging.getLogger("headrouter.models")

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request):
    state = request.app.state
    settings = state.settings
    gateway_key = getattr(request.state, "gateway_key", None)

    ids = set(settings.models_for_key(gateway_key))

    # A scoped key holding a "*" wildcard grant has no finite local model set;
    # fetch the granting providers' live catalogs and merge them in.
    wildcard = [p for p in settings.wildcard_providers(gateway_key) if p != "*"]
    if wildcard:
        ids.discard("*")
        adapter_cache = getattr(state, "adapter_cache", None)
        if adapter_cache is None:
            adapter_cache = state.adapter_cache = {}
        client = getattr(state, "http_client", None)
        if client is None:
            client = state.http_client = httpx.AsyncClient(
                timeout=settings.request_timeout_seconds
            )
        for provider in wildcard:
            try:
                adapter = get_adapter(provider, settings, adapter_cache)
                ids.update(await adapter.models(client))
            except Exception as exc:  # provider down/unreachable — degrade gracefully
                logger.warning("model list fetch failed provider=%s error=%s", provider, exc)

    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": "headrouter"}
            for mid in sorted(ids)
        ],
    }
