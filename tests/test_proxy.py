import json
import logging

import httpx

from app import create_app
from compression_service import CompressionResult
from config import Route, Settings

from conftest import API_KEY, make_handler


def make_settings(**overrides) -> Settings:
    base = dict(
        api_keys=frozenset({API_KEY}),
        routes={
            "gpt4o": Route("openai", "gpt-4o"),
            "sonnet": Route("anthropic", "claude-3-5-sonnet-latest"),
        },
        default_route=Route("openai", "gpt-4o-mini"),
        compression_threshold_tokens=100000,
        compression_prefetch_enabled=False,
        provider_base_urls={
            "openai": "https://api.openai.test/v1",
            "openrouter": "https://openrouter.test/v1",
            "ollama": "http://ollama.test/v1",
            "anthropic": "https://anthropic.test",
            "gemini": "https://gemini.test",
        },
        provider_api_keys={"openai": "sk-openai", "anthropic": "sk-ant", "gemini": "goog"},
    )
    base.update(overrides)
    return Settings(**base)


def test_embeddings_forwarded_with_model_resolution(captured):
    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt4o", "input": "hello"},
        )
    assert r.status_code == 200
    assert r.json()["object"] == "list"
    sent = captured["requests"][0]
    # /v1 prefix stripped for openai-compat providers (base URL already has it)
    assert sent["url"] == "https://api.openai.test/v1/embeddings"
    assert sent["body"]["model"] == "gpt-4o"
    # client gateway key must not leak upstream; provider key injected
    assert sent["headers"]["authorization"] == "Bearer sk-openai"


def test_anthropic_path_not_stripped(captured):
    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        c.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]},
        )
    sent = captured["requests"][0]
    assert sent["url"] == "https://anthropic.test/v1/messages"
    assert sent["headers"]["x-api-key"] == "sk-ant"
    assert "authorization" not in sent["headers"]


def test_anthropic_messages_uses_shared_compression(captured, caplog):
    long_text = "x" * (70 * 1024)
    compressed_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "compressed"},
                {"type": "tool_result", "tool_use_id": "tool-1", "content": "sunny"},
            ],
        }
    ]

    class FakeCompression:
        def __init__(self):
            self.calls = []

        def maybe_compress(self, messages, model):
            self.calls.append((messages, model))
            return CompressionResult(
                messages=compressed_messages,
                applied=True,
                tokens_before=18000,
                tokens_after=6000,
                engine="headroom",
                transforms_applied=["summarize"],
            )

    compression = FakeCompression()
    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    app.state.compression = compression
    from fastapi.testclient import TestClient

    with caplog.at_level(logging.INFO, logger="headrouter.proxy"):
        with TestClient(app) as c:
            response = c.post(
                "/v1/messages?beta=true",
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={
                    "model": "sonnet",
                    "system": "Keep this system prompt unchanged.",
                    "max_tokens": 128,
                    "tools": [{"name": "weather", "input_schema": {"type": "object"}}],
                    "messages": [{"role": "user", "content": long_text}],
                },
            )

    assert response.status_code == 200
    assert response.headers["x-compression-applied"] == "true"
    assert compression.calls == [
        ([{"role": "user", "content": long_text}], "claude-3-5-sonnet-latest")
    ]
    sent = captured["requests"][0]["body"]
    assert sent["model"] == "claude-3-5-sonnet-latest"
    assert sent["messages"] == compressed_messages
    assert sent["system"] == "Keep this system prompt unchanged."
    assert sent["max_tokens"] == 128
    assert sent["tools"] == [{"name": "weather", "input_schema": {"type": "object"}}]
    assert "original_tokens=18000 compressed_tokens=6000" in caplog.text
    assert "compression_ratio=3.000 savings_pct=66.7" in caplog.text


def test_get_request_uses_default_route(captured):
    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/v1/anything", headers={"Authorization": f"Bearer {API_KEY}"}, params={"a": "1"})
    assert r.status_code == 404  # mocked handler returns 404 for unknown, but it was forwarded
    sent = captured["requests"][0]
    assert sent["url"] == "https://api.openai.test/v1/anything?a=1"
    assert sent["headers"]["authorization"] == "Bearer sk-openai"


def test_upstream_error_body_is_logged(caplog):
    def error_handler(request):
        return httpx.Response(
            400,
            json={"error": {"message": "max_tokens must be greater than zero"}},
            headers={"x-request-id": "req-debug-123"},
        )

    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(error_handler)),
    )
    from fastapi.testclient import TestClient

    with caplog.at_level(logging.WARNING, logger="headrouter.proxy"):
        with TestClient(app) as c:
            r = c.post(
                "/v1/messages?beta=true",
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={"model": "sonnet", "messages": []},
            )

    assert r.status_code == 400
    assert "provider=anthropic status=400" in caplog.text
    assert "request_id=req-debug-123" in caplog.text
    assert "max_tokens must be greater than zero" in caplog.text
    assert API_KEY not in caplog.text


def test_no_provider_resolvable_404(captured):
    settings = make_settings(default_route=None)
    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/v1/whatever", headers={"Authorization": f"Bearer {API_KEY}"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "no_provider"


def test_streaming_passthrough(captured):
    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        with c.stream(
            "POST",
            "/v1/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt4o", "prompt": "hi", "stream": True},
        ) as r:
            assert r.status_code == 200
            lines = [line for line in r.iter_lines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"


def test_chat_completions_still_handled_by_dedicated_route(captured):
    app = create_app(
        settings=make_settings(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt4o", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert r.headers["x-compression-applied"] == "false"
