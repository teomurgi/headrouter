from __future__ import annotations

import json

import httpx
import pytest

from app import create_app
from config import Route, Settings

API_KEY = "test-key"


@pytest.fixture
def captured():
    return {"requests": []}


def _openai_response(content="Hello!", model="gpt-4o", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "created": 1720000000,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


async def _openai_stream_chunks():
    chunks = [
        {"id": "chatcmpl-123", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": "chatcmpl-123", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]},
        {"id": "chatcmpl-123", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]},
        {"id": "chatcmpl-123", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    for c in chunks:
        yield (f"data: {json.dumps(c)}\n\n").encode()
    yield b"data: [DONE]\n\n"


def _anthropic_response():
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-sonnet-latest",
        "content": [{"type": "text", "text": "Hi from Claude"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 8, "output_tokens": 4},
    }


async def _anthropic_stream():
    events = [
        ("message_start", {"type": "message_start", "message": {"role": "assistant"}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " there"}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    for event, obj in events:
        yield (f"event: {event}\ndata: {json.dumps(obj)}\n\n").encode()


def _gemini_response():
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "Hi from Gemini"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 3, "totalTokenCount": 9},
        "modelVersion": "gemini-1.5-pro",
    }


async def _gemini_stream():
    parts = [
        {"candidates": [{"content": {"parts": [{"text": "Hel"}]}, "finishReason": None}]},
        {"candidates": [{"content": {"parts": [{"text": "lo"}]}, "finishReason": "STOP"}]},
    ]
    for p in parts:
        yield (f"data: {json.dumps(p)}\n\n").encode()


def make_handler(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["requests"].append(
            {"url": str(request.url), "body": json.loads(request.content.decode())}
        )
        url = str(request.url)
        request_body = json.loads(request.content.decode()) if request.content else {}
        if ":streamGenerateContent" in url:
            return httpx.Response(200, content=_gemini_stream())
        if url.endswith(":generateContent"):
            return httpx.Response(200, json=_gemini_response())
        if url.endswith("/v1/messages"):
            if request_body.get("stream"):
                return httpx.Response(200, content=_anthropic_stream())
            return httpx.Response(200, json=_anthropic_response())
        if url.endswith("/chat/completions"):
            if request_body.get("stream"):
                return httpx.Response(200, content=_openai_stream_chunks())
            return httpx.Response(200, json=_openai_response())
        return httpx.Response(404, json={"error": "not found"})

    return handler


@pytest.fixture
def settings():
    return Settings(
        api_keys=frozenset({API_KEY}),
        routes={
            "gpt4o": Route("openai", "gpt-4o"),
            "sonnet": Route("anthropic", "claude-3-5-sonnet-latest"),
            "gem": Route("gemini", "gemini-1.5-pro"),
        },
        default_route=Route("openai", "gpt-4o-mini"),
        provider_base_urls={
            "openai": "https://api.openai.test/v1",
            "openrouter": "https://openrouter.test/v1",
            "ollama": "http://ollama.test/v1",
            "anthropic": "https://anthropic.test",
            "gemini": "https://gemini.test",
        },
        provider_api_keys={"openai": "sk-openai", "anthropic": "sk-ant", "gemini": "goog"},
        compression_threshold_tokens=100000,  # no compression during routing tests
    )


@pytest.fixture
def client(settings, captured):
    app = create_app(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(make_handler(captured))),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def auth_headers():
    return {"Authorization": f"Bearer {API_KEY}"}
