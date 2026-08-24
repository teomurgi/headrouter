import json

import httpx
import pytest

from app import create_app
from compression import CompressionService
from config import Route, Settings

from conftest import API_KEY, make_handler


def test_estimate_tokens_fallback():
    messages = [{"role": "user", "content": "x" * 400}]
    svc = CompressionService(enabled=False)
    tokens = svc._count(messages, "gpt-4o")
    assert tokens >= 50


def test_compression_disabled_passthrough():
    svc = CompressionService(enabled=False, threshold_tokens=0)
    messages = [{"role": "user", "content": "hi"}]
    result = svc.maybe_compress(messages)
    assert result.messages is messages
    assert not result.applied


def test_compression_below_threshold():
    svc = CompressionService(enabled=True, threshold_tokens=100000)
    messages = [{"role": "user", "content": "hi"}]
    result = svc.maybe_compress(messages)
    assert not result.applied
    assert result.messages is messages


def test_compression_headroom_unavailable_falls_back():
    svc = CompressionService(enabled=True, threshold_tokens=1)
    messages = [{"role": "user", "content": "hello world, this is a longer message"}]
    # Force pipeline check to find nothing
    svc._pipeline_checked = True
    svc._pipeline = None
    result = svc.maybe_compress(messages)
    assert not result.applied
    assert result.messages is messages
    assert result.tokens_before > 0
    assert result.tokens_after == result.tokens_before


def test_compression_uses_headroom_pipeline_when_available():
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
    result = svc.maybe_compress(messages)
    assert result.applied
    assert result.engine == "headroom"
    assert result.messages == [{"role": "user", "content": "ok"}]
    assert result.tokens_after < result.tokens_before
    assert result.transforms_applied == ["router:mixed:0.5"]


def test_compression_never_grows_context():
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
    result = svc.maybe_compress(messages)
    assert not result.applied
    assert result.messages is messages


def test_compression_pipeline_exception_passthrough():
    svc = CompressionService(enabled=True, threshold_tokens=1)

    class BoomPipeline:
        def apply(self, msgs, model, model_limit=None, **kwargs):
            raise RuntimeError("boom")

    svc._pipeline_checked = True
    svc._pipeline = BoomPipeline()
    messages = [{"role": "user", "content": "hello world"}]
    result = svc.maybe_compress(messages)
    assert not result.applied
    assert result.messages is messages


def test_compression_with_real_headroom():
    pytest.importorskip("headroom")
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
    result = svc.maybe_compress(messages, model="gpt-4o")
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
    settings.compression_threshold_tokens = 1
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


def test_upstream_error_propagates_status(settings, captured):
    def error_handler(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(error_handler)),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"model": "gpt4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 429
        assert "rate limited" in r.json()["error"]["message"]


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
