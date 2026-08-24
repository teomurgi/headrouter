import json
import logging

import httpx

from app import create_app
from conftest import auth_headers, make_handler


def test_health_no_auth_required(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["compression"]["strategy"] == "coding"


def test_models_list(client):
    r = client.get("/v1/models", headers=auth_headers())
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert set(["gpt4o", "sonnet", "gem", "default"]) <= set(ids)


def test_auth_missing_key(client):
    r = client.post("/v1/chat/completions", json={"model": "gpt4o", "messages": []})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


def test_auth_invalid_key(client):
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong"},
        json={"model": "gpt4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


def test_auth_x_api_key(client):
    r = client.post(
        "/v1/chat/completions",
        headers={"x-api-key": "test-key"},
        json={"model": "gpt4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200


def test_validation_error_is_logged_without_request_content(client, caplog):
    secret_content = "do-not-log-this-message"

    with caplog.at_level(logging.WARNING, logger="headrouter"):
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers(),
            json={"model": "gpt4o", "messages": secret_content},
        )

    assert response.status_code == 422
    assert "request validation error" in caplog.text
    assert "messages" in caplog.text
    assert secret_content not in caplog.text


def test_unknown_model_404(settings, captured):
    settings.default_route = None
    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers=auth_headers(),
            json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


def test_openai_nonstream(client, captured):
    r = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={
            "model": "gpt4o",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hello"},
            ],
            "temperature": 0.2,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["content"] == "Hello!"
    assert data["usage"]["prompt_tokens"] == 10

    sent = captured["requests"][0]
    assert sent["url"] == "https://api.openai.test/v1/chat/completions"
    assert sent["body"]["model"] == "gpt-4o"
    assert sent["body"]["temperature"] == 0.2
    assert sent["body"]["messages"][0]["role"] == "system"


def test_default_route_passthrough(client, captured):
    r = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"model": "anything-at-all", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert captured["requests"][0]["body"]["model"] == "gpt-4o-mini"


def test_provider_colon_model_syntax(client, captured):
    r = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"model": "anthropic:claude-x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert captured["requests"][0]["url"] == "https://anthropic.test/v1/messages"
    assert captured["requests"][0]["body"]["model"] == "claude-x"


def test_openai_stream(client):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"model": "gpt4o", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in r.iter_lines() if line.startswith("data: ")]
    payloads = [json.loads(line[6:]) for line in lines[:-1]]
    assert lines[-1] == "data: [DONE]"
    text = "".join(
        c["choices"][0]["delta"].get("content", "") for c in payloads if c.get("choices")
    )
    assert text == "Hello"
    assert payloads[0]["choices"][0]["delta"].get("role") == "assistant"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
