# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.4] - 2026-08-29

### Fixed

- **Tray: "Open logs" / "Edit environment" silently did nothing on the
  packaged Linux build** — the tray is a PyInstaller onefile binary whose
  runtime hooks inject `LD_LIBRARY_PATH`, `GI_TYPELIB_PATH`, `GIO_MODULE_DIR`,
  `GTK_PATH`, etc. into the tray *process itself* (they never appear in
  `/proc/<tray>/environ`). The spawned GUI editor / `xdg-open` inherited that
  environment, dynamically linked against the bundled (older) glib, and died
  instantly with `symbol lookup error: ... g_sort_array` before any window
  appeared. Helper processes are now spawned via a new `_clean_child_env()`
  that strips the PyInstaller-injected variables (restoring the user's real
  `*_ORIG` values) so children link against the system libraries.

### Added

- **Regression tests for the above** — env-sanitisation unit tests
  (`_clean_child_env` strips/restores correctly, no-op when unfrozen), an
  end-to-end test that `_open_in_text_editor` spawns the editor with a clean
  environment, and tray menu-wiring tests that click "Open logs" / "Edit
  environment" and assert they reach the editor with the right file.

## [0.2.3] - 2026-08-28

### Fixed

- **Tray: "Edit environment" / "Open logs" opened in the web browser** —
  both menu items used `webbrowser.open(file://...)`, which hands the file
  to the desktop's default handler (often Firefox). Firefox refuses
  `file://` dotfiles, and sandboxed (snap/flatpak) browsers can't read
  `~/.config` at all, showing "Access to the file was denied". Both now
  open in a GUI text editor via a new `_open_in_text_editor()` helper:
  `$VISUAL`/`$EDITOR` (skipping terminal editors that can't be detached
  from a tray), then a curated list of GUI editors, then `open -t` on
  macOS / `xdg-open` as a last resort.

### Added

- **Feature test suite** — 23 end-to-end tests in `tests/test_feature_*.py`
  covering gateway user journeys (discover → chat, cross-provider alias
  routing, streaming, auth enforcement, no key leakage upstream), tray &
  launcher desktop behaviour (env/providers file creation and permissions,
  editor selection), and the CLI entry points (localhost-only binding when
  unauthenticated, `0.0.0.0` with keys, flag wiring).

## [0.2.2] - 2026-08-28

### Fixed

- **Tray: startup crash on machines with system Pillow** — the runtime hook
  prepended system dist-packages, so a system Pillow built for a different
  Python ABI shadowed the bundled PIL and crashed the tray with
  `ImportError: cannot import name '_imaging'`. The runtime hook is removed
  entirely: the frozen binary is self-contained (gi, AppIndicator typelibs,
  GTK libraries bundled), so system dist-packages are never consulted.

## [0.2.1] - 2026-08-28

### Fixed

- **Tray: .deb build failed to start** — the frozen tray crashed during
  bootstrap with `ModuleNotFoundError: No module named
  '_sysconfigdata__aarch64-linux-gnu'`. The runtime hook no longer calls
  `sysconfig` (PyInstaller's hook cannot collect the Debian-patched
  `_sysconfigdata` module); dist-packages globs already cover every path.


## [0.2.0] - 2026-08-27

### Added

- **Tray: single-instance detection** — the tray now checks (via a health
  check plus a shared, per-port PID marker) whether a gateway is already
  serving on its configured host:port before starting one, and attaches to
  it instead of spawning a duplicate. Covers another tray instance's child
  process as well as gateways started out-of-band. Stop/Restart can signal
  an adopted process when its PID is known; the status menu reports
  `running`, `running (external)`, or `running (unmanaged)` accordingly.

### Fixed

- **Proxy streaming** — no longer re-raises after a mid-stream upstream
  disconnect (e.g. `httpcore.ReadError`) or client disconnect; the response
  has already started, so the stream now just ends instead of surfacing an
  unhandled-exception traceback.

## [0.1.0] - 2026-08-27

First public release.

### Added

- **OpenAI-compatible gateway** — `POST /v1/chat/completions` with streaming
  SSE passthrough, plus native Anthropic Messages (`/v1/messages`) and
  transparent proxying of any other provider path (`/v1/embeddings`,
  `/v1/responses`, files, audio, …).
- **Headroom context compression** — threshold-gated, never blocks a request
  (any failure degrades to passthrough). Strategies: `coding`, `balanced`,
  `general`, `agent-90`, `default`. `X-Compression-Applied` response header.
- **Multi-provider routing** — OpenAI, OpenRouter, Ollama, any
  OpenAI-compatible endpoint, plus full format translation for Anthropic and
  Gemini (messages, system prompts, tools, streaming).
- **v2 config (`providers.json`)** — providers, global aliases, and scoped
  keys with per-key alias grants; closed resolution for key-bound traffic
  (no fallthrough, no raw `provider:model` passthrough). Legacy configs
  migrate automatically at startup.
- **Admin UI & API (`/admin`)** — dashboard with KPIs and live request stream,
  stage → validate → apply config editing with client-side diff, provider
  reachability with blast radius, one-time-display key issuance. Config
  applies atomically with hot reload — no restart.
- **No database** — bounded metadata-only request log (500 entries), config
  persisted via tmp-file + rename.
- **Desktop app** — system-tray application managing the gateway as a child
  process; PyInstaller frozen binaries; Ubuntu `.deb` and macOS `.app`
  packaging with CI release workflow.
- **Observability** — Prometheus `/metrics`, `/health` with compression
  engine status.
- **Test suite** — 145 tests covering adapters, alias routing, admin API,
  compression, proxying, SSE, and adversarial UI cases.

[0.2.0]: https://github.com/teomurgi/headrouter/releases/tag/v0.2.0
[0.1.0]: https://github.com/teomurgi/headrouter/releases/tag/v0.1.0
