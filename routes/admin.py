"""Admin surface (§5): config get/validate/apply, request log, provider health.

All /admin/* endpoints inherit gateway auth (AuthMiddleware) — gateway keys
only, no separate login — and additionally require an ADMIN key (INV-9):
GATEWAY_API_KEYS members; scoped config keys get 403 regardless of grants.
The API sends/accepts env-var names, never secret values (INV-5).
"""

from __future__ import annotations

import asyncio
import secrets
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from config import ConfigError

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

HEALTH_CACHE_TTL_SECONDS = 30.0
_health_cache_lock = asyncio.Lock()

_build_id_cache: str | None = None


async def require_admin_key(request: Request) -> None:
    """INV-9: the admin surface is for admin keys only, enforced in one place."""
    settings = request.app.state.settings
    token = getattr(request.state, "gateway_key", None)
    is_admin = token is not None and any(
        secrets.compare_digest(token, key) for key in settings.api_keys
    )
    if not is_admin:
        raise HTTPException(
            status_code=403,
            detail={"error": "admin key required",
                    "hint": "this key is valid but lacks admin access; use a GATEWAY_API_KEYS value"},
        )


ADMIN = [Depends(require_admin_key)]


def _store(request: Request):
    store = getattr(request.app.state, "config_store", None)
    if store is None:
        raise RuntimeError("config store not configured on this app")
    return store


def _reject_secrets(data: dict) -> list[str]:
    errors = []
    keys = data.get("keys", []) if isinstance(data, dict) else []
    if not isinstance(keys, list):
        keys = []
    for k in keys:
        if isinstance(k, dict) and k.get("api_key"):
            errors.append(
                f"key '{k.get('name', '?')}': secret 'api_key' values are not accepted; "
                "use 'api_key_env'"
            )
    return errors


@router.get("/admin", include_in_schema=False)
async def admin_page():
    build = _build_id()
    html = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    html = html.replace(">build ?</span>", f">{build}</span>")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/help", include_in_schema=False)
async def help_page():
    html = (STATIC_DIR / "help.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/static/{filename}", include_in_schema=False)
async def static_file(filename: str):
    """Serve bundled static assets (icons etc.) with a long cache lifetime."""
    path = (STATIC_DIR / filename).resolve()
    if not path.is_file() or path.parent != STATIC_DIR:
        raise HTTPException(status_code=404, detail={"error": "not found"})
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


def _build_id() -> str:
    global _build_id_cache
    if _build_id_cache is None:
        try:
            _build_id_cache = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=STATIC_DIR.parent, text=True,
            ).strip() or "dev"
        except Exception:
            _build_id_cache = "dev"
    return _build_id_cache


@router.get("/admin/config", dependencies=ADMIN)
async def get_config(request: Request):
    return _store(request).sanitized_config()


@router.post("/admin/config/validate", dependencies=ADMIN)
async def validate_staged(request: Request):
    data = await request.json()
    from config import validate_config

    errors = _reject_secrets(data) + validate_config(data)
    return {"valid": not errors, "errors": errors}


@router.put("/admin/config", dependencies=ADMIN)
async def apply_config(request: Request):
    store = _store(request)
    data = await request.json()
    errors = _reject_secrets(data) + store.validate(data)
    if errors:
        return JSONResponse({"detail": {"errors": errors}}, status_code=400)
    try:
        _, generated = await store.apply_async(data)
    except ConfigError as exc:
        return JSONResponse({"detail": {"errors": [str(exc)]}}, status_code=400)
    except OSError as exc:
        # persistence failed after validation: name the file and reason so
        # this is diagnosable from the browser (running settings untouched)
        return JSONResponse(
            {"detail": {"errors": [
                f"cannot write config file {store.path}: {exc.strerror or exc} — "
                "applied nowhere; check permissions/owner of the file and its directory"
            ]}},
            status_code=500,
        )
    return {"applied": True, "migrated_keys": sorted(store.migrated_keys),
            "generated": generated}


@router.get("/admin/log", dependencies=ADMIN)
async def get_log(request: Request, after: int = 0):
    log = request.app.state.request_log
    return {"entries": log.since(after), "last_seq": log.last_seq}


@router.get("/admin/health/providers", dependencies=ADMIN)
async def provider_health(request: Request):
    state = request.app.state
    settings = state.settings
    cache = getattr(state, "_provider_health_cache", None)
    now = time.time()
    if cache is not None and now - cache["checked_at"] < HEALTH_CACHE_TTL_SECONDS:
        return cache["body"]

    async with _health_cache_lock:
        # Re-check: another request may have refreshed the cache while we
        # waited for the lock.
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
    base = base_url.rstrip("/")
    headers = {}
    if ptype == "anthropic":
        url = f"{base}/v1/models"
        if api_key:
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif ptype == "gemini":
        url = f"{base}/v1beta/models"
        if api_key:
            headers = {"x-goog-api-key": api_key}
    else:
        url = f"{base}/models"
        if api_key:
            headers = {"authorization": f"Bearer {api_key}"}
    try:
        r = await client.get(url, headers=headers)
        # 401/403 still proves reachability; only transport errors don't.
        return True, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)[:200]
