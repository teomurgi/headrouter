# Contributing to Headrouter

Thanks for your interest in contributing! Headrouter is deliberately small and
dependency-light — the whole gateway is ~3,500 lines of Python. Contributions
that keep it that way are the most welcome.

## Ways to contribute

- **Bug reports** — open an issue with reproduction steps, your config shape
  (redact secrets!), and relevant logs from
  `~/.local/state/headrouter/gateway.log` or stdout.
- **Provider adapters** — new OpenAI-compatible or translated providers
  (see [adapters/](adapters/)).
- **Compression tuning** — strategy profiles, threshold behavior, benchmarks.
- **Docs, tests, and UX polish** — always appreciated.

## Development setup

```bash
git clone https://github.com/teomurgi/headrouter.git
cd headroom
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Run the test suite (145 tests):

```bash
.venv/bin/python -m pytest -q
```

Run the gateway locally with hot reload:

```bash
cp .env.example .env   # fill in provider credentials + GATEWAY_API_KEYS
.venv/bin/python -m uvicorn app:app --reload --port 8000
```

The admin UI is at <http://localhost:8000/admin>.

## Project layout

| Path | Purpose |
| --- | --- |
| [app.py](app.py) | FastAPI app assembly, middleware wiring |
| [config.py](config.py) / [config_store.py](config_store.py) | Settings, atomic load → validate → swap → persist |
| [compression_service.py](compression_service.py) | Headroom integration (never blocks a request) |
| [adapters/](adapters/) | Provider adapters (OpenAI-compat, Anthropic, Gemini) + SSE |
| [middleware/](middleware/) | Auth (live settings) and metrics |
| [routes/](routes/) | Chat, proxy, models, admin API, health |
| [docs/architecture.md](docs/architecture.md) | Engineering invariants (INV-1…9) — read before changing routing/auth |
| [docs/ux-spec.md](docs/ux-spec.md) | Admin UI behavior contracts |

## Ground rules

1. **Invariants are contracts.** [docs/architecture.md](docs/architecture.md)
   lists invariants (e.g. closed alias resolution for key-bound traffic,
   secrets never through the admin surface, compression never blocks a
   request). If your change touches one, update the doc and the tests that
   encode it.
2. **No new heavyweight dependencies.** No database, no background queue, no
   framework sprawl. If a feature needs state, prefer bounded in-memory
   structures (see `RequestLog`) or the existing config file.
3. **Tests for behavior changes.** Routing, auth, and compression-path changes
   need tests in [tests/](tests/). The adversarial UI tests
   ([tests/test_ui_adversarial.py](tests/test_ui_adversarial.py)) are a good
   model for security-sensitive surfaces.
4. **Secrets stay out of the repo.** Config files referenced in tests must use
   env-var names, not values. `providers.json` and `.env` are git-ignored.

## Commit & PR style

- Small, focused commits; imperative subject lines (`fix: 404 on revoked key`
  not `fixed stuff`).
- PRs: describe the *behavior* change, link the issue, note any invariant
  touched. CI runs the full pytest suite on Ubuntu — keep it green.
- For packaging changes (PyInstaller specs, .deb/.app), explain how you
  verified the frozen binary — see [packaging/](packaging/) notes in the
  README.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind.
