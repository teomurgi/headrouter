"""Anthropic adapter: converts OpenAI chat format to/from the Anthropic Messages API."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from .base import AdapterError, BaseAdapter, error_body
from .sse import sse_events

ANTHROPIC_VERSION = "2023-06-01"
STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def _json_loads(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _user_blocks(content: Any) -> Any:
    """Convert OpenAI user content (str or parts) into Anthropic content blocks."""
    if isinstance(content, str) or content is None:
        return content or ""
    if not isinstance(content, list):
        return str(content)
    blocks = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                header, _, data = url.partition(",")
                media_type = header[5:].split(";")[0] if header.startswith("data:") else "image/png"
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    }
                )
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks or ""


def _usage_chunk(chunk_id: str, created: int, model: str, usage: dict) -> bytes:
    """A trailing OpenAI-style chunk carrying token usage, sent just before [DONE]."""
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    return (
        "data: "
        + json.dumps(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
            separators=(",", ":"),
        )
        + "\n\n"
    ).encode()


def to_anthropic_request(body: dict) -> dict:
    """Build an Anthropic /v1/messages payload from an OpenAI-format body."""
    system_parts: list[str] = []
    messages: list[dict] = []

    for msg in body.get("messages", []):
        role = msg.get("role")
        if role == "system" or role == "developer":
            text = _content_text(msg.get("content"))
            if text:
                system_parts.append(text)
        elif role == "user":
            messages.append({"role": "user", "content": _user_blocks(msg.get("content"))})
        elif role == "assistant":
            blocks = []
            text = _content_text(msg.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "name": fn.get("name", ""),
                        "input": _json_loads(fn.get("arguments")),
                    }
                )
            messages.append({"role": "assistant", "content": blocks or ""})
        elif role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id", ""),
                            "content": _content_text(msg.get("content")),
                        }
                    ],
                }
            )

    # Anthropic requires strictly alternating roles: merge consecutive same-role turns.
    merged: list[dict] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            prev = merged[-1]["content"]
            incoming = msg["content"]
            if isinstance(prev, str) and isinstance(incoming, str):
                merged[-1]["content"] = (prev + "\n" + incoming).strip()
            else:
                prev_blocks = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
                next_blocks = incoming if isinstance(incoming, list) else [{"type": "text", "text": incoming}]
                merged[-1]["content"] = prev_blocks + next_blocks
        else:
            merged.append(msg)

    payload: dict[str, Any] = {"model": body.get("model"), "messages": merged or [{"role": "user", "content": ""}]}
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_completion_tokens")
    if max_tokens is None:
        max_tokens = 4096
    payload["max_tokens"] = max_tokens
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        payload["top_p"] = body["top_p"]
    stop = body.get("stop")
    if stop:
        payload["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)

    tools = [
        {
            "name": t.get("function", {}).get("name", ""),
            "description": t.get("function", {}).get("description") or "",
            "input_schema": t.get("function", {}).get("parameters") or {"type": "object", "properties": {}},
        }
        for t in body.get("tools") or []
        if t.get("type") == "function"
    ]
    if tools:
        payload["tools"] = tools
        choice = body.get("tool_choice")
        if choice == "required":
            payload["tool_choice"] = {"type": "any"}
        elif isinstance(choice, dict) and choice.get("type") == "function":
            payload["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
        elif choice == "none":
            payload.pop("tools")
    return payload


def from_anthropic_response(resp: dict, model: str) -> dict:
    """Convert an Anthropic messages response to OpenAI chat.completion format."""
    content = [b for b in resp.get("content", []) if isinstance(b, dict)]
    text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
    tool_calls = []
    for i, block in enumerate(content):
        if block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(p for p in text_parts if p) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = resp.get("usage", {})
    return {
        "id": resp.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": STOP_REASONS.get(resp.get("stop_reason", ""), "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


class AnthropicAdapter(BaseAdapter):
    name = "anthropic"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    async def complete(self, client: httpx.AsyncClient, body: dict) -> dict:
        payload = to_anthropic_request(body)
        resp = await client.post(
            f"{self.base_url}/v1/messages", json=payload, headers=self._headers()
        )
        if resp.status_code >= 400:
            raise AdapterError(resp.status_code, error_body(resp))
        return from_anthropic_response(resp.json(), body.get("model", ""))

    async def stream(self, client: httpx.AsyncClient, body: dict) -> AsyncIterator[bytes]:
        payload = to_anthropic_request(body)
        payload["stream"] = True
        model = body.get("model", "")
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        tool_indexes: dict[int, int] = {}

        def chunk(delta: dict, finish_reason: str | None = None) -> bytes:
            return (
                "data: "
                + json.dumps(
                    {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                    },
                    separators=(",", ":"),
                )
                + "\n\n"
            ).encode()

        req = client.build_request(
            "POST", f"{self.base_url}/v1/messages", json=payload, headers=self._headers()
        )
        resp = await client.send(req, stream=True)
        done = False
        usage = {"input_tokens": 0, "output_tokens": 0}
        try:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode("utf-8", "replace")
                raise AdapterError(resp.status_code, text)
            async for event, data in sse_events(resp.aiter_bytes()):
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                etype = obj.get("type") or event
                if etype == "message_start":
                    msg_usage = obj.get("message", {}).get("usage") or {}
                    usage["input_tokens"] = msg_usage.get("input_tokens", 0)
                    usage["output_tokens"] = msg_usage.get("output_tokens", 0)
                    yield chunk({"role": "assistant", "content": ""})
                elif etype == "content_block_start":
                    block = obj.get("content_block", {})
                    if block.get("type") == "tool_use":
                        idx = len(tool_indexes)
                        tool_indexes[obj.get("index", 0)] = idx
                        yield chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": idx,
                                        "id": block.get("id", ""),
                                        "type": "function",
                                        "function": {"name": block.get("name", ""), "arguments": ""},
                                    }
                                ]
                            }
                        )
                elif etype == "content_block_delta":
                    delta = obj.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield chunk({"content": delta.get("text", "")})
                    elif delta.get("type") == "input_json_delta":
                        yield chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": tool_indexes.get(obj.get("index", 0), 0),
                                        "function": {"arguments": delta.get("partial_json", "")},
                                    }
                                ]
                            }
                        )
                elif etype == "message_delta":
                    delta_usage = obj.get("usage") or {}
                    if "output_tokens" in delta_usage:
                        usage["output_tokens"] = delta_usage["output_tokens"]
                    stop = obj.get("delta", {}).get("stop_reason")
                    if stop:
                        yield chunk({}, STOP_REASONS.get(stop, "stop"))
                elif etype == "message_stop":
                    done = True
                    yield _usage_chunk(chunk_id, created, model, usage)
                    yield b"data: [DONE]\n\n"
            if not done:
                yield _usage_chunk(chunk_id, created, model, usage)
                yield b"data: [DONE]\n\n"
        finally:
            await resp.aclose()
