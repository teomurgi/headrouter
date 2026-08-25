"""A4 — admin API (INV-5/6): config get/validate/apply, log ring buffer, provider health."""

import json

import httpx
import pytest

from app import create_app
from conftest import make_handler
from config import Settings
from config_store import ConfigStore

V2 = {
    "providers": [
        {"name": "or", "type": "openrouter", "base_url": "https://or.test/v1", "api_key_env": "OR_KEY"},
    ],
    "aliases": {"fast": "or:gpt-4o-mini"},
    "keys": [{"name": "team-a", "api_key_env": "KEY_A", "aliases": ["fast"]}],
}


def make_client(tmp_path, captured, data=V2, env=None):
    from fastapi.testclient import TestClient

    path = tmp_path / "providers.json"
    path.write_text(json.dumps(data))
    store = ConfigStore(
        path,
        base_settings=Settings(
            compression_threshold_tokens=100000,
            api_keys=frozenset({"test-key"}),
            provider_base_urls={"openai": "https://api.openai.test/v1"},
        ),
        env=env or {"KEY_A": "key-a-val", "KEY_B": "key-b-val", "OR_KEY": "sk-or"},
    )
    app = create_app(
        settings=store.settings,
        config_store=store,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    client = TestClient(app)
    client.__enter__()  # enter lifespan so http_client is set on state
    return client, store


H = {"Authorization": "Bearer test-key"}


def test_get_admin_config_env_names_only(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    r = c.get("/admin/config", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["keys"][0]["api_key_env"] == "KEY_A"
    assert "api_key" not in body["keys"][0]
    assert "api_key" not in {k: v for k, v in body["providers"][0].items() if k != "api_key_env"}


def test_put_admin_config_rejects_secret_values(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    bad = json.loads(json.dumps(V2))
    bad["providers"][0]["api_key"] = "sk-secret"
    r = c.put("/admin/config", headers=H, json=bad)
    assert r.status_code == 400
    assert any("api_key" in e for e in r.json()["detail"]["errors"])


def test_validate_endpoint_no_mutation(tmp_path, captured):
    c, store = make_client(tmp_path, captured)
    staged = json.loads(json.dumps(V2))
    staged["keys"][0]["aliases"] = []
    r = c.post("/admin/config/validate", headers=H, json=staged)
    assert r.status_code == 200
    assert r.json()["valid"] is False
    assert any("alias" in e for e in r.json()["errors"])
    assert json.loads((tmp_path / "providers.json").read_text()) == V2  # untouched
    assert store.settings is not None


def test_apply_endpoint_swaps_and_persists(tmp_path, captured):
    c, store = make_client(tmp_path, captured)
    staged = json.loads(json.dumps(V2))
    staged["aliases"]["smart"] = "or:gpt-4o"
    staged["keys"].append({"name": "team-b", "api_key_env": "KEY_B", "aliases": ["smart"]})
    r = c.put("/admin/config", headers=H, json=staged)
    assert r.status_code == 200
    assert json.loads((tmp_path / "providers.json").read_text()) == staged
    assert "smart" in store.settings.aliases
    # invalid staged config rejected with the same validation body
    bad = json.loads(json.dumps(staged))
    bad["keys"][1]["aliases"] = []
    r = c.put("/admin/config", headers=H, json=bad)
    assert r.status_code == 400
    assert any("alias" in e for e in r.json()["detail"]["errors"])


def test_admin_endpoints_require_gateway_key(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    assert c.get("/admin/config").status_code == 401
    assert c.put("/admin/config", json=V2).status_code == 401


# --- request log ring buffer (INV-6) ------------------------------------------


def make_logged_client(tmp_path, captured):
    # settings with an admin key so requests pass auth; store for apply
    c, store = make_client(tmp_path, captured)
    return c, store


def test_log_records_metadata_after_request(tmp_path, captured):
    c, _ = make_logged_client(tmp_path, captured)
    r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer key-a-val"},
               json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    r = c.get("/admin/log", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["entries"]
    e = body["entries"][0]
    assert set(e) >= {"seq", "ts", "key_name", "alias", "resolved", "status",
                      "latency_ms", "tokens_in", "tokens_out", "compressed", "tokens_saved"}
    assert e["key_name"] == "team-a"
    assert e["alias"] == "fast"
    assert e["resolved"] == "or:gpt-4o-mini"
    assert e["status"] == 200


def test_log_cursor_and_bounded(tmp_path, captured):
    c, _ = make_logged_client(tmp_path, captured)
    first = c.get("/admin/log", headers=H).json()
    after = first["last_seq"]
    r = c.get("/admin/log", params={"after": after}, headers=H)
    assert r.json()["entries"] == []


def test_log_never_contains_bodies_or_key_values(tmp_path, captured):
    c, _ = make_logged_client(tmp_path, captured)
    c.post("/v1/chat/completions", headers={"Authorization": "Bearer key-a-val"},
           json={"model": "fast", "messages": [{"role": "user", "content": "SECRET-PROMPT"}]})
    raw = c.get("/admin/log", headers=H).text
    assert "SECRET-PROMPT" not in raw
    assert "key-a-val" not in raw


# --- provider health -----------------------------------------------------------


def test_provider_health_cached(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    r = c.get("/admin/health/providers", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["providers"][0]["name"] == "or"
    assert "reachable" in body["providers"][0]
    assert "checked_at" in body
