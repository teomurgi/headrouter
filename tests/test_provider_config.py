import json
from unittest import mock

import httpx
import pytest

from app import create_app
from compression import CompressionService
from config import ConfigError, ProviderDef, Route, Settings, load_provider_defs

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
