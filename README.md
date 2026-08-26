# Headrouter

A lightweight, **stateless** OpenAI-compatible gateway that:

1. Accepts OpenAI Chat Completions requests (`/v1/chat/completions`)
2. Applies [Headroom](https://github.com/headroomlabs-ai/headroom) context compression when the conversation exceeds a token threshold
3. Routes requests through **global aliases** with **per-key entitlements** (OpenAI, Anthropic, Gemini, Ollama, OpenRouter — or any OpenAI-compatible endpoint)
4. Streams SSE responses back to the client without buffering
5. Ships a built-in admin UI (`/admin`) with stage→validate→apply config editing, live request stream, and provider health
6. Requires **no database** — runs as a single container/process

Works with any OpenAI-compatible client: Claude Code, OpenCode, Cline, Aider, Roo Code, PydanticAI, LangGraph, etc.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e .

cp .env.example .env   # then edit: provider credentials, GATEWAY_API_KEYS, ...

c
```

A `.env` file in the working directory (or the repo root) is loaded automatically at startup — no need to `export` variables manually. Real environment variables always take precedence; `.env` only fills in what isn't already set. Set `GATEWAY_ENV_FILE` to load a different file.

Point any OpenAI client at `http://localhost:8000/v1`:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer key-team-a" \
  -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "hello"}]}'
```

## Concepts (three nouns)

- **Provider** — where requests go (`type`, `base_url`, credential via env-var name).
- **Alias** — a named model route (`fast -> openrouter:gpt-4o-mini`), global, defined once. Clients only ever send alias names.
- **Key** — a downstream credential entitled to a *set* of aliases.

Resolution is **closed** for key-bound traffic: a key can reach exactly the models its granted aliases point to — no fallthrough, no raw `provider:model` passthrough. Asking for anything else returns a 404-style error listing the key's available aliases (the same list `GET /v1/models` shows for that key). Admin keys (`GATEWAY_API_KEYS`) keep full legacy behavior: global routes, direct `provider:model`, and the default route.

## Configuration (providers.json, v2)

```json
{
  "providers": [
    { "name": "my-openai", "type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "MY_OPENAI_KEY" },
    { "name": "or", "type": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY" }
  ],
  "aliases": {
    "fast": "or:gpt-4o-mini",
    "smart": "my-openai:gpt-4o"
  },
  "keys": [
    { "name": "team-a", "api_key_env": "KEY_TEAM_A", "aliases": ["fast"] },
    { "name": "team-b", "api_key": "key-team-b", "aliases": ["fast", "smart"] }
  ]
}
```

Point at it with `GATEWAY_PROVIDERS_FILE=providers.json` (or inline via `GATEWAY_PROVIDERS`).

- **providers** — `name`, `type` (`openai`, `openrouter`, `ollama`, `openai-compat`, `anthropic`, `gemini`), `base_url`, and `api_key_env` (the *name* of an environment variable holding the key; the value never lives in the file). Inline `api_key` values are accepted on disk for local use but are **rejected by the admin API** (see below).
- **aliases** — `name -> "provider:model"`. Each alias's provider must exist.
- **keys** — `name` (display only), credential via `api_key_env` or `api_key`, and a non-empty `aliases` grant list; granted names must exist in `aliases`. Duplicate key values are rejected.

**Legacy configs migrate automatically**: the old shape (`keys[].provider` + per-key `routes`) is accepted at startup and converted in-memory to explicit per-key grants — per-key routes become global aliases granted to that key; routeless keys keep provider-scoped access, marked as *migrated* in the admin UI for review/pruning. Every Apply writes the v2 shape back to disk.

### Env-based routing (no JSON)

Without a providers file, `GATEWAY_ROUTES="alias=provider:model,..."` and `GATEWAY_DEFAULT_ROUTE="provider:model"` work as before for admin-key traffic.

## Admin UI & API (`/admin`)

Open `c`. The page itself is public (static HTML); every API call below requires a **gateway key** (`Authorization: Bearer ...`). It inherits gateway auth — there is no separate login.

- **Dashboard** — KPI row (requests, latency p50/p95, tokens in/out, compression savings), live request stream (cursor-polled, filter by compressed/errors, pausable), keys/providers rail.
- **Keys & Aliases** — chip-card editing with a sticky stage → validate → apply bar. Staged changes show a client-computed diff; Validate surfaces server errors verbatim; Apply is re-validated server-side and, on rejection, your staging is preserved.
- **Providers** — cards with type, base URL, key env-var name, reachability with blast radius ("unreachable — 2 aliases affected"), and bound keys.

REST surface (consumed by the page, usable directly):

| Endpoint | Behavior |
| --- | --- |
| `GET /admin/config` | Applied config, **env-var names only** — no secret values ever |
| `POST /admin/config/validate` | Staged config → `{valid, errors[]}`, no mutation |
| `PUT /admin/config` | Validate → atomic apply (hot reload, no restart). Rejects invalid configs with the same validation body; rejects any `api_key` value field. Returns one-time `generated` key values for newly issued keys |
| `GET /admin/log?after=SEQ` | Cursor over the last 500 requests — metadata only (key name, alias, resolved model, status, latency, tokens, compression); never bodies or key values |
| `GET /admin/health/providers` | Per-provider reachability, cached ~30s |

**Issuing a key:** Keys page → "+ Issue key" → grant alias chips → Apply → the generated key value is shown **exactly once** in a modal. It is stored server-side (in providers.json) and never displayed again. If you lose it, revoke and re-issue.

**Config changes apply atomically** (tmp-file + rename, single settings swap): in-flight requests finish against the old snapshot, newly-issued keys authenticate immediately, and a changed `base_url`/credential takes effect on the next request — no container restart.

## Configuration (env)

| Variable | Default | Description |
| --- | --- | --- |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Listen address |
| `GATEWAY_ENV_FILE` | `.env` | Path to an alternative env file to auto-load |
| `GATEWAY_API_KEYS` | *(empty)* | Comma-separated **admin** keys (full routing power); empty disables auth |
| `GATEWAY_PROVIDERS_FILE` / `GATEWAY_PROVIDERS` | *(empty)* | Providers/aliases/keys: JSON file path / inline JSON |
| `GATEWAY_ROUTES` | *(empty)* | `alias=provider:model,...` env-based routing (admin keys) |
| `GATEWAY_DEFAULT_ROUTE` | *(empty)* | `provider:model` fallback for unrouted models (admin keys) |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` | | Provider credentials |
| `*_BASE_URL` | provider defaults | Override provider endpoints (e.g. proxies, Azure-style gateways) |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama endpoint (no key needed) |
| `COMPRESSION_ENABLED` | `1` | Enable headroom compression |
| `COMPRESSION_THRESHOLD_TOKENS` | `0` | Compress only when input exceeds this token budget; `0` = always compress |
| `COMPRESSION_STRATEGY` | `coding` | Headroom routing profile: `coding` (recommended), `balanced`, `general`, `agent-90` (aggressive), or `default` (legacy router) |
| `COMPRESSION_PREFETCH_ENABLED` | `1` | Download the Headroom ONNX compression model and tokenizer before the gateway accepts requests |
| `REQUEST_TIMEOUT_SECONDS` | `300` | Upstream timeout |

## Endpoints

- `POST /v1/chat/completions` or `/chat/completions` — OpenAI chat completions with compression + provider translation (streaming and non-streaming)
- `POST /v1/messages` — native Anthropic Messages requests with the same message compression, metrics, logging, and `X-Compression-Applied` response header
- **Any other path** — transparently proxied to the resolved provider (method, path, query, body, streaming preserved; base URL and auth swapped). `/v1/embeddings`, `/v1/responses`, files, audio, etc. work as if pointed directly at the provider. Key-bound requests resolve closed: only granted aliases, otherwise the same 404-style deny error.
- `GET /v1/models` — the models available **to the authenticated key** (granted aliases for bound keys; all routes for admin keys)
- `GET /admin` + `/admin/*` — admin UI and API (see above)
- `GET /health` — liveness + compression engine status
- `GET /metrics` — Prometheus text format (requests, latency, tokens, compression savings)

## Behavior notes

- **Compression never blocks a request**: any headroom failure degrades to passthrough.
- Non-OpenAI providers get full format translation: messages, system prompts, tools / tool calls / tool results, streaming SSE — all converted to/from OpenAI wire format.
- Upstream errors are propagated with their original status codes in an OpenAI-style error body.
- `X-Compression-Applied: true|false` response header on Chat Completions and Anthropic Messages indicates whether compression reduced the request context.
- Secrets never travel through the admin surface: only env-var *names* are stored, sent, or displayed; generated key values are shown once at creation.

## Docker

```bash
docker build -t headrouter .
docker run -p 8000:8000 \
  -e OPENAI_API_KEY=sk-... \
  -e GATEWAY_API_KEYS=admin-key \
  -v "$PWD/providers.json:/app/providers.json" \
  -e GATEWAY_PROVIDERS_FILE=/app/providers.json \
  headrouter
```

Or mount your `.env` and skip the `-e` flags: `-v "$PWD/.env:/app/.env"` (auto-loaded at startup).

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Architecture

```
Client -> FastAPI app
           |- AuthMiddleware (reads live settings: applies take effect without restart)
           |- CompressionService (headroom TransformPipeline, threshold-gated)
           |- ConfigStore (atomic load -> validate -> swap -> tmp+rename persist)
           |- RequestLog (bounded metadata-only ring buffer, 500 entries)
           |- Router (closed resolution: aliases ∩ key grants; INV-1/2)
           |- Admin API (/admin/config, /admin/log, /admin/health/providers)
           |- Provider adapters
                        |- OpenAICompatAdapter (OpenAI / OpenRouter / Ollama)
                        |- AnthropicAdapter  (format translation + SSE conversion)
                        |- GeminiAdapter     (format translation + SSE conversion)
```

Engineering invariants and contracts live in [`docs/architecture.md`](docs/architecture.md); UX behavior in [`docs/ux-spec.md`](docs/ux-spec.md).
