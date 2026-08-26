"""Gemini adapter: converts OpenAI chat format to/from the Gemini generateContent API."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from .base import AdapterError, BaseAdapter, error_body
from .sse import sse_events

FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "OTHER": "stop",
}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content or "")


def _json_loads(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def to_gemini_request(body: dict) -> dict:
    """Build a Gemini generateContent payload from an OpenAI-format body."""
    system_parts: list[str] = []
    contents: list[dict] = []
    tool_id_to_name: dict[str, str] = {}

    for msg in body.get("messages", []):
        role = msg.get("role")
        if role in ("system", "developer"):
            text = _content_text(msg.get("content"))
            if text:
                system_parts.append(text)
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": _content_text(msg.get("content"))}]})
        elif role == "assistant":
            parts: list[dict] = []
            text = _content_text(msg.get("content"))
            if text:
                parts.append({"text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                tool_id_to_name[tc.get("id", "")] = name
                parts.append({"functionCall": {"name": name, "args": _json_loads(fn.get("arguments"))}})
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif role == "tool":
            name = tool_id_to_name.get(msg.get("tool_call_id", ""), "")
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": name,
                                "response": {"result": _content_text(msg.get("content"))},
                            }
                        }
                    ],
                }
            )

    payload: dict[str, Any] = {"contents": contents}
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

    gen_cfg: dict[str, Any] = {}
    if body.get("temperature") is not None:
        gen_cfg["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        gen_cfg["topP"] = body["top_p"]
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    if max_tokens is not None:
        gen_cfg["maxOutputTokens"] = max_tokens
    stop = body.get("stop")
    if stop:
        gen_cfg["stopSequences"] = [stop] if isinstance(stop, str) else list(stop)
    if gen_cfg:
        payload["generationConfig"] = gen_cfg

    decls = [
        {
            "name": t.get("function", {}).get("name", ""),
            "description": t.get("function", {}).get("description") or "",
            "parameters": t.get("function", {}).get("parameters") or {"type": "OBJECT", "properties": {}},
        }
        for t in body.get("tools") or []
        if t.get("type") == "function"
    ]
    if decls:
        payload["tools"] = [{"functionDeclarations": decls}]
    return payload


def _candidate_to_openai(obj: dict, model: str, chunk_id: str, delta: bool) -> dict:
    candidate = (obj.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if "text" in p)
    tool_calls = None
    if any("functionCall" in p for p in parts):
        tool_calls = []
        for i, p in enumerate(parts):
            fc = p.get("functionCall")
            if not fc:
                continue
            tool_calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": fc.get("name", ""), "arguments": json.dumps(fc.get("args") or {})},
                }
            )
    finish = FINISH_REASONS.get(candidate.get("finishReason") or "", None)
    if delta:
        msg: dict[str, Any] = {}
        if text:
            msg["content"] = text
        if tool_calls:
            for i, tc in enumerate(tool_calls):
                tc["index"] = i
            msg["tool_calls"] = tool_calls
        choice = {"index": 0, "delta": msg, "finish_reason": finish}
    else:
        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        choice = {"index": 0, "message": msg, "finish_reason": finish or "stop"}
    out = {
        "id": chunk_id,
        "object": "chat.completion.chunk" if delta else "chat.completion",
        "created": int(time.time()),
        "model": obj.get("modelVersion") or model,
        "choices": [choice],
    }
    usage_meta = obj.get("usageMetadata")
    if usage_meta:
        out["usage"] = {
            "prompt_tokens": usage_meta.get("promptTokenCount", 0),
            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
            "total_tokens": usage_meta.get("totalTokenCount", 0),
        }
    return out


def from_gemini_response(obj: dict, model: str) -> dict:
    resp = _candidate_to_openai(obj, model, f"chatcmpl-{uuid.uuid4().hex[:12]}", delta=False)
    resp.setdefault("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    return resp


class GeminiAdapter(BaseAdapter):
    name = "gemini"

    def _url(self, model: str, stream: bool) -> str:
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{self.base_url}/v1beta/models/{model}:{action}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return headers

    async def complete(self, client: httpx.AsyncClient, body: dict) -> dict:
        model = body.get("model", "")
        resp = await client.post(
            self._url(model, stream=False),
            json=to_gemini_request(body),
            headers=self._headers(),
        )
        if resp.status_code >= 400:
            raise AdapterError(resp.status_code, error_body(resp))
        return from_gemini_response(resp.json(), model)

    async def models(self, client: httpx.AsyncClient) -> list[str]:
        resp = await client.get(f"{self.base_url}/v1beta/models", headers=self._headers())
        if resp.status_code >= 400:
            raise AdapterError(resp.status_code, error_body(resp))
        out = []
        for m in resp.json().get("models", []):
            if isinstance(m, dict) and m.get("name"):
                out.append(str(m["name"]).removeprefix("models/"))
        return out

    async def stream(self, client: httpx.AsyncClient, body: dict) -> AsyncIterator[bytes]:
        model = body.get("model", "")
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        req = client.build_request(
            "POST",
            self._url(model, stream=True),
            json=to_gemini_request(body),
            headers=self._headers(),
        )
        resp = await client.send(req, stream=True)
        try:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode("utf-8", "replace")
                raise AdapterError(resp.status_code, text)
            first = True
            async for _event, data in sse_events(resp.aiter_bytes()):
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                out = _candidate_to_openai(obj, model, chunk_id, delta=True)
                if first:
                    out["choices"][0]["delta"] = {"role": "assistant", **out["choices"][0]["delta"]}
                    first = False
                yield ("data: " + json.dumps(out, separators=(",", ":")) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        finally:
            await resp.aclose()
