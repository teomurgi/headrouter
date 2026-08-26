import json
import logging
from dataclasses import replace

import httpx
import pytest

from app import create_app
from compression_service import CompressionResult, CompressionService
from config import Route, Settings

from conftest import API_KEY, make_handler


def test_estimate_tokens_fallback():
    messages = [{"role": "user", "content": "x" * 400}]
    svc = CompressionService(enabled=False)
    tokens = svc._count(messages, "gpt-4o")
    assert tokens >= 50


@pytest.mark.asyncio
async def test_compression_disabled_passthrough():
    svc = CompressionService(enabled=False, threshold_tokens=0)
    messages = [{"role": "user", "content": "hi"}]
    result = await svc.maybe_compress(messages)
    assert result.messages is messages
    assert not result.applied


def test_compression_prefetch_starts_only_when_enabled(monkeypatch):
    model_calls = []
    tokenizer_calls = []

    monkeypatch.setattr(
        "headroom.transforms.kompress_compressor.prefetch_kompress_artifacts",
        lambda: model_calls.append(True) or True,
    )
    monkeypatch.setattr(
        "huggingface_hub.hf_hub_download",
        lambda **kwargs: tokenizer_calls.append(kwargs["filename"]),
    )

    assert CompressionService(enabled=True).prefetch()
    assert not CompressionService(enabled=False).prefetch()
    assert model_calls == [True]
    assert tokenizer_calls == [
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
    ]


def test_compression_prefetch_runs_on_startup(settings, captured, monkeypatch):
    calls = []
    settings = replace(settings, compression_prefetch_enabled=True)
    monkeypatch.setattr(
        CompressionService,
        "prefetch",
        lambda self: calls.append(self.strategy) or True,
    )
    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app):
        pass

    assert calls == ["coding"]


@pytest.mark.asyncio
async def test_compression_below_threshold():
    svc = CompressionService(enabled=True, threshold_tokens=100000)
    messages = [{"role": "user", "content": "hi"}]
    result = await svc.maybe_compress(messages)
    assert not result.applied
    assert result.messages is messages


@pytest.mark.asyncio
async def test_compression_uses_headroom_pipeline_when_available():
    svc = CompressionService(enabled=True, threshold_tokens=1)
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]

    class FakeResult:
        messages = [{"role": "user", "content": "ok"}]
        tokens_after = 1
        tokens_before = 10
        transforms_applied = ["router:mixed:0.5"]

    class FakePipeline:
        def apply(self, msgs, model, model_limit=None, **kwargs):
            assert model
            return FakeResult()

    svc._pipeline_checked = True
    svc._pipeline = FakePipeline()
    result = await svc.maybe_compress(messages)
    assert result.applied
    assert result.engine == "headroom"
    assert result.messages == [{"role": "user", "content": "ok"}]
    assert result.tokens_after < result.tokens_before
    assert result.transforms_applied == ["router:mixed:0.5"]


@pytest.mark.asyncio
async def test_compression_never_grows_context():
    svc = CompressionService(enabled=True, threshold_tokens=1)

    class BadResult:
        messages = [{"role": "user", "content": "way longer than before"}]
        tokens_after = 999999
        tokens_before = 1

    class BadPipeline:
        def apply(self, msgs, model, model_limit=None, **kwargs):
            return BadResult()

    svc._pipeline_checked = True
    svc._pipeline = BadPipeline()
    messages = [{"role": "user", "content": "hi"}]
    result = await svc.maybe_compress(messages)
    assert not result.applied
    assert result.messages is messages


@pytest.mark.asyncio
async def test_compression_pipeline_exception_passthrough():
    svc = CompressionService(enabled=True, threshold_tokens=1)

    class BoomPipeline:
        def apply(self, msgs, model, model_limit=None, **kwargs):
            raise RuntimeError("boom")

    svc._pipeline_checked = True
    svc._pipeline = BoomPipeline()
    messages = [{"role": "user", "content": "hello world"}]
    result = await svc.maybe_compress(messages)
    assert not result.applied
    assert result.messages is messages


def test_coding_strategy_configures_headroom_router():
    service = CompressionService(strategy="coding")
    pipeline = service._get_pipeline()
    assert pipeline is not None
    router_config = pipeline.transforms[0].config
    assert not router_config.force_kompress_all
    assert router_config.protect_recent_code == 0
    assert router_config.protect_error_outputs
    assert router_config.min_chars_for_block_compression == 25
    assert service._compress_user_messages
    assert not service._compress_system_messages


@pytest.mark.asyncio
async def test_compression_with_real_headroom():
    import json as _json

    svc = CompressionService(enabled=True, threshold_tokens=4000)
    if svc._get_pipeline() is None:
        pytest.skip("headroom pipeline unavailable")
    big = _json.dumps(
        [{"id": i, "name": f"user{i}", "email": f"u{i}@example.com"} for i in range(300)],
        indent=2,
    )
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": big},
    ]
    result = await svc.maybe_compress(messages, model="gpt-4o")
    assert result.applied
    assert result.tokens_after < result.tokens_before
    assert result.tokens_saved > 0


def test_metrics_endpoint_tracks_requests(client):
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"model": "gpt4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    r = client.get("/metrics")
    assert r.status_code == 200
    text = r.text
    assert "gateway_requests_total" in text
    assert 'provider="openai",status="200"' in text
    assert "gateway_input_tokens_total 10" in text
    assert "gateway_compression_tokens_saved_total" in text


def test_compression_metrics_recorded(settings, captured):
    settings = replace(settings, compression_threshold_tokens=1)
    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt4o", "messages": [{"role": "user", "content": "hello"}]},
        )
        r = c.get("/metrics")
        assert "gateway_compression_attempts_total 1" in r.text


def test_compression_result_is_always_logged(client, caplog):
    class FakeCompression:
        async def maybe_compress(self, messages, model):
            return CompressionResult(
                messages=messages,
                applied=True,
                tokens_before=120,
                tokens_after=40,
                engine="headroom",
                transforms_applied=["code", "summarize"],
            )

    client.app.state.compression = FakeCompression()
    with caplog.at_level(logging.INFO, logger="headrouter.chat"):
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt4o", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    assert "original_tokens=120 compressed_tokens=40 tokens_saved=80" in caplog.text
    assert "compression_ratio=3.000 savings_pct=66.7" in caplog.text
    assert "applied=True engine=headroom" in caplog.text
    assert "transforms=code,summarize" in caplog.text


def test_compression_noop_result_is_logged(client, caplog):
    class FakeCompression:
        async def maybe_compress(self, messages, model):
            return CompressionResult(
                messages=messages,
                applied=False,
                tokens_before=25,
                tokens_after=25,
            )

    client.app.state.compression = FakeCompression()
    with caplog.at_level(logging.INFO, logger="headrouter.chat"):
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt4o", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    assert "applied=False engine=none" in caplog.text
    assert "original_tokens=25 compressed_tokens=25 tokens_saved=0" in caplog.text
    assert "compression_ratio=1.000 savings_pct=0.0" in caplog.text


def test_unprefixed_chat_completions_uses_compression_handler(client, caplog):
    with caplog.at_level(logging.INFO, logger="headrouter.chat"):
        response = client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt4o", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    assert response.headers["x-compression-applied"] == "false"
    assert "compression result model=gpt-4o" in caplog.text
    assert "original_tokens=" in caplog.text
    assert "compressed_tokens=" in caplog.text
    assert "compression_ratio=" in caplog.text


def test_upstream_error_propagates_status(settings, captured, caplog):
    def error_handler(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(error_handler)),
    )
    from fastapi.testclient import TestClient

    with caplog.at_level("WARNING", logger="headrouter.chat"):
        with TestClient(app) as c:
            r = c.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={"model": "gpt4o", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert r.status_code == 429
            assert "rate limited" in r.json()["error"]["message"]

    assert "provider=openai model=gpt-4o status=429" in caplog.text
    assert "rate limited" in caplog.text
    assert API_KEY not in caplog.text


def test_connection_failure_returns_502(settings):
    def flaky_handler(request):
        raise httpx.ConnectError("connection refused")

    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(flaky_handler)),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 502
