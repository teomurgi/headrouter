"""A1 — alias model, closed resolver (INV-1/2/3), migration (INV-8)."""

import json

import pytest

from config import (
    ConfigError,
    ModelNotGranted,
    Route,
    Settings,
    load_config_v2,
    load_gateway_config,
    validate_config,
)

V2_JSON = json.dumps(
    {
        "providers": [
            {"name": "my-openai", "type": "openai", "base_url": "https://proxy.example.com/v1", "api_key": "sk-custom"},
            {"name": "or", "type": "openrouter", "base_url": "https://openrouter.example.com/v1", "api_key": "sk-or"},
        ],
        "aliases": {"fast": "or:gpt-4o-mini", "smart": "my-openai:gpt-4o"},
        "keys": [
            {"name": "team-a", "api_key": "key-a", "aliases": ["fast"]},
            {"name": "team-b", "api_key": "key-b", "aliases": ["fast", "smart"]},
        ],
    }
)


def make_v2_settings(**overrides) -> Settings:
    defs, keys, aliases = load_config_v2(V2_JSON)
    overrides.setdefault("aliases", aliases)
    return Settings(custom_providers=defs, key_bindings=keys, **overrides)


# --- validate_config (INV-3, §4 rules) -------------------------------------


def test_validate_ok():
    assert validate_config(json.loads(V2_JSON)) == []


def test_validate_key_with_empty_aliases():
    data = json.loads(V2_JSON)
    data["keys"][0]["aliases"] = []
    errs = validate_config(data)
    assert any("team-a" in e and "alias" in e for e in errs)


def test_validate_key_referencing_unknown_alias():
    data = json.loads(V2_JSON)
    data["keys"][0]["aliases"] = ["nope"]
    errs = validate_config(data)
    assert any("nope" in e for e in errs)


def test_validate_alias_unknown_provider():
    data = json.loads(V2_JSON)
    data["aliases"]["bad"] = "ghost:model"
    errs = validate_config(data)
    assert any("bad" in e and "ghost" in e for e in errs)


def test_validate_alias_bad_syntax():
    data = json.loads(V2_JSON)
    data["aliases"]["bad"] = "noseparator"
    errs = validate_config(data)
    assert any("bad" in e for e in errs)


def test_validate_duplicate_alias_keys_is_invalid_json_shape():
    # duplicate alias names are impossible in a JSON object; duplicate key
    # VALUES remain the checkable duplicate rule (§4).
    data = json.loads(V2_JSON)
    data["keys"][1]["api_key"] = "key-a"
    errs = validate_config(data)
    assert any("key-a" in e.lower() and "duplicate" in e.lower() for e in errs)


def test_validate_provider_rules_still_enforced():
    data = json.loads(V2_JSON)
    data["providers"][0]["type"] = "bogus"
    errs = validate_config(data)
    assert any("invalid type" in e for e in errs)


# --- loader: v2 shape -------------------------------------------------------


def test_load_v2_aliases_and_key_grants():
    defs, keys, aliases = load_config_v2(V2_JSON)
    assert set(defs) == {"my-openai", "or"}
    assert aliases == {"fast": Route("or", "gpt-4o-mini"), "smart": Route("my-openai", "gpt-4o")}
    assert keys["key-a"].aliases == frozenset({"fast"})
    assert keys["key-a"].name == "team-a"
    assert keys["key-a"].provider == "or"  # derived from granted alias target


def test_load_v2_rejects_invalid():
    data = json.loads(V2_JSON)
    data["keys"][0]["aliases"] = []
    with pytest.raises(ConfigError, match="alias"):
        load_config_v2(data)


# --- migration (INV-8): old shape → explicit per-key alias grants -----------


OLD_JSON = json.dumps(
    {
        "providers": [
            {"name": "my-openai", "type": "openai", "base_url": "https://proxy.example.com/v1", "api_key": "sk-custom"}
        ],
        "keys": [
            {"api_key": "key-a", "provider": "my-openai"},
            {"api_key": "key-b", "provider": "my-openai", "routes": {"gpt4o": "gpt-4o-turbo"}},
        ],
    }
)


def test_migration_synthesizes_explicit_grants():
    defs, keys, aliases = load_config_v2(OLD_JSON)
    # key-b's per-key route becomes an explicit global alias + grant
    assert keys["key-b"].aliases == frozenset({"gpt4o"})
    assert aliases == {"gpt4o": Route("my-openai", "gpt-4o-turbo")}
    # key-a had no routes: provider-scoped legacy grant ("*") synthesized
    assert keys["key-a"].aliases == frozenset({"*"})
    assert keys["key-a"].legacy_provider_grant == "my-openai"


def test_migration_aliases_resolve_on_bound_provider():
    defs, keys, aliases = load_config_v2(OLD_JSON)
    s = Settings(custom_providers=defs, key_bindings=keys, aliases=aliases)
    route = s.resolve("gpt4o", "key-b")
    assert route == Route("my-openai", "gpt-4o-turbo")
    # legacy passthrough grant: raw model to the bound provider
    assert s.resolve("gpt-4o-mini", "key-a") == Route("my-openai", "gpt-4o-mini")


def test_migration_logged(caplog):
    with caplog.at_level("INFO", logger="headrouter.config"):
        load_config_v2(OLD_JSON)
    assert any("migrat" in r.message.lower() for r in caplog.records)


# --- closed resolution (INV-1) ----------------------------------------------


def test_bound_key_granted_alias_resolves():
    s = make_v2_settings()
    assert s.resolve("fast", "key-a") == Route("or", "gpt-4o-mini")
    assert s.resolve("smart", "key-b") == Route("my-openai", "gpt-4o")


def test_bound_key_ungranted_alias_denied():
    s = make_v2_settings()
    with pytest.raises(ModelNotGranted) as ei:
        s.resolve("smart", "key-a")
    assert ei.value.available == ["fast"]


def test_bound_key_raw_provider_model_denied():
    s = make_v2_settings()
    with pytest.raises(ModelNotGranted):
        s.resolve("or:gpt-4o-mini", "key-a")


def test_bound_key_unknown_name_denied_not_fallthrough():
    s = make_v2_settings()
    with pytest.raises(ModelNotGranted) as ei:
        s.resolve("gpt-4o-turbo-preview", "key-a")
    assert ei.value.available == ["fast"]


def test_admin_key_full_legacy_behavior():
    s = make_v2_settings(
        api_keys=frozenset({"admin"}),
        routes={"envroute": Route("my-openai", "gpt-4o")},
        default_route=Route("or", "gpt-4o-mini"),
        aliases={"fast": Route("or", "gpt-4o-mini")},
    )
    assert s.resolve("fast", "admin") == Route("or", "gpt-4o-mini")
    assert s.resolve("envroute", "admin") == Route("my-openai", "gpt-4o")
    assert s.resolve("my-openai:gpt-4o", "admin") == Route("my-openai", "gpt-4o")
    assert s.resolve("anything", "admin") == Route("or", "gpt-4o-mini")
    assert s.resolve("anything", None) == Route("or", "gpt-4o-mini")


# --- single source for /v1models agreement (INV-2 groundwork) ---------------


def test_models_for_key_bound_vs_admin():
    s = make_v2_settings(api_keys=frozenset({"admin"}))
    assert s.models_for_key("key-a") == ["fast"]
    assert s.models_for_key("key-b") == ["fast", "smart"]
    assert set(s.models_for_key("admin")) >= {"fast", "smart"}


# --- A2: key-aware /v1/models + deny error (INV-2, HTTP level) ---------------

import httpx
from app import create_app
from conftest import make_handler


def make_client(captured, **overrides):
    from fastapi.testclient import TestClient
    s = make_v2_settings(api_keys=frozenset({"admin"}), compression_threshold_tokens=100000, compression_prefetch_enabled=False, **overrides)
    app = create_app(
        settings=s,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    return TestClient(app)


def test_v1_models_scoped_to_bound_key(captured):
    with make_client(captured) as c:
        r = c.get("/v1/models", headers={"Authorization": "Bearer key-a"})
        assert r.status_code == 200
        assert [m["id"] for m in r.json()["data"]] == ["fast"]


def test_v1_models_admin_sees_all(captured):
    with make_client(captured) as c:
        r = c.get("/v1/models", headers={"Authorization": "Bearer admin"})
        assert {m["id"] for m in r.json()["data"]} >= {"fast", "smart"}


def test_deny_error_lists_exact_granted_set(captured):
    with make_client(captured) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer key-a"},
            json={"model": "smart", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 404
        msg = r.json()["error"]["message"]
        # INV-2: same set as /v1/models for this key
        models_msg = set(msg.split("Available models: ")[1].split(".")[0].split(", "))
        assert models_msg == {"fast"}


def test_deny_error_via_proxy_path(captured):
    with make_client(captured) as c:
        r = c.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer key-a"},
            json={"model": "smart", "input": "hi"},
        )
        assert r.status_code == 404
        assert "not available for this key" in r.json()["error"]["message"]
        assert not captured["requests"]  # never reached upstream


def test_granted_alias_still_proxied(captured):
    with make_client(captured) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer key-a"},
            json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert captured["requests"][0]["url"] == "https://openrouter.example.com/v1/chat/completions"
