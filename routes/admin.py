"""Admin surface (§5): config get/validate/apply, request log, provider health.

All /admin/* endpoints inherit gateway auth (AuthMiddleware) — gateway keys
only, no separate login. The API sends/accepts env-var names, never secret
values (INV-5).
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import ConfigError

router = APIRouter()

HEALTH_CACHE_TTL_SECONDS = 30.0


def _store(request: Request):
    store = getattr(request.app.state, "config_store", None)
    if store is None:
        raise RuntimeError("config store not configured on this app")
    return store


def _reject_secrets(data: dict) -> list[str]:
    errors = []
    for p in data.get("providers", []):
        if isinstance(p, dict) and p.get("api_key"):
            errors.append(
                f"provider '{p.get('name', '?')}': secret 'api_key' values are not accepted; "
                "use 'api_key_env'"
            )
    for k in data.get("keys", []):
        if isinstance(k, dict) and k.get("api_key"):
            errors.append(
                f"key '{k.get('name', '?')}': secret 'api_key' values are not accepted; "
                "use 'api_key_env'"
            )
    return errors


@router.get("/admin/config")
async def get_config(request: Request):
    return _store(request).sanitized_config()


@router.post("/admin/config/validate")
async def validate_staged(request: Request):
    data = await request.json()
    from config import validate_config

    errors = _reject_secrets(data) + validate_config(data)
    return {"valid": not errors, "errors": errors}


@router.put("/admin/config")
async def apply_config(request: Request):
    store = _store(request)
    data = await request.json()
    errors = _reject_secrets(data) + store.validate(data)
    if errors:
        return JSONResponse({"detail": {"errors": errors}}, status_code=400)
    try:
        await store.apply_async(data)
    except ConfigError as exc:
        return JSONResponse({"detail": {"errors": [str(exc)]}}, status_code=400)
    return {"applied": True, "migrated_keys": sorted(store.migrated_keys)}


@router.get("/admin/log")
async def get_log(request: Request, after: int = 0):
    log = request.app.state.request_log
    return {"entries": log.since(after), "last_seq": log.last_seq}


@router.get("/admin/health/providers")
async def provider_health(request: Request):
    state = request.app.state
    settings = state.settings
    cache = getattr(state, "_provider_health_cache", None)
    now = time.time()
    if cache is not None and now - cache["checked_at"] < HEALTH_CACHE_TTL_SECONDS:
        return cache["body"]

    providers = []
    client: httpx.AsyncClient = state.http_client
    for name in sorted(settings.custom_providers):
        info = settings.custom_providers[name]
        reachable, detail = await _probe(client, info.base_url, info.type, info.api_key)
        providers.append({"name": name, "type": info.type, "base_url": info.base_url,
                          "reachable": reachable, "detail": detail})

    body = {"checked_at": now, "providers": providers}
    state._provider_health_cache = {"checked_at": now, "body": body}
    return body


async def _probe(client: httpx.AsyncClient, base_url: str, ptype: str, api_key: str):
    """Cheap reachability probe per provider type; never raises."""
    headers = {}
    if api_key:
        if ptype == "anthropic":
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        elif ptype == "gemini":
            headers = {"x-goog-api-key": api_key}
        else:
            headers = {"authorization": f"Bearer {api_key}"}
    try:
        r = await client.get(base_url.rstrip("/") + "/models", headers=headers)
        # 401/403 still proves reachability; only transport errors don't.
        return True, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)[:200]
