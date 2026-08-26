import json

from conftest import auth_headers


def test_anthropic_conversion_nonstream(client, captured):
    r = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={
            "model": "sonnet",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city": "Rome"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "sunny, 25C"},
                {"role": "user", "content": "thanks"},
            ],
            "max_tokens": 128,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["content"] == "Hi from Claude"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["prompt_tokens"] == 8

    sent = captured["requests"][0]
    assert sent["url"] == "https://anthropic.test/v1/messages"
    assert sent["body"]["system"] == "be terse"
    assert sent["body"]["max_tokens"] == 128
    msgs = sent["body"]["messages"]
    # user + (assistant tool_use + user tool_result merged ordering) + user thanks
    roles = [m["role"] for m in msgs]
    assert roles[0] == "user"
    assert "assistant" in roles
    tool_result_msg = [m for m in msgs if any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in (m["content"] or []) if isinstance(m["content"], list)
    )]
    assert tool_result_msg, "tool result should be converted to a tool_result block"
    tool_use_blocks = [
        b for m in msgs if isinstance(m["content"], list) for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    assert tool_use_blocks[0]["name"] == "get_weather"
    assert tool_use_blocks[0]["input"] == {"city": "Rome"}


def test_anthropic_tools_forwarded(client, captured):
    r = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={
            "model": "sonnet",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": "auto",
        },
    )
    assert r.status_code == 200
    tools = captured["requests"][0]["body"]["tools"]
    assert tools[0]["name"] == "get_weather"
    assert tools[0]["input_schema"]["type"] == "object"


def test_anthropic_stream(client):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as r:
        assert r.status_code == 200
        lines = [line for line in r.iter_lines() if line.startswith("data: ")]
    payloads = [json.loads(line[6:]) for line in lines[:-1]]
    assert lines[-1] == "data: [DONE]"
    assert payloads[-1]["usage"]["completion_tokens"] > 0
    content_payloads = [c for c in payloads if c["choices"]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in content_payloads)
    assert text == "Hi there"
    assert content_payloads[0]["choices"][0]["delta"]["role"] == "assistant"
    assert content_payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert content_payloads[0]["object"] == "chat.completion.chunk"


def test_gemini_conversion_nonstream(client, captured):
    r = client.post(
        "/v1/chat/completions",
        headers=auth_headers(),
        json={
            "model": "gem",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ],
            "temperature": 0.5,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["content"] == "Hi from Gemini"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["prompt_tokens"] == 6

    sent = captured["requests"][0]
    assert sent["url"].endswith(":generateContent")
    assert sent["body"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert sent["body"]["contents"][0]["role"] == "user"
    assert sent["body"]["generationConfig"]["temperature"] == 0.5


def test_gemini_stream(client):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth_headers(),
        json={"model": "gem", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as r:
        assert r.status_code == 200
        lines = [line for line in r.iter_lines() if line.startswith("data: ")]
    payloads = [json.loads(line[6:]) for line in lines[:-1]]
    assert lines[-1] == "data: [DONE]"
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in payloads)
    assert text == "Hello"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
