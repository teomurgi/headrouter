"""Configuration for the gateway: env vars + optional JSON provider definitions."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("headroom-gateway.config")

# Adapter types a provider can use. openai/openrouter/ollama are all handled
# by the OpenAI-compatible adapter; the set members are accepted as `type`.
OPENAI_COMPAT_TYPES = {"openai", "openrouter", "ollama", "openai-compat"}
ADAPTER_TYPES = OPENAI_COMPAT_TYPES | {"anthropic", "gemini"}


@dataclass(frozen=True)
class Route:
    """A mapping from a logical (gateway) model to a provider + upstream model."""

    provider: str
    model: str


@dataclass(frozen=True)
class ProviderDef:
    """A configured upstream provider endpoint."""

    name: str
    type: str
    base_url: str
    api_key: str = ""

    @property
    def is_openai_compat(self) -> bool:
        return self.type in OPENAI_COMPAT_TYPES


PROVIDER_DEFAULTS: dict[str, tuple[str | None, str]] = {
    # provider -> (env var for api key, default base url)
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
    "ollama": (None, "http://localhost:11434/v1"),
    "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com"),
}

OPENAI_COMPAT_PROVIDERS = {"openai", "openrouter", "ollama"}
KNOWN_PROVIDERS = OPENAI_COMPAT_PROVIDERS | {"anthropic", "gemini"}


class ConfigError(ValueError):
    """Raised for invalid provider/route configuration."""


def _parse_routes(raw: str) -> dict[str, Route]:
    """Parse `alias=provider:model,alias2=provider2:model2`."""
    routes: dict[str, Route] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        alias, sep, target = part.partition("=")
        if not sep:
            continue
        provider, sep2, model = target.partition(":")
        alias, provider, model = alias.strip(), provider.strip(), model.strip()
        if alias and sep2 and provider and model:
            routes[alias] = Route(provider=provider, model=model)
    return routes


def _parse_route(raw: str | None) -> Route | None:
    if not raw:
        return None
    provider, sep, model = raw.partition(":")
    if sep and provider.strip() and model.strip():
        return Route(provider=provider.strip(), model=model.strip())
    return None


def load_provider_defs(source: str | list | dict, env: dict[str, str] | None = None) -> dict[str, ProviderDef]:
    """Load custom provider definitions from a JSON string or a JSON file path.

    Accepted shapes:
        {"providers": [{"name": ..., "type": ..., "base_url": ..., "api_key": ...}]}
        [{"name": ..., "type": ..., "base_url": ..., "api_key": ...}]

    `api_key` may be given directly or via `api_key_env` (name of an env var).
    """
    env = os.environ if env is None else env
    if isinstance(source, (list, dict)):
        data = source
    else:
        text = source.strip()
        if text.startswith("[") or text.startswith("{"):
            data = json.loads(text)
        else:
            data = json.loads(Path(text).read_text(encoding="utf-8"))

    entries = data.get("providers", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ConfigError("providers JSON must be a list or an object with a 'providers' list")

    defs: dict[str, ProviderDef] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"provider entry #{i} must be an object")
        name = str(entry.get("name", "")).strip()
        ptype = str(entry.get("type", "")).strip().lower()
        base_url = str(entry.get("base_url", "") or "").strip()
        if not name:
            raise ConfigError(f"provider entry #{i} is missing 'name'")
        if ptype not in ADAPTER_TYPES:
            raise ConfigError(
                f"provider '{name}': invalid type '{ptype}' (expected one of {sorted(ADAPTER_TYPES)})"
            )
        if not base_url:
            raise ConfigError(f"provider '{name}' is missing 'base_url'")
        api_key = str(entry.get("api_key", "") or "")
        api_key_env = entry.get("api_key_env")
        if not api_key and api_key_env:
            api_key = env.get(str(api_key_env), "")
        if name in defs:
            raise ConfigError(f"duplicate provider name '{name}'")
        defs[name] = ProviderDef(name=name, type=ptype, base_url=base_url.rstrip("/"), api_key=api_key)
    return defs


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    api_keys: frozenset[str] = frozenset()
    routes: dict[str, Route] = field(default_factory=dict)
    default_route: Route | None = None
    compression_enabled: bool = True
    compression_threshold_tokens: int = 4000
    request_timeout_seconds: float = 300.0
    provider_base_urls: dict[str, str] = field(default_factory=dict)
    provider_api_keys: dict[str, str] = field(default_factory=dict)
    custom_providers: dict[str, ProviderDef] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)

        def get(key: str, default: str = "") -> str:
            return env.get(key, default)

        base_urls: dict[str, str] = {}
        api_keys: dict[str, str] = {}
        for provider, (key_var, default_url) in PROVIDER_DEFAULTS.items():
            base_urls[provider] = get(f"{provider.upper()}_BASE_URL", default_url) or default_url
            if key_var:
                api_keys[provider] = env.get(key_var, "")

        custom: dict[str, ProviderDef] = {}
        source = get("GATEWAY_PROVIDERS_FILE") or get("GATEWAY_PROVIDERS")
        if source and source.strip():
            custom = load_provider_defs(source, env=env)

        keys_raw = get("GATEWAY_API_KEYS")
        api_keys_set = frozenset(k.strip() for k in keys_raw.split(",") if k.strip())

        default_route = _parse_route(get("GATEWAY_DEFAULT_ROUTE"))

        return cls(
            host=get("HOST", "0.0.0.0"),
            port=int(get("PORT", "8000") or 8000),
            api_keys=api_keys_set,
            routes=_parse_routes(get("GATEWAY_ROUTES")),
            default_route=default_route,
            compression_enabled=get("COMPRESSION_ENABLED", "1") not in ("0", "false", "False"),
            compression_threshold_tokens=int(get("COMPRESSION_THRESHOLD_TOKENS", "4000") or 4000),
            request_timeout_seconds=float(get("REQUEST_TIMEOUT_SECONDS", "300") or 300),
            provider_base_urls=base_urls,
            provider_api_keys=api_keys,
            custom_providers=custom,
        )

    def known_provider_names(self) -> set[str]:
        return KNOWN_PROVIDERS | set(self.custom_providers)

    def resolve(self, model: str) -> Route | None:
        """Resolve a requested model name to a Route."""
        if model in self.routes:
            return self.routes[model]
        # Allow explicit `provider:model` passthrough in the request itself.
        direct = _parse_route(model)
        if direct and direct.provider in self.known_provider_names():
            return direct
        return self.default_route

    def endpoint(self, provider: str) -> ProviderDef:
        """Return the ProviderDef (type, base_url, api_key) for a provider name."""
        custom = self.custom_providers.get(provider)
        if custom is not None:
            return custom
        if provider in KNOWN_PROVIDERS:
            return ProviderDef(
                name=provider,
                type=provider,
                base_url=self.provider_base_urls[provider],
                api_key=self.provider_api_keys.get(provider, ""),
            )
        raise KeyError(provider)
