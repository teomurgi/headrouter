"""Regression tests for GitHub issues #1-#4."""

import json

import httpx
import pytest

from app import create_app
from config import Route, Settings

from conftest import API_KEY, make_handler

AUTH = {"Authorization": f"Bearer {API_KEY}"}


def make_settings(**overrides) -> Settings:
    base = dict(
        api_keys=frozenset({API_KEY}),
        routes={"gpt4o": Route("openai", "gpt-4o")},
        default_route=Route("openai", "gpt-4o-mini"),
        compression_threshold_tokens=100000,
        provider_base_urls={
            "openai": "https://api.openai.test/v1",
            "anthropic": "https://anthropic.test",
        },
        provider_api_keys={"openai": "sk-openai"},
    )
    base.update(overrides)
    return Settings(**base)


def make_client(settings, captured):
    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    return TestClient(app)


# --- Issue #4: trailing-slash base URLs -------------------------------------


@pytest.mark.parametrize("trailing", ["", "/"])
def test_issue4_trailing_slash_base_url(trailing, captured):
    settings = make_settings(provider_base_urls={"openai": f"https://api.openai.test/v1{trailing}"})
    with make_client(settings, captured) as c:
        r = c.post("/v1/embeddings", headers=AUTH, json={"model": "gpt4o", "input": "x"})
    assert r.status_code == 200
    assert captured["requests"][0]["url"] == "https://api.openai.test/v1/embeddings"


def test_issue4_env_base_url_normalized():
    settings = Settings.from_env(
        {
            "OPENAI_BASE_URL": "https://proxy.example/v1/",
            "GATEWAY_ROUTES": "g=gpt-4o",
        }
    )
    assert settings.provider_base_urls["openai"] == "https://proxy.example/v1"


# --- Issue #3: duplicate response headers -----------------------------------


def test_issue3_duplicate_set_cookie_preserved(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[("Set-Cookie", "a=1; Path=/"), ("Set-Cookie", "b=2; Path=/")],
            json={"ok": True},
        )

    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/v1/session", headers=AUTH)
    cookies = r.headers.get_list("set-cookie")
    assert cookies == ["a=1; Path=/", "b=2; Path=/"]


# --- Issue #2: bodies on DELETE and other methods ---------------------------


def test_issue2_delete_json_body_preserved(captured):
    with make_client(make_settings(), captured) as c:
        r = c.request(
            "DELETE",
            "/v1/resources/123",
            headers=AUTH,
            json={"model": "gpt4o", "reason": "cleanup"},
        )
    assert r.status_code == 404  # mock upstream 404s unknown paths; forwarding is what matters
    sent = captured["requests"][0]
    assert sent["url"].endswith("/v1/resources/123")
    # body preserved (with model rewriting intact)
    assert sent["body"]["reason"] == "cleanup"
    assert sent["body"]["model"] == "gpt-4o"


def test_issue2_delete_binary_body_preserved(captured):
    blob = b"\x00\x01\x02binary-delete-payload"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"len": len(request.content)})

    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.request(
            "DELETE",
            "/v1/files/abc",
            headers={**AUTH, "Content-Type": "application/octet-stream"},
            content=blob,
        )
    assert r.status_code == 200
    assert r.json()["len"] == len(blob)


def test_issue2_bodyless_get_stays_bodyless(captured):
    with make_client(make_settings(), captured) as c:
        c.get("/v1/models/xyz", headers=AUTH)
    sent = captured["requests"][0]
    assert sent["body"] == {}


# --- Issue #1: large uploads are streamed, not buffered ---------------------


def test_issue1_large_binary_upload_forwarded(captured):
    payload = b"x" * (1_000_000)  # 1 MB, well over the peek limit
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["len"] = len(request.content)
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"stored": True})

    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(
            "/v1/files",
            headers={**AUTH, "Content-Type": "application/octet-stream"},
            content=payload,
        )
    assert r.status_code == 200
    assert seen["len"] == len(payload)
    assert seen["content_type"] == "application/octet-stream"


def test_issue1_small_json_still_routes_by_model(captured):
    with make_client(make_settings(), captured) as c:
        r = c.post(
            "/v1/embeddings",
            headers={**AUTH, "Content-Type": "application/json"},
            json={"model": "gpt4o", "input": "hello"},
        )
    assert r.status_code == 200
    sent = captured["requests"][0]
    assert sent["url"] == "https://api.openai.test/v1/embeddings"
    assert sent["body"]["model"] == "gpt-4o"


def test_issue1_large_json_streamed_with_fallback_resolution(captured):
    # Large JSON body: streamed without model rewriting; provider from default route.
    big = json.dumps({"model": "gpt4o", "data": "y" * 200_000})

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body.get("model"), "len": len(request.content)})

    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(
            "/v1/batch",
            headers={**AUTH, "Content-Type": "application/json"},
            content=big.encode(),
        )
    assert r.status_code == 200
    # model alias is preserved (not rewritten) but the request still went through
    assert r.json()["len"] == len(big.encode())
