"""A1 — grants model, closed resolver (INV-1/2/3), migration (INV-8)."""

import json

import pytest

from config import (
    ConfigError,
    ModelGrant,
    ModelNotGranted,
    Route,
    Settings,
    load_config_v2,
    validate_config,
)

V3_JSON = json.dumps(
    {
        "providers": [
            {"name": "my-openai", "type": "openai", "base_url": "https://proxy.example.com/v1", "api_key": "sk-custom"},
            {"name": "or", "type": "openrouter", "base_url": "https://openrouter.example.com/v1", "api_key": "sk-or"},
        ],
        "keys": [
            {"name": "team-a", "api_key": "key-a", "grants": [{"provider": "or", "models": ["gpt-4o-mini"]}]},
            {
                "name": "team-b",
                "api_key": "key-b",
                "grants": [
                    {"provider": "or", "models": ["gpt-4o-mini"]},
                    {"provider": "my-openai", "models": ["gpt-4o"]},
                ],
            },
        ],
    }
)


def make_v3_settings(**overrides) -> Settings:
    defs, keys = load_config_v2(V3_JSON)
    return Settings(custom_providers=defs, key_bindings=keys, **overrides)


# --- validate_config (INV-3, §4 rules) -------------------------------------


def test_validate_ok():
    assert validate_config(json.loads(V3_JSON)) == []


def test_validate_key_without_grants():
    data = json.loads(V3_JSON)
    data["keys"][0]["grants"] = []
    errs = validate_config(data)
    assert any("team-a" in e and "grant" in e for e in errs)


def test_validate_grant_must_be_object():
    data = json.loads(V3_JSON)
    data["keys"][0]["grants"] = ["or:gpt-4o-mini"]
    errs = validate_config(data)
    assert any("team-a" in e and "object" in e for e in errs)


def test_validate_grant_unknown_provider():
    data = json.loads(V3_JSON)
    data["keys"][0]["grants"] = [{"provider": "ghost", "models": ["m"]}]
    errs = validate_config(data)
    assert any("ghost" in e for e in errs)


def test_validate_grant_empty_models():
    data = json.loads(V3_JSON)
    data["keys"][0]["grants"] = [{"provider": "or", "models": []}]
    errs = validate_config(data)
    assert any("model" in e for e in errs)


def test_validate_duplicate_key_values():
    data = json.loads(V3_JSON)
    data["keys"][1]["api_key"] = "key-a"
    errs = validate_config(data)
    assert any("key-a" in e.lower() and "duplicate" in e.lower() for e in errs)


def test_validate_provider_rules_still_enforced():
    data = json.loads(V3_JSON)
    data["providers"][0]["type"] = "bogus"
    errs = validate_config(data)
    assert any("invalid type" in e for e in errs)


def test_validate_rejects_admin_flag_on_keys():
    # keys[].admin is dead config with teeth: admin comes solely from
    # GATEWAY_API_KEYS (INV-9) — accept-and-ignore would mislead hand-editors.
    data = json.loads(V3_JSON)
    data["keys"][0]["admin"] = True
    errs = validate_config(data)
    assert any("GATEWAY_API_KEYS" in e and "'admin'" in e for e in errs), errs


# --- loader: v3 shape -------------------------------------------------------


def test_load_v3_grants():
    defs, keys = load_config_v2(V3_JSON)
    assert set(defs) == {"my-openai", "or"}
    assert keys["key-a"].grants == (ModelGrant(provider="or", models=frozenset({"gpt-4o-mini"})),)
    assert keys["key-a"].name == "team-a"
    assert keys["key-a"].provider == "or"  # single grant → derived provider
    assert keys["key-b"].provider == ""  # multi-grant → no single provider
    assert {g.provider for g in keys["key-b"].grants} == {"or", "my-openai"}


def test_load_v3_merges_duplicate_provider_grants():
    data = json.loads(V3_JSON)
    data["keys"][1]["grants"].append({"provider": "or", "models": ["gpt-4o"]})
    _, keys = load_config_v2(data)
    or_grants = [g for g in keys["key-b"].grants if g.provider == "or"]
    assert len(or_grants) == 1
    assert or_grants[0].models == frozenset({"gpt-4o-mini", "gpt-4o"})


def test_load_v3_rejects_invalid():
    data = json.loads(V3_JSON)
    data["keys"][0]["grants"] = []
    with pytest.raises(ConfigError, match="grant"):
        load_config_v2(data)


# --- migration (INV-8): v2 aliases → explicit per-key grants ----------------


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


def test_migration_v2_aliases_become_grants():
    defs, keys = load_config_v2(V2_JSON)
    assert keys["key-a"].grants == (ModelGrant(provider="or", models=frozenset({"gpt-4o-mini"})),)
    b_grants = {g.provider: g.models for g in keys["key-b"].grants}
    assert b_grants == {"or": frozenset({"gpt-4o-mini"}), "my-openai": frozenset({"gpt-4o"})}
    # granted upstream model names resolve to their granting provider
    s = Settings(custom_providers=defs, key_bindings=keys)
    assert s.resolve("gpt-4o-mini", "key-a") == Route("or", "gpt-4o-mini")
    assert s.resolve("gpt-4o", "key-b") == Route("my-openai", "gpt-4o")


def test_migration_v2_logged(caplog):
    with caplog.at_level("INFO", logger="headrouter.config"):
        load_config_v2(V2_JSON)
    assert any("migrat" in r.message.lower() for r in caplog.records)


# --- migration (INV-8): v1 provider+routes → grants -------------------------


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


def test_migration_v1_synthesizes_grants():
    _, keys = load_config_v2(OLD_JSON)
    # key-b's route grants BOTH the client-facing alias and the upstream model
    assert keys["key-b"].grants == (
        ModelGrant(provider="my-openai", models=frozenset({"gpt4o", "gpt-4o-turbo"})),
    )
    # key-a had no routes: legacy provider-scoped passthrough grant
    assert keys["key-a"].grants == ()
    assert keys["key-a"].legacy_provider_grant == "my-openai"


def test_migration_v1_resolves():
    defs, keys = load_config_v2(OLD_JSON)
    s = Settings(custom_providers=defs, key_bindings=keys)
    # v3 grants pass the model name upstream verbatim — "keeps working" means
    # both old client names are still ACCEPTED (granted), not re-mapped.
    assert s.resolve("gpt4o", "key-b") == Route("my-openai", "gpt4o")
    assert s.resolve("gpt-4o-turbo", "key-b") == Route("my-openai", "gpt-4o-turbo")
    # legacy passthrough grant: raw model to the bound provider
    assert s.resolve("gpt-4o-mini", "key-a") == Route("my-openai", "gpt-4o-mini")


def test_migration_v1_logged(caplog):
    with caplog.at_level("INFO", logger="headrouter.config"):
        load_config_v2(OLD_JSON)
    assert any("migrat" in r.message.lower() for r in caplog.records)


# --- closed resolution (INV-1) ----------------------------------------------


def test_granted_model_resolves():
    s = make_v3_settings()
    assert s.resolve("gpt-4o-mini", "key-a") == Route("or", "gpt-4o-mini")
    assert s.resolve("gpt-4o", "key-b") == Route("my-openai", "gpt-4o")


def test_ungranted_model_denied():
    s = make_v3_settings()
    with pytest.raises(ModelNotGranted) as ei:
        s.resolve("gpt-4o", "key-a")
    assert ei.value.available == ["gpt-4o-mini"]


def test_raw_provider_model_denied_for_scoped_key():
    s = make_v3_settings()
    with pytest.raises(ModelNotGranted):
        s.resolve("or:gpt-4o-mini", "key-a")


def test_unknown_model_denied_not_fallthrough():
    s = make_v3_settings()
    with pytest.raises(ModelNotGranted) as ei:
        s.resolve("gpt-4o-turbo-preview", "key-a")
    assert ei.value.available == ["gpt-4o-mini"]


def test_admin_key_full_legacy_behavior():
    s = make_v3_settings(
        api_keys=frozenset({"admin"}),
        routes={"envroute": Route("my-openai", "gpt-4o")},
        default_route=Route("or", "gpt-4o-mini"),
    )
    assert s.resolve("envroute", "admin") == Route("my-openai", "gpt-4o")
    assert s.resolve("my-openai:gpt-4o", "admin") == Route("my-openai", "gpt-4o")
    assert s.resolve("anything", "admin") == Route("or", "gpt-4o-mini")
    assert s.resolve("anything", None) == Route("or", "gpt-4o-mini")


# --- single source for /v1/models agreement (INV-2 groundwork) ---------------


def test_models_for_key_scoped_vs_admin():
    s = make_v3_settings(api_keys=frozenset({"admin"}), default_route=Route("or", "gpt-4o-mini"))
    assert s.models_for_key("key-a") == ["gpt-4o-mini"]
    assert s.models_for_key("key-b") == ["gpt-4o", "gpt-4o-mini"]
    assert "default" in s.models_for_key("admin")


# --- A2: key-aware /v1/models + deny error (INV-2, HTTP level) ---------------

import httpx
from app import create_app
from conftest import make_handler


def make_client(captured, **overrides):
    from fastapi.testclient import TestClient
    s = make_v3_settings(api_keys=frozenset({"admin"}), compression_threshold_tokens=100000, compression_prefetch_enabled=False, **overrides)
    app = create_app(
        settings=s,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    return TestClient(app)


def test_v1_models_scoped_to_key(captured):
    with make_client(captured) as c:
        r = c.get("/v1/models", headers={"Authorization": "Bearer key-a"})
        assert r.status_code == 200
        assert [m["id"] for m in r.json()["data"]] == ["gpt-4o-mini"]


def test_v1_models_admin_sees_all(captured):
    with make_client(captured) as c:
        r = c.get("/v1/models", headers={"Authorization": "Bearer admin"})
        assert r.status_code == 200


def test_deny_error_lists_exact_granted_set(captured):
    with make_client(captured) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer key-a"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 404
        msg = r.json()["error"]["message"]
        # INV-2: same set as /v1/models for this key
        models_msg = set(msg.split("Available models: ")[1].split(".")[0].split(", "))
        assert models_msg == {"gpt-4o-mini"}


def test_deny_error_via_proxy_path(captured):
    with make_client(captured) as c:
        r = c.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer key-a"},
            json={"model": "gpt-4o", "input": "hi"},
        )
        assert r.status_code == 404
        assert "not available for this key" in r.json()["error"]["message"]
        assert not captured["requests"]  # never reached upstream


def test_granted_model_still_proxied(captured):
    with make_client(captured) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer key-a"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert captured["requests"][0]["url"] == "https://openrouter.example.com/v1/chat/completions"


# --- wildcard grants ("*" = all models the provider serves) -----------------

WILDCARD_JSON = json.dumps(
    {
        "providers": [
            {"name": "or", "type": "openrouter", "base_url": "https://openrouter.example.com/v1", "api_key": "sk-or"},
            {"name": "local", "type": "ollama", "base_url": "http://localhost:11434/v1"},
        ],
        "keys": [
            {"name": "wild", "api_key": "key-wild", "grants": [{"provider": "or", "models": ["*"]}]},
            {
                "name": "mixed",
                "api_key": "key-mixed",
                "grants": [
                    {"provider": "or", "models": ["gpt-4o-mini"]},
                    {"provider": "local", "models": ["*"]},
                ],
            },
        ],
    }
)


def make_wildcard_settings(**overrides) -> Settings:
    defs, keys = load_config_v2(WILDCARD_JSON)
    return Settings(custom_providers=defs, key_bindings=keys, **overrides)


def test_validate_accepts_wildcard():
    assert validate_config(json.loads(WILDCARD_JSON)) == []


def test_load_wildcard_grant():
    _, keys = load_config_v2(WILDCARD_JSON)
    assert keys["key-wild"].grants == (ModelGrant(provider="or", models=frozenset({"*"})),)


def test_wildcard_resolves_verbatim():
    s = make_wildcard_settings()
    assert s.resolve("gpt-4o", "key-wild") == Route("or", "gpt-4o")
    assert s.resolve("anything-at-all", "key-wild") == Route("or", "anything-at-all")


def test_wildcard_prefers_explicit_grant():
    s = make_wildcard_settings()
    # explicit grant wins over the wildcard for the mixed key
    assert s.resolve("gpt-4o-mini", "key-mixed") == Route("or", "gpt-4o-mini")
    # a non-explicit model falls through to the wildcard provider
    assert s.resolve("llama3", "key-mixed") == Route("local", "llama3")


def test_models_for_key_reports_wildcard_marker():
    s = make_wildcard_settings()
    assert s.models_for_key("key-wild") == ["*"]
    assert s.models_for_key("key-mixed") == ["*", "gpt-4o-mini"]


def test_wildcard_providers_helper():
    s = make_wildcard_settings()
    assert s.wildcard_providers("key-wild") == ["or"]
    assert s.wildcard_providers("key-mixed") == ["local"]
    assert s.wildcard_providers("admin") == []
    assert s.wildcard_providers(None) == []


def test_wildcard_v1_models_fetches_live(captured):
    defs, keys = load_config_v2(WILDCARD_JSON)
    s = Settings(
        api_keys=frozenset({"admin"}),
        custom_providers=defs,
        key_bindings=keys,
        compression_threshold_tokens=100000,
        compression_prefetch_enabled=False,
    )
    from fastapi.testclient import TestClient
    app = create_app(
        settings=s,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    with TestClient(app) as c:
        r = c.get("/v1/models", headers={"Authorization": "Bearer key-wild"})
        assert r.status_code == 200
        ids = [m["id"] for m in r.json()["data"]]
        # live list from the mock provider replaces the "*" marker
        assert "*" not in ids
        assert "gpt-4o-mini" in ids and "gpt-4o" in ids
        # the wildcard grant hit the provider's /models endpoint
        assert any(req["url"] == "https://openrouter.example.com/v1/models" for req in captured["requests"])
