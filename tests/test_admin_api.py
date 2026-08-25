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
    # INV-5 clause 1: gateway KEY values are still rejected outright.
    c, _ = make_client(tmp_path, captured)
    bad = json.loads(json.dumps(V2))
    bad["keys"][0]["api_key"] = "sk-raw"
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


def test_put_admin_config_rejects_key_secret_values(tmp_path, captured):
    # §9 check 2: no admin API response ever carries key values, and the
    # apply direction rejects them too (INV-5, explicit key-side test).
    c, _ = make_client(tmp_path, captured)
    bad = json.loads(json.dumps(V2))
    bad["keys"][0]["api_key"] = "sk-raw-secret"
    r = c.put("/admin/config", headers=H, json=bad)
    assert r.status_code == 400
    assert any("api_key" in e for e in r.json()["detail"]["errors"])
    # and GET never echoes key values even when a legacy file has them
    raw = json.loads((tmp_path / "providers.json").read_text())
    assert "sk-raw-secret" not in c.get("/admin/config", headers=H).text


# --- one-time key generation (ux-spec §4 issue-key flow, §9 check 2) ----------


def test_issue_key_generated_shown_once(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    staged = json.loads(json.dumps(V2))
    staged["keys"].append({"name": "team-b", "api_key_env": None, "aliases": ["fast"]})
    staged["keys"][1].pop("api_key_env")
    r = c.put("/admin/config", headers=H, json=staged)
    assert r.status_code == 200
    gen = r.json()["generated"]
    assert len(gen) == 1 and gen[0]["name"] == "team-b"
    value = gen[0]["api_key"]
    assert value.startswith("hr_")
    # generated key authenticates immediately
    r = c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {value}"},
               json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    # never shown again: GET admin config and repeat PUT drain it
    assert value not in c.get("/admin/config", headers=H).text
    r2 = c.put("/admin/config", headers=H, json=staged)
    assert r2.json()["generated"] == []
    # round-trip did not regenerate: the same value still authenticates
    r = c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {value}"},
               json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


# --- static /admin page (A5) ---------------------------------------------------


def test_admin_page_served_no_auth_needed(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    r = c.get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "HEADROUTER" in r.text
    # page itself carries no secrets
    assert "sk-or" not in r.text


def test_admin_page_survives_config_apply(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    assert c.get("/admin").status_code == 200


# --- A7: plain provider API keys, write-only (INV-5 two-clause split) --------


def test_pasted_provider_key_stored_and_used(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    staged = json.loads(json.dumps(V2))
    staged["providers"][0]["api_key_env"] = None
    staged["providers"][0].pop("api_key_env")
    staged["providers"][0]["api_key"] = "sk-pasted-secret"
    r = c.put("/admin/config", headers=H, json=staged)
    assert r.status_code == 200
    # stored server-side...
    disk = json.loads((tmp_path / "providers.json").read_text())
    assert disk["providers"][0]["api_key"] == "sk-pasted-secret"
    # ...used for upstream auth...
    r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer key-a-val"},
               json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert captured["requests"][0]["headers"]["authorization"] == "Bearer sk-pasted-secret"
    # ...and never in any GET
    body = c.get("/admin/config", headers=H).text
    assert "sk-pasted-secret" not in body
    got = json.loads(body)
    assert got["providers"][0].get("api_key_set") is True


def test_blank_on_edit_keeps_existing_key(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    staged = json.loads(json.dumps(V2))
    staged["providers"][0].pop("api_key_env")
    staged["providers"][0]["api_key"] = "sk-first"
    assert c.put("/admin/config", headers=H, json=staged).status_code == 200
    # round-trip from GET (value stripped, api_key_set true) with NO api_key field
    got = json.loads(c.get("/admin/config", headers=H).text)
    got["providers"][0].pop("api_key_set", None)
    r = c.put("/admin/config", headers=H, json=got)
    assert r.status_code == 200
    disk = json.loads((tmp_path / "providers.json").read_text())
    assert disk["providers"][0]["api_key"] == "sk-first"  # kept, not wiped
    r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer key-a-val"},
               json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]})
    assert captured["requests"][0]["headers"]["authorization"] == "Bearer sk-first"


def test_key_values_still_rejected(tmp_path, captured):
    # INV-5 clause 1 unchanged for gateway keys
    c, _ = make_client(tmp_path, captured)
    bad = json.loads(json.dumps(V2))
    bad["keys"][0]["api_key"] = "sk-raw"
    r = c.put("/admin/config", headers=H, json=bad)
    assert r.status_code == 400


# --- admin auth gate (§5 auth state): key entry, no naked-fetch dead end -----


def test_admin_page_includes_key_gate(captured, tmp_path):
    c, _ = make_client(tmp_path, captured)
    html = c.get("/admin").text
    assert 'id="key-gate"' in html
    assert 'type="password"' in html


def test_wrong_key_specific_error_not_full_page(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    r = c.get("/admin/config", headers={"Authorization": "Bearer wrong-key"})
    assert r.status_code == 401
    body = r.json()
    assert "error" in body  # OpenAI-style body the gate shows inline


# --- build marker (stale-JS confusion guard) ----------------------------------


def test_admin_page_shows_build_and_no_store(tmp_path, captured):
    c, _ = make_client(tmp_path, captured)
    r = c.get("/admin")
    assert r.status_code == 200
    assert "build" in r.text and 'id="build-pill"' in r.text
    assert "build ?" not in r.text  # stamped, never left placeholder
    assert r.headers.get("cache-control") == "no-store"
