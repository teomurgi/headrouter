"""A3 — atomic config lifecycle (INV-4) + migrated-grant provenance."""

import asyncio
import json

import pytest

from config import Route, Settings
from config_store import ConfigStore

V2 = {
    "providers": [
        {"name": "or", "type": "openrouter", "base_url": "https://or.test/v1", "api_key_env": "OR_KEY"},
    ],
    "aliases": {"fast": "or:gpt-4o-mini"},
    "keys": [{"name": "team-a", "api_key": "key-a", "aliases": ["fast"]}],
}

V2_B = {
    "providers": [
        {"name": "or", "type": "openrouter", "base_url": "https://or.test/v1", "api_key_env": "OR_KEY"},
    ],
    "aliases": {"fast": "or:gpt-4o-mini", "smart": "or:gpt-4o"},
    "keys": [
        {"name": "team-a", "api_key": "key-a", "aliases": ["fast"]},
        {"name": "team-b", "api_key": "key-b", "aliases": ["smart"]},
    ],
}

OLD = {
    "providers": [
        {"name": "or", "type": "openrouter", "base_url": "https://or.test/v1", "api_key_env": "OR_KEY"},
    ],
    "keys": [{"api_key": "legacy-key", "provider": "or"}],
}


ENV = {"OR_KEY": "sk-or"}


def make_store(tmp_path, data=V2) -> ConfigStore:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(data))
    return ConfigStore(path, base_settings=Settings(compression_threshold_tokens=100000), env=ENV)


def test_load_initial(tmp_path):
    store = make_store(tmp_path)
    assert store.settings.aliases == {"fast": Route("or", "gpt-4o-mini")}
    assert "key-a" in store.settings.key_bindings


def test_apply_swaps_settings_atomically(tmp_path):
    store = make_store(tmp_path)
    old = store.settings
    store.apply(V2_B)
    assert store.settings is not old
    assert set(store.settings.key_bindings) == {"key-a", "key-b"}
    # in-flight: previously captured snapshot unchanged
    assert set(old.key_bindings) == {"key-a"}


def test_apply_persists_v2_shape_atomically(tmp_path):
    store = make_store(tmp_path, OLD)
    store.apply(V2_B)
    on_disk = json.loads((tmp_path / "providers.json").read_text())
    assert on_disk == V2_B  # v2 shape written, never the migrated-from v1
    assert not list(tmp_path.glob("*.tmp*"))  # no temp litter


def test_invalid_apply_rejected_no_mutation(tmp_path):
    store = make_store(tmp_path)
    before = store.settings
    bad = json.loads(json.dumps(V2_B))
    bad["keys"][0]["aliases"] = []
    with pytest.raises(Exception, match="alias"):
        store.apply(bad)
    assert store.settings is before
    assert json.loads((tmp_path / "providers.json").read_text()) == V2


def test_migrated_grants_marked_not_blessed(tmp_path):
    store = make_store(tmp_path, OLD)
    # provenance survives into the store: migrated keys are named
    assert store.migrated_keys == {"legacy-key"}
    # and a subsequent apply of a hand-edited config clears it
    store.apply(V2_B)
    assert store.migrated_keys == set()


def test_concurrent_apply_serialized(tmp_path):
    store = make_store(tmp_path)
    async def race():
        await asyncio.gather(store.apply_async(V2_B), store.apply_async(V2))
    asyncio.run(race())
    # last writer wins cleanly; disk holds exactly one of the two configs
    on_disk = json.loads((tmp_path / "providers.json").read_text())
    assert on_disk in (V2, V2_B)
    assert store.settings.aliases  # coherent snapshot either way


# --- live auth after apply (AuthMiddleware must read current settings) -------

def test_new_key_authenticates_after_apply(tmp_path, captured):
    import httpx
    from app import create_app
    from fastapi.testclient import TestClient
    from conftest import make_handler

    store = make_store(tmp_path, V2)
    app = create_app(
        settings=store.settings,
        config_store=store,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    with TestClient(app) as c:
        # key-b not yet known -> 401
        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer key-b"},
                   json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 401
        store.apply(V2_B)
        # after apply, key-b authenticates and routes (no app restart)
        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer key-b"},
                   json={"model": "smart", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
