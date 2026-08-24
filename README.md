# Headroom Gateway

A lightweight, **stateless** OpenAI-compatible gateway that:

1. Accepts OpenAI Chat Completions requests (`/v1/chat/completions`)
2. Applies [Headroom](https://github.com/headroomlabs-ai/headroom) context compression when the conversation exceeds a token threshold
3. Routes requests to a configured provider (OpenAI, Anthropic, Gemini, Ollama, OpenRouter — or any OpenAI-compatible endpoint)
4. Streams SSE responses back to the client without buffering
5. Requires **no database** — runs as a single container/process

Works with any OpenAI-compatible client: Claude Code, OpenCode, Cline, Aider, Roo Code, PydanticAI, LangGraph, etc.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e .
# optional, enables real compression (otherwise gateway estimates tokens and passes through):
.venv/bin/pip install "headroom-ai>=0.36"

export OPENAI_API_KEY=sk-...
export GATEWAY_ROUTES="gpt4o=openai:gpt-4o,sonnet=anthropic:claude-sonnet-4"
export ANTHROPIC_API_KEY=sk-ant-...

.venv/bin/uvicorn app:app --port 8000
```

Point any OpenAI client at `http://localhost:8000/v1`:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "messages": [{"role": "user", "content": "hello"}]}'
```

## Configuration (env)

| Variable | Default | Description |
| --- | --- | --- |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Listen address |
| `GATEWAY_API_KEYS` | *(empty)* | Comma-separated API keys; empty disables auth |
| `GATEWAY_ROUTES` | *(empty)* | `alias=provider:model,...` logical model routing |
| `GATEWAY_DEFAULT_ROUTE` | *(empty)* | `provider:model` fallback for unrouted models |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` | | Provider credentials |
| `*_BASE_URL` | provider defaults | Override provider endpoints (e.g. proxies, Azure-style gateways) |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama endpoint (no key needed) |
| `COMPRESSION_ENABLED` | `1` | Enable headroom compression |
| `COMPRESSION_THRESHOLD_TOKENS` | `4000` | Compress only when input exceeds this budget |
| `REQUEST_TIMEOUT_SECONDS` | `300` | Upstream timeout |

Models can also be addressed directly as `provider:model`, e.g. `"model": "anthropic:claude-sonnet-4"`.

## Endpoints

- `POST /v1/chat/completions` — chat completions (streaming and non-streaming)
- `GET /v1/models` — configured aliases
- `GET /health` — liveness + compression engine status
- `GET /metrics` — Prometheus text format (requests, latency, tokens, compression savings)

## Behavior notes

- **Compression never blocks a request**: any headroom failure degrades to passthrough.
- Non-OpenAI providers get full format translation: messages, system prompts, tools / tool calls / tool results, streaming SSE — all converted to/from OpenAI wire format.
- Upstream errors are propagated with their original status codes in an OpenAI-style error body.
- `X-Compression-Applied: true|false` response header indicates whether compression ran.

## Docker

```bash
docker build -t headroom-gateway .
docker run -p 8000:8000 \
  -e OPENAI_API_KEY=sk-... \
  -e GATEWAY_ROUTES="gpt4o=openai:gpt-4o" \
  headroom-gateway
```

For compression inside the container, uncomment the `headroom-ai` line in the `Dockerfile`.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Architecture

```
Client -> FastAPI app
           |- AuthMiddleware (optional API keys)
           |- CompressionService (headroom TransformPipeline, threshold-gated)
           |- Router -> provider adapters
                        |- OpenAICompatAdapter (OpenAI / OpenRouter / Ollama)
                        |- AnthropicAdapter  (format translation + SSE conversion)
                        |- GeminiAdapter     (format translation + SSE conversion)
```
