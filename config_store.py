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
        """The applied config as the admin API may return it (INV-5):
        env-var names only, never secret values."""
        data = json.loads(json.dumps(self._raw))  # deep copy
        for p in data.get("providers", []):
            p.pop("api_key", None)
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
        # INV-5: the admin surface never accepts secret values.
        for p in data.get("providers", []):
            if p.get("api_key"):
                raise ConfigError("providers must reference 'api_key_env', not embed 'api_key' values")
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
