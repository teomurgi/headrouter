"""Environment-driven configuration for the gateway."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Route:
    """A mapping from a logical (gateway) model to a provider + upstream model."""

    provider: str
    model: str


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
        )

    def resolve(self, model: str) -> Route | None:
        """Resolve a requested model name to a Route."""
        if model in self.routes:
            return self.routes[model]
        # Allow explicit `provider:model` passthrough in the request itself.
        direct = _parse_route(model)
        if direct and direct.provider in KNOWN_PROVIDERS:
            return direct
        return self.default_route

    def endpoint(self, provider: str) -> tuple[str, str]:
        """Return (base_url, api_key) for a provider."""
        if provider not in KNOWN_PROVIDERS:
            raise KeyError(provider)
        return self.provider_base_urls[provider], self.provider_api_keys.get(provider, "")
