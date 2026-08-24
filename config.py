"""Configuration for the gateway: env vars + optional JSON provider definitions."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from compression_service import COMPRESSION_STRATEGIES

logger = logging.getLogger("headrouter.config")

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


def load_dotenv(path: str | Path | None = None, env: dict[str, str] | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Existing environment variables are never overridden. Lines starting
    with '#' and blank lines are ignored; surrounding quotes are stripped.
    """
    target = os.environ if env is None else env
    candidates = [Path(path)] if path else [Path(".env"), Path(__file__).resolve().parent / ".env"]
    for candidate in candidates:
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in target:
                target[key] = value
        if path:
            break


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


@dataclass(frozen=True)
class KeyBinding:
    """Maps a gateway API key to a provider (and optional per-key model aliases)."""

    api_key: str
    provider: str
    routes: dict[str, str] = field(default_factory=dict)


def _load_json(source: str | list | dict) -> dict | list:
    if isinstance(source, (list, dict)):
        return source
    text = source.strip()
    if text.startswith("[") or text.startswith("{"):
        return json.loads(text)
    return json.loads(Path(text).read_text(encoding="utf-8"))


def _resolve_secret(entry: dict, env: dict[str, str], what: str) -> str:
    secret = str(entry.get("api_key", "") or "")
    secret_env = entry.get("api_key_env")
    if not secret and secret_env:
        secret = env.get(str(secret_env), "")
    if not secret:
        raise ConfigError(f"{what} needs a non-empty 'api_key' or resolvable 'api_key_env'")
    return secret


def load_gateway_config(
    source: str | list | dict, env: dict[str, str] | None = None
) -> tuple[dict[str, ProviderDef], dict[str, KeyBinding]]:
    """Load providers and API-key bindings from JSON.

    Shape:
        {
          "providers": [{"name": ..., "type": ..., "base_url": ..., "api_key"/"api_key_env": ...}],
          "keys": [{"api_key"/"api_key_env": ..., "provider": ..., "routes": {alias: model}}]
        }

    Each key binding routes requests authenticated with that key to the given
    provider. A key must not appear under different providers.
    """
    env = os.environ if env is None else env
    data = _load_json(source)
    if not isinstance(data, dict):
        data = {"providers": data}

    entries = data.get("providers", [])
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

    key_entries = data.get("keys", [])
    if not isinstance(key_entries, list):
        raise ConfigError("'keys' must be a list")

    bindings: dict[str, KeyBinding] = {}
    for i, entry in enumerate(key_entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"key entry #{i} must be an object")
        provider = str(entry.get("provider", "")).strip()
        if not provider:
            raise ConfigError(f"key entry #{i} is missing 'provider'")
        if provider not in defs and provider not in KNOWN_PROVIDERS:
            raise ConfigError(f"key entry #{i} references unknown provider '{provider}'")
        api_key = _resolve_secret(entry, env, f"key entry #{i}")
        routes = {str(k): str(v) for k, v in (entry.get("routes") or {}).items()}
        existing = bindings.get(api_key)
        if existing is not None:
            if existing.provider != provider:
                raise ConfigError(
                    f"api key may not map to multiple providers: '{api_key}' is bound to both "
                    f"'{existing.provider}' and '{provider}'"
                )
            continue  # identical duplicate binding
        bindings[api_key] = KeyBinding(api_key=api_key, provider=provider, routes=routes)
    return defs, bindings


def load_provider_defs(
    source: str | list | dict, env: dict[str, str] | None = None
) -> dict[str, ProviderDef]:
    """Backward-compatible wrapper returning only the provider definitions."""
    return load_gateway_config(source, env)[0]


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    api_keys: frozenset[str] = frozenset()
    routes: dict[str, Route] = field(default_factory=dict)
    default_route: Route | None = None
    compression_enabled: bool = True
    compression_threshold_tokens: int = 0
    compression_strategy: str = "coding"
    compression_prefetch_enabled: bool = True
    request_timeout_seconds: float = 300.0
    provider_base_urls: dict[str, str] = field(default_factory=dict)
    provider_api_keys: dict[str, str] = field(default_factory=dict)
    custom_providers: dict[str, ProviderDef] = field(default_factory=dict)
    key_bindings: dict[str, KeyBinding] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        if env is None:
            load_dotenv(os.environ.get("GATEWAY_ENV_FILE") or None)
        env = dict(os.environ if env is None else env)

        def get(key: str, default: str = "") -> str:
            return env.get(key, default)

        base_urls: dict[str, str] = {}
        api_keys: dict[str, str] = {}
        for provider, (key_var, default_url) in PROVIDER_DEFAULTS.items():
            base_urls[provider] = get(f"{provider.upper()}_BASE_URL", default_url).rstrip("/") or default_url
            if key_var:
                api_keys[provider] = env.get(key_var, "")

        custom: dict[str, ProviderDef] = {}
        key_bindings: dict[str, KeyBinding] = {}
        source = get("GATEWAY_PROVIDERS_FILE") or get("GATEWAY_PROVIDERS")
        if source and source.strip():
            custom, key_bindings = load_gateway_config(source, env=env)

        keys_raw = get("GATEWAY_API_KEYS")
        api_keys_set = frozenset(k.strip() for k in keys_raw.split(",") if k.strip())

        default_route = _parse_route(get("GATEWAY_DEFAULT_ROUTE"))
        compression_strategy = get("COMPRESSION_STRATEGY", "coding").strip().lower() or "coding"
        if compression_strategy not in COMPRESSION_STRATEGIES:
            raise ConfigError(
                f"invalid COMPRESSION_STRATEGY {compression_strategy!r}; expected one of "
                f"{sorted(COMPRESSION_STRATEGIES)}"
            )

        return cls(
            host=get("HOST", "0.0.0.0"),
            port=int(get("PORT", "8000") or 8000),
            api_keys=api_keys_set,
            routes=_parse_routes(get("GATEWAY_ROUTES")),
            default_route=default_route,
            compression_enabled=get("COMPRESSION_ENABLED", "1") not in ("0", "false", "False"),
            compression_threshold_tokens=int(get("COMPRESSION_THRESHOLD_TOKENS", "0") or 0),
            compression_strategy=compression_strategy,
            compression_prefetch_enabled=get("COMPRESSION_PREFETCH_ENABLED", "1")
            not in ("0", "false", "False"),
            request_timeout_seconds=float(get("REQUEST_TIMEOUT_SECONDS", "300") or 300),
            provider_base_urls=base_urls,
            provider_api_keys=api_keys,
            custom_providers=custom,
            key_bindings=key_bindings,
        )

    def effective_api_keys(self) -> frozenset[str]:
        """All keys accepted by the gateway: env-configured plus JSON-bound."""
        return self.api_keys | frozenset(self.key_bindings)

    def key_binding(self, api_key: str | None) -> KeyBinding | None:
        if not api_key:
            return None
        return self.key_bindings.get(api_key)

    def known_provider_names(self) -> set[str]:
        return KNOWN_PROVIDERS | set(self.custom_providers)

    def resolve(self, model: str, gateway_key: str | None = None) -> Route | None:
        """Resolve a requested model name to a Route.

        When the request was authenticated with a key that has a provider
        binding, that provider takes precedence over any route/direct provider.
        """
        binding = self.key_binding(gateway_key)

        if binding is None:
            if model in self.routes:
                return self.routes[model]
            direct = _parse_route(model)
            if direct and direct.provider in self.known_provider_names():
                return direct
            return self.default_route

        # Key-bound: provider is fixed; resolve only the model name.
        if model in binding.routes:
            return Route(provider=binding.provider, model=binding.routes[model])
        if model in self.routes:
            return Route(provider=binding.provider, model=self.routes[model].model)
        direct = _parse_route(model)
        if direct:
            return Route(provider=binding.provider, model=direct.model)
        if self.default_route is not None:
            return Route(provider=binding.provider, model=self.default_route.model)
        # No alias matched: pass the raw model name to the bound provider.
        return Route(provider=binding.provider, model=model)

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
