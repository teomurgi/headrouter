# Headrouter Architecture Spine — Two-Layer Routing (Aliases + Key Entitlements)

v1.0 — 2026-08-24 · Owner: Winston (arch) · Consumers: Amelia (eng), Sally (UX spec `ux-spec.md`), John (PRD)

This document defines the **invariants and interface contracts** for the two-layer routing model (global aliases, per-key alias entitlements) and the admin surface. It is the engineering source of truth; Sally's `ux-spec.md` is the UX source of truth. Where they conflict on UX behavior, ux-spec wins; on mechanics, this doc wins.

---

## 1. Conceptual model (three nouns — matches ux-spec §2)

```
Provider  ::= { name, type, base_url, api_key | api_key_env }
Alias     ::= { name → provider:model }          # global table
Key       ::= { api_key | api_key_env, aliases: [alias names] }
```

- A client request carries only an **alias name** in `model`.
- Resolution is **closed**: a key can reach exactly the models its granted aliases point to. No fallthrough, no raw `provider:model` passthrough for key-bound traffic.
- `provider:model` direct syntax remains valid **only** for requests authenticated with `GATEWAY_API_KEYS` (admin/operator traffic), not for JSON-bound keys.

## 2. Invariants (the spine — these may not be violated by any story)

- **INV-1 (Closed resolution).** For a key-bound request, `resolve(model, key)` returns a Route iff `model ∈ aliases(key)`; otherwise it fails. No default-route rescue, no raw-name passthrough for bound keys. *(Replaces today's fallthrough at config.py L326.)*
- **INV-2 (List/error agreement).** `GET /v1/models` for key K lists exactly `∩(global aliases, K.aliases)`, and the deny error for K names exactly that same set. Both derive from one function; they can never disagree.
- **INV-3 (Every key ≥ 1 alias).** Validation (startup and admin apply) rejects a key with an empty alias set. Aliases referenced by a key must exist.
- **INV-4 (Atomic config lifecycle).** Config changes go: build new immutable `Settings` → validate → single atomic swap of `app.state.settings` (under an asyncio lock). In-flight requests finish against the old snapshot. Disk writes are tmp-file + `os.replace`.
- **INV-5 (No secrets to the browser)** — two clauses. (1) **Reads:** no admin API response ever contains a secret value — `sanitized_config()` is the enforcement point; provider credentials surface as `api_key_set: true` (or the env-var name), gateway keys as generated-once values. Absolute, unchanged. (2) **Writes:** `PUT /admin/config` accepts a pasted `api_key` value **for providers only, write-only** (stored server-side in providers.json, never echoed back; blank-on-edit keeps the existing value). Gateway *keys* still reject raw values outright. The env-var path remains supported for providers.
- **INV-6 (Log is metadata-only).** The request ring buffer stores no request/response bodies — headers-extracted metadata only (see §5).
- **INV-7 (No database).** All state is config file + in-memory. Ring buffers are bounded (`deque(maxlen=N)`).
- **INV-9 (Admin surface requires admin claim).** All `/admin/*` API endpoints demand a key with the `admin` claim, enforced by one router-level `Depends(require_admin_key)` — a valid-but-scoped key gets **403** with a distinct body (`"admin key required"`), vs. 401 = no/invalid key from the auth middleware. New admin endpoints inherit the guard automatically; the escalation chain (scoped key PUTs a self-promoting config) is regression-locked. `GATEWAY_API_KEYS` is the **sole** admin source — a `keys[].admin` field in providers.json is rejected by `validate_config()` (config can never confer admin). (`945b76d`, `e3b6b8c`)

## 3. Resolution algorithm (normative)

```python
def resolve(model: str, key: str | None) -> Route:
    # 1. Admin key (GATEWAY_API_KEYS): full legacy behavior
    #    (global routes, direct provider:model, default_route) — unchanged.
    # 2. Bound key:
    #    granted = {a for a in key.aliases if a in global_aliases}   # INV-3 ⇒ non-empty
    #    if model in granted: return global_aliases[model]
    #    raise ModelNotGranted(available=sorted(granted))            # INV-1, INV-2
```

`ModelNotGranted` renders as a 404-style OpenAI-compatible error whose `message` enumerates the key's available aliases (wording/format per ux-spec §6).

**Migration invariant (INV-8):** migration from the single-provider schema generates **explicit** per-key `aliases:` entries (one per model that key could previously reach on its provider) written into the config file — no runtime "all aliases on provider" fallback, no silent entitlement widening. The admin UI surfaces these synthesized grants for pruning (ux-spec §3.2).

## 4. Config schema (v2 providers.json)

```json
{
  "providers": [{ "name": "...", "type": "...", "base_url": "...", "api_key_env": "..." }],
  "aliases":   { "fast": "openrouter:gpt-4o-mini", "smart": "my-openai:gpt-4o" },
  "keys": [
    { "name": "team-b", "api_key_env": "KEY_TEAM_B", "aliases": ["fast"] }
  ]
}
```

Rules (enforced by one `validate_config()` used at startup *and* by admin apply — single implementation):

- alias names unique; each alias's provider exists in `providers` (or `KNOWN_PROVIDERS`)
- `keys[].aliases` non-empty and all names exist in `aliases`
- duplicate api_key across entries → error (as today)
- `name` on keys is optional metadata (for admin UI display; never used in auth)

**Compat:** the loader accepts the old shape (`keys[].provider` + per-key `routes`) and migrates it in-memory per INV-8, but `PUT /admin/config` always writes the v2 shape. Startup log prints the migration explicitly.

## 5. Admin surface

| Endpoint | Auth | Behavior |
|---|---|---|
| `GET /admin` | gateway key | static page (no build step) |
| `GET /admin/config` | gateway key | v2 config, secrets as env-var names |
| `POST /admin/config/validate` | gateway key | staged config → validation result `{valid, errors}` only; the client computes the display diff (staged vs. last-saved, both in hand). No mutation |
| `PUT /admin/config` | gateway key | validate → apply (INV-4/5). Reject invalid with same validation body |
| `GET /admin/log?after=SEQ` | gateway key | ring buffer entries with `seq > SEQ`; response includes `last_seq`; empty delta = `204`-style `{entries: [], last_seq}` |
| `GET /admin/health/providers` | gateway key | per-provider reachability (cached, TTL ~30s) |

**Ring buffer entry:** `{seq, ts, key_name, alias, resolved: "provider:model", status, latency_ms, tokens_in, tokens_out, compressed, tokens_saved}`. Never bodies, never key values (INV-6). Bounded at ~500 (`deque(maxlen=500)`).

**Apply semantics:** `PUT` carries the full staged config; server re-validates (never trust the client-side Validate), swaps atomically (INV-4). If the ring buffer's key_name references a deleted key, entries render with the stored name (log is historical).

## 6. Story slicing for Amelia (suggested order)

1. **A1 — Alias model + closed resolver.** New schema, `validate_config()`, INV-1/2/3, migration. Pure config.py work + tests. No behavior change for admin keys.
2. **A2 — Key-aware `/v1/models` + deny error.** Small; depends on A1. Locked by INV-2 tests.
3. **A3 — Atomic hot reload.** Loader refactor + `os.replace` write path. INV-4.
4. **A4 — Admin API** (config get/validate/apply, log, provider health). INV-5/6.
5. **A5 — Static `/admin` page** per ux-spec (Sally's domain; consume A4).

A1–A3 are the risk spine; A4–A5 are additive.

## 7. Explicitly deferred (v2+)

- Config editing beyond keys/aliases; per-key rate limits/spend tracking; persistence of the request log; OAuth/multi-user admin auth; alias versioning/history. (Matches ux-spec §8 "Evolution".)
