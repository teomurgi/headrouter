import json
from unittest import mock

import httpx
import pytest

from app import create_app
from compression import CompressionService
from config import (
    ConfigError,
    KeyBinding,
    ProviderDef,
    Route,
    Settings,
    load_gateway_config,
    load_provider_defs,
)

from conftest import API_KEY, make_handler

PROVIDERS_JSON = json.dumps(
    {
        "providers": [
            {
                "name": "my-openai",
                "type": "openai",
                "base_url": "https://proxy.example.com/v1",
                "api_key": "sk-custom",
            },
            {
                "name": "work-claude",
                "type": "anthropic",
                "base_url": "https://claude.example.com",
                "api_key_env": "WORK_ANTHROPIC_KEY",
            },
        ]
    }
)

KEYED_PROVIDERS_JSON = json.dumps(
    {
        "providers": [
            {
                "name": "my-openai",
                "type": "openai",
                "base_url": "https://proxy.example.com/v1",
                "api_key": "sk-custom",
            },
            {
                "name": "work-claude",
                "type": "anthropic",
                "base_url": "https://claude.example.com",
                "api_key": "sk-ant-custom",
            },
        ],
        "keys": [
            {"api_key": "key-team-a", "provider": "my-openai"},
            {"api_key": "key-team-b", "provider": "work-claude", "routes": {"gpt4o": "gpt-4o-turbo"}},
        ],
    }
)


def make_settings(**overrides) -> Settings:
    custom = load_provider_defs(PROVIDERS_JSON, env={"WORK_ANTHROPIC_KEY": "sk-ant-custom"})
    return Settings(
        api_keys=frozenset({API_KEY}),
        routes={
            "proxy-gpt": Route("my-openai", "gpt-4o"),
            "work-sonnet": Route("work-claude", "claude-sonnet-4"),
        },
        compression_threshold_tokens=100000,
        custom_providers=custom,
        provider_base_urls={
            "openai": "https://api.openai.test/v1",
            "openrouter": "https://openrouter.test/v1",
            "ollama": "http://ollama.test/v1",
            "anthropic": "https://anthropic.test",
            "gemini": "https://gemini.test",
        },
        **overrides,
    )


def test_load_provider_defs_from_inline_json():
    defs = load_provider_defs(PROVIDERS_JSON, env={})
    assert set(defs) == {"my-openai", "work-claude"}
    assert defs["my-openai"].type == "openai"
    assert defs["my-openai"].base_url == "https://proxy.example.com/v1"
    assert defs["my-openai"].api_key == "sk-custom"
    assert defs["my-openai"].is_openai_compat


def test_compression_strategy_from_env():
    settings = Settings.from_env({"COMPRESSION_STRATEGY": "BALANCED"})
    assert settings.compression_strategy == "balanced"


def test_invalid_compression_strategy_rejected():
    with pytest.raises(ConfigError, match="invalid COMPRESSION_STRATEGY"):
        Settings.from_env({"COMPRESSION_STRATEGY": "maximum"})


def test_load_provider_defs_api_key_env_resolution():
    defs = load_provider_defs(PROVIDERS_JSON, env={"WORK_ANTHROPIC_KEY": "secret123"})
    assert defs["work-claude"].api_key == "secret123"
    assert not defs["work-claude"].is_openai_compat


def test_load_provider_defs_from_file(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(PROVIDERS_JSON)
    defs = load_provider_defs(str(path))
    assert set(defs) == {"my-openai", "work-claude"}


def test_load_provider_defs_accepts_bare_list():
    defs = load_provider_defs(
        [{"name": "x", "type": "gemini", "base_url": "https://x.test"}]
    )
    assert defs["x"].type == "gemini"


@pytest.mark.parametrize(
    "entry,match",
    [
        ({"type": "openai", "base_url": "https://x"}, "name"),
        ({"name": "x", "type": "bogus", "base_url": "https://x"}, "invalid type"),
        ({"name": "x", "type": "openai"}, "base_url"),
    ],
)
def test_load_provider_defs_validation(entry, match):
    with pytest.raises(ConfigError, match=match):
        load_provider_defs(json.dumps([entry]))


def test_load_provider_defs_duplicate_name():
    raw = json.dumps(
        [
            {"name": "x", "type": "openai", "base_url": "https://x"},
            {"name": "x", "type": "openai", "base_url": "https://y"},
        ]
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_provider_defs(raw)


def test_from_env_reads_providers_file(tmp_path):
    from config import Settings as S

    path = tmp_path / "providers.json"
    path.write_text(PROVIDERS_JSON)
    settings = S.from_env(
        {
            "GATEWAY_PROVIDERS_FILE": str(path),
            "WORK_ANTHROPIC_KEY": "k",
            "GATEWAY_ROUTES": "p=my-openai:gpt-4o",
        }
    )
    assert settings.custom_providers["my-openai"].base_url == "https://proxy.example.com/v1"
    assert settings.routes["p"].provider == "my-openai"


def test_custom_openai_provider_routing(captured):
    settings = make_settings()
    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "proxy-gpt", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    sent = captured["requests"][0]
    assert sent["url"] == "https://proxy.example.com/v1/chat/completions"
    assert sent["body"]["model"] == "gpt-4o"
    assert sent["headers"]["authorization"] == "Bearer sk-custom"


def test_custom_anthropic_provider_routing(captured):
    settings = make_settings()
    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "work-sonnet", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    sent = captured["requests"][0]
    assert sent["url"] == "https://claude.example.com/v1/messages"
    assert sent["body"]["model"] == "claude-sonnet-4"
    assert sent["headers"]["x-api-key"] == "sk-ant-custom"


def test_custom_provider_direct_syntax(captured):
    settings = make_settings()
    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "my-openai:gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert captured["requests"][0]["url"] == "https://proxy.example.com/v1/chat/completions"


def test_key_bindings_parsed():
    defs, keys = load_gateway_config(KEYED_PROVIDERS_JSON)
    assert keys["key-team-a"] == KeyBinding("key-team-a", "my-openai")
    assert keys["key-team-b"].provider == "work-claude"
    assert keys["key-team-b"].routes == {"gpt4o": "gpt-4o-turbo"}


def test_key_binding_unknown_provider_rejected():
    raw = json.dumps({"keys": [{"api_key": "k", "provider": "nope"}]})
    with pytest.raises(ConfigError, match="unknown provider 'nope'"):
        load_gateway_config(raw)


def test_key_binding_missing_secret_rejected():
    raw = json.dumps(
        {"providers": [{"name": "p", "type": "openai", "base_url": "https://x"}],
         "keys": [{"api_key_env": "NOT_SET_ANYWHERE", "provider": "p"}]}
    )
    with pytest.raises(ConfigError, match="api_key"):
        load_gateway_config(raw, env={})


def test_same_key_two_providers_rejected():
    raw = json.dumps(
        {
            "providers": [
                {"name": "a", "type": "openai", "base_url": "https://a"},
                {"name": "b", "type": "anthropic", "base_url": "https://b"},
            ],
            "keys": [
                {"api_key": "shared", "provider": "a"},
                {"api_key": "shared", "provider": "b"},
            ],
        }
    )
    with pytest.raises(ConfigError, match="may not map to multiple providers"):
        load_gateway_config(raw)


def test_same_key_same_provider_ok():
    raw = json.dumps(
        {
            "providers": [{"name": "a", "type": "openai", "base_url": "https://a"}],
            "keys": [
                {"api_key": "shared", "provider": "a"},
                {"api_key": "shared", "provider": "a"},
            ],
        }
    )
    _defs, keys = load_gateway_config(raw)
    assert list(keys) == ["shared"]


def make_keyed_settings(**overrides) -> Settings:
    defs, keys = load_gateway_config(KEYED_PROVIDERS_JSON)
    return Settings(
        routes={"gpt4o": Route("work-claude", "gpt-4o"), "claude": Route("work-claude", "claude-3")},
        compression_threshold_tokens=100000,
        custom_providers=defs,
        key_bindings=keys,
        **overrides,
    )


def _post(c, model, key):
    return c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_bound_key_overrides_route_provider(captured):
    app = create_app(
        settings=make_keyed_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        # Alias "gpt4o" is routed to work-claude, but key-team-a is bound to my-openai.
        r = _post(c, "gpt4o", "key-team-a")
        assert r.status_code == 200
        assert captured["requests"][0]["url"] == "https://proxy.example.com/v1/chat/completions"
        assert captured["requests"][0]["body"]["model"] == "gpt-4o"

        # Unbound model name goes to the bound provider as-is.
        r = _post(c, "gpt-4o-mini", "key-team-b")
        assert r.status_code == 200
        assert captured["requests"][1]["url"] == "https://claude.example.com/v1/messages"
        assert captured["requests"][1]["body"]["model"] == "gpt-4o-mini"


def test_per_key_route_overrides_model(captured):
    app = create_app(
        settings=make_keyed_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        # key-team-b has {"gpt4o": "gpt-4o-turbo"} per-key route.
        r = _post(c, "gpt4o", "key-team-b")
        assert r.status_code == 200
        assert captured["requests"][0]["url"] == "https://claude.example.com/v1/messages"
        assert captured["requests"][0]["body"]["model"] == "gpt-4o-turbo"


def test_bound_key_accepted_for_auth(captured):
    # No GATEWAY_API_KEYS configured: JSON-bound keys alone enable auth.
    app = create_app(
        settings=make_keyed_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        assert _post(c, "gpt4o", "wrong-key").status_code == 401
        assert _post(c, "gpt4o", "key-team-a").status_code == 200


def test_from_env_loads_key_bindings(tmp_path):
    from config import Settings as S

    path = tmp_path / "providers.json"
    path.write_text(KEYED_PROVIDERS_JSON)
    settings = S.from_env({"GATEWAY_PROVIDERS_FILE": str(path)})
    assert "key-team-a" in settings.key_bindings
    assert settings.effective_api_keys() >= {"key-team-a", "key-team-b"}
