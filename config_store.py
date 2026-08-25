"""Atomic config lifecycle (INV-4): load → validate → swap → persist.

A ConfigStore owns one providers.json file. Applies build a fresh immutable
Settings, validate it, atomically swap the live snapshot, and persist the v2
shape via tmp-file + os.replace. In-flight requests keep the old snapshot
because Settings is immutable and never mutated in place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from config import ConfigError, Settings, load_config_v2, validate_config

logger = logging.getLogger("headrouter.config_store")


class ConfigStore:
    def __init__(self, path: str | Path, base_settings: Settings | None = None, env: dict[str, str] | None = None):
        self.path = Path(path)
        self._base = base_settings or Settings()
        self._env = env
        self._lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None
        defs, keys, aliases = load_config_v2(str(self.path), env=self._env)
        self._settings = self._build(defs, keys, aliases)
        self._migrated_keys = {k for k, b in keys.items() if b.legacy_provider_grant}
        self._listeners: list = []
        self._raw: dict = self._read_raw()
        self._last_generated: list[dict] = []

    def on_apply(self, callback) -> None:
        """Register a callable(Settings) invoked after each atomic swap."""
        self._listeners.append(callback)

    # -- construction -----------------------------------------------------

    def _read_raw(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except OSError:
            return {}

    def sanitized_config(self) -> dict:
        """The applied config as the admin API may return it (INV-5, clause 1):
        env-var names and a set/not-set hint only — never secret values."""
        data = json.loads(json.dumps(self._raw))  # deep copy
        for p in data.get("providers", []):
            if "api_key" in p:
                p.pop("api_key")
                p["api_key_set"] = True
            else:
                p["api_key_set"] = bool(p.get("api_key_env"))
        for k in data.get("keys", []):
            k.pop("api_key", None)
        return data

    def _build(self, defs, keys, aliases) -> Settings:
        return replace(
            self._base,
            custom_providers=defs,
            key_bindings=keys,
            aliases=aliases,
        )

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def migrated_keys(self) -> set[str]:
        """Keys whose grants were synthesized by v1→v2 migration, not hand-authored."""
        return set(self._migrated_keys)

    # -- apply ------------------------------------------------------------

    def _prepare(self, data: dict):
        errors = validate_config(data)
        if errors:
            raise ConfigError("; ".join(errors))
        # INV-5 split: provider api_key VALUES are accepted write-only; gateway
        # key values never are. Blank/absent on edit keeps the stored value.
        for k in data.get("keys", []):
            if isinstance(k, dict) and k.get("api_key"):
                raise ConfigError("keys must reference 'api_key_env' or be generated; raw values are not accepted")
        saved_providers = {p.get("name"): p for p in self._raw.get("providers", []) if isinstance(p, dict)}
        for p in data.get("providers", []):
            if not isinstance(p, dict):
                continue
            p.pop("api_key_set", None)  # read-only hint from GET, not input
            if p.get("api_key") == "":
                # explicit empty string = remove the stored credential (§3.3);
                # distinct from absent, which means keep (write-only round-trip)
                p.pop("api_key")
                p["_strip_credential"] = True
                continue
            if p.get("api_key"):
                continue
            saved = saved_providers.get(p.get("name"))
            if saved is not None and saved.get("api_key") and not saved.get("_strip_credential"):
                p["api_key"] = saved["api_key"]  # blank-on-edit keeps existing
        for p in data.get("providers", []):
            if isinstance(p, dict):
                p.pop("_strip_credential", None)
        # Keys with neither api_key nor api_key_env: reuse the stored value if
        # this is an existing key (GET strips values, so round-trips must not
        # regenerate them); otherwise generate one server-side (issue-key
        # flow) — returned once, stored in the file, stripped from every GET.
        self._last_generated = []
        existing = {b.name: b.api_key for b in self._settings.key_bindings.values() if b.name}
        for k in data.get("keys", []):
            if not isinstance(k, dict) or k.get("api_key") or k.get("api_key_env"):
                continue
            name = str(k.get("name") or "")
            if name in existing:
                k["api_key"] = existing[name]
                continue
            value = "hr_" + secrets.token_urlsafe(24)
            k["api_key"] = value
            self._last_generated.append({"name": name or "key", "api_key": value})
        defs, keys, aliases = load_config_v2(data, env=self._env)
        return defs, keys, aliases

    def _persist(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".providers-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _do_apply(self, data: dict) -> Settings:
        defs, keys, aliases = self._prepare(data)
        self._persist(data)
        settings = self._build(defs, keys, aliases)
        self._migrated_keys = {k for k, b in keys.items() if b.legacy_provider_grant}
        self._settings = settings
        self._raw = data
        for listener in self._listeners:
            listener(settings)
        logger.info(
            "config applied atomically: %d provider(s), %d alias(es), %d key(s)%s",
            len(defs), len(aliases), len(keys),
            f" ({len(self._migrated_keys)} migrated grant(s) pending review)" if self._migrated_keys else "",
        )
        return settings

    def apply(self, data: dict) -> Settings:
        with self._lock:
            return self._do_apply(data)

    async def apply_async(self, data: dict) -> Settings:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        async with self._async_lock:
            return await asyncio.to_thread(self.apply, data)

    def validate(self, data: dict) -> list[str]:
        """Dry-run validation only (POST /admin/config/validate)."""
        return validate_config(data)

    def pop_generated(self) -> list[dict]:
        """One-time generated key values from the last apply (drained on read)."""
        out = self._last_generated
        self._last_generated = []
        return out
