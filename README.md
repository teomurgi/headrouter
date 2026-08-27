# Headrouter

**Your LLM traffic, minus the tokens you didn't need.**

[![CI](https://github.com/teomurgi/headrouter/actions/workflows/ci.yml/badge.svg)](https://github.com/teomurgi/headrouter/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/teomurgi/headrouter)](https://github.com/teomurgi/headrouter/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)

A lightweight, **stateless** OpenAI-compatible gateway that:

1. Accepts OpenAI Chat Completions requests (`/v1/chat/completions`)
2. Applies [Headroom](https://github.com/headroomlabs-ai/headroom) context compression when the conversation exceeds a token threshold
3. Routes requests through **global aliases** with **per-key entitlements** (OpenAI, Anthropic, Gemini, Ollama, OpenRouter — or any OpenAI-compatible endpoint)
4. Streams SSE responses back to the client without buffering
5. Ships a built-in admin UI (`/admin`) with stage→validate→apply config editing, live request stream, and provider health
6. Requires **no database** — runs as a single container/process

Works with any OpenAI-compatible client: Claude Code, OpenCode, Cline, Aider, Roo Code, PydanticAI, LangGraph, etc.

## Why Headrouter?

**It pays for itself in tokens.** Agentic coding sessions drag enormous
contexts — tool outputs, logs, diffs, re-read files — through every single
API call. Headrouter sits in front of your providers and compresses that
context before you get billed for it.

**It is genuinely tiny.** There is no database, no message queue, no sidecar,
no cluster. One process holds the entire control plane, data plane, and admin
UI.

### Measured, not marketed

Numbers below are from this repository, measured on an ARM64 Linux laptop
(Python 3.12, `coding` strategy, default settings). Reproduce them with the
one-liners in the collapsible section.

**Running footprint:**

| What | RSS |
| --- | ---: |
| Gateway idle (compression model preloaded) | **~50 MB** |
| Gateway core only (compression disabled) | **~15 MB** |
| Under active compression load | ~365 MB transient |
| Extra processes / databases / queues | **0** |

Everything else about the footprint: **~3,500 lines of Python** for the whole
gateway, a **94 MB self-contained binary** (includes the compression model
runtime — no Python install needed on target machines), and a config that
fits in a single JSON file.

**Compression, measured on real request shapes** (`/metrics` reports these
live for your own traffic):

| Workload | Tokens in → out | Saved |
| --- | --- | ---: |
| Repetitive logs (SRE/agent tool output) | 32,036 → 111 | **99.7%** |
| Log-heavy agent session, 5 turns cumulative | 112,800 → 72,435 | **35.8%** |
| Structured tool results (JSON lint output) | 35,444 → 26,444 | **25.4%** |
| Git-diff-heavy session | 10,987 → 9,427 | **14.2%** |
| Mixed workload (logs + JSON + diffs + code + prose) | 46,400 → 41,018 | **11.6%** |
| Already-dense source code | — | 0% (protected, never grows) |

Two honest caveats, by design: compression is content-dependent — dense,
already-irreducible context is passed through untouched (the compressor
**never expands** a request), and any compression failure degrades to
passthrough rather than blocking your request. The aggressive `agent-90`
profile targets ~90% savings for tool-output-heavy agent trajectories.

<details>
<summary><strong>Reproduce these numbers</strong></summary>

```bash
.venv/bin/pip install -e .
.venv/bin/python - <<'EOF'
import os
os.environ["COMPRESSION_STRATEGY"] = "coding"
from compression_service import CompressionService

def rss_mb():
    return int([l for l in open("/proc/self/status") if l.startswith("VmRSS")][0].split()[1]) / 1024

svc = CompressionService(); svc.prefetch()
print(f"RSS idle with model: {rss_mb():.0f} MB")

logs = "\n".join(f"2026-08-27 10:{i%60:02d}:00 INFO worker-{i%4} job={1000+i} ok" for i in range(400))
msgs = [{"role": "system", "content": "You are an SRE."},
        {"role": "user", "content": f"Logs:\n{logs}\nErrors?"},
        {"role": "assistant", "content": "None."},
        {"role": "user", "content": f"More:\n{logs}\nSummary?"}]
r = svc._maybe_compress_sync(msgs)
print(f"{r.tokens_before} -> {r.tokens_after} tokens ({r.tokens_saved/r.tokens_before:.1%} saved)")
EOF
```

For live numbers on your own traffic, run the gateway and open `/admin` —
the dashboard shows cumulative compression savings, or scrape `/metrics`.

</details>

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e .

cp .env.example .env   # then edit: provider credentials, GATEWAY_API_KEYS, ...

.venv/bin/python -m uvicorn app:app --port 8000
# or: .venv/bin/headrouter-gateway
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

Open <http://localhost:8000/admin>. The page itself is public (static HTML); every API call below requires a **gateway key** (`Authorization: Bearer ...`). It inherits gateway auth — there is no separate login.

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

## Desktop app (tray + .deb / .app)

Headrouter also ships as a **system-tray application**: a small always-on tray icon that manages the gateway as a child subprocess (start/stop/restart, open admin UI, open logs, greyed-out icon when stopped). The gateway is a self-contained frozen binary — no Python install required on the target machine.

### Ubuntu (.deb)

```bash
bash packaging/ubuntu/build-deb.sh           # produces packaging/ubuntu/build/headrouter_0.1.0_arm64.deb
sudo apt install ./packaging/ubuntu/build/headrouter_0.1.0_arm64.deb
```

Then launch "Headrouter" from the app grid (it also autostarts at login). **GNOME/Wayland requires the "AppIndicator and KStatusNotifierItem Support" extension** for the icon to appear: `sudo apt install gnome-shell-extension-appindicator`, then log out/in.

### macOS (.app)

See [packaging/macos/README.md](packaging/macos/README.md) — build with `bash packaging/macos/build-app.sh` on a Mac (uses the native pystray `_darwin` backend, `LSUIElement` menu-bar agent). Signing/notarization steps are documented there.

### Desktop config: no `.env` is loaded

The frozen gateway **does not read `.env` or the repo's `providers.json`**. Runtime config lives at:

- **Providers/aliases/keys** → `$XDG_CONFIG_HOME/headrouter/providers.json` (default `~/.config/headrouter/providers.json`), seeded as `{"providers": [], "keys": []}` on first run. Edit via the admin UI (tray → Open admin UI) or replace the file and Restart.
- **Environment variables** (`GATEWAY_API_KEYS`, `COMPRESSION_STRATEGY`, provider `api_key_env` credentials, …) must exist in the **desktop session environment** — the tray inherits it and passes it down to the gateway. Set them in `~/.profile` and log out/in, e.g.:

  ```bash
  export GATEWAY_API_KEYS=hr_...          # admin key for /admin
  export OPENROUTER_API_KEY=sk-or-...     # any api_key_env credentials referenced in providers.json
  # export COMPRESSION_STRATEGY=balanced  # optional; defaults: coding, enabled, threshold 0
  ```

  Quick one-off test without logout: `pkill -f headrouter; GATEWAY_API_KEYS=hr_... headrouter-tray &`
- **Logs** → `$XDG_STATE_HOME/headrouter/gateway.log` (default `~/.local/state/headrouter/gateway.log`); tray → Open logs.

### How the freeze works (for maintainers)

Two separate PyInstaller binaries are built with **two different interpreters**:

| Binary | Frozen with | Why |
| --- | --- | --- |
| `headrouter-gateway` | project `.venv` (Python 3.12) | bundles headroom-ai + onnxruntime |
| `headrouter-tray` | system `python3` (3.14) venv | system `gi`/AyatanaAppIndicator3 typelibs are compiled for the 3.14 ABI and **cannot** be bundled; [packaging/tray_runtime_hook.py](packaging/tray_runtime_hook.py) prepends system dist-packages to `sys.path` so `import gi` resolves from the OS at runtime. Do **not** add `excludes=["gi"]` to tray.spec — that makes the frozen importer block it entirely. |

Build specs: [packaging/gateway.spec](packaging/gateway.spec), [packaging/tray.spec](packaging/tray.spec) (run `pyinstaller` from `packaging/`). The `.deb` ships `providers.example.json` as a reference only — never a real `providers.json` (which holds secrets).

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

## Community

- **Bugs & features** — [open an issue](../../issues) (templates provided; redact secrets)
- **Contributing** — see [CONTRIBUTING.md](CONTRIBUTING.md) (setup, ground rules, PR checklist)
- **Security** — report privately per [SECURITY.md](SECURITY.md); never in a public issue
- **Conduct** — this project follows the [Contributor Covenant](CODE_OF_CONDUCT.md)
- **Changes** — see [CHANGELOG.md](CHANGELOG.md)

## License

[MIT](LICENSE) © 2026 Headrouter contributors.
