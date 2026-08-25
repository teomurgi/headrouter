# Headrouter Admin UX Spec

v1.0 — 2026-08-24 · Owner: Sally (UX) · Consumers: Amelia (eng, story A5 + A2 copy), Winston (arch, `docs/architecture.md`)

Companion to `docs/architecture.md` (engineering source of truth). Where they conflict on UX behavior, this spec wins; on mechanics, architecture.md wins. Visual reference: `sketches/004-dense-config/` (approved direction).

---

## 1. Personas

**Dev-Danilo** — runs Headrouter locally for his own agent sessions (Claude Code, OpenCode). Terminal-native. Moment of need: *"something felt slow at 2pm — was it compression, routing, or the upstream?"* — answer in 5 seconds.

**Ops-Olga** — runs the gateway in Docker for her team. Issues keys to teams, decides who can use which models, prunes access. Moments of need: issuing a scoped key in under a minute; spotting a misbehaving provider before her teammates do; revoking access without breaking the wrong team.

## 2. Conceptual model (three nouns)

- **Provider** — where requests go (type, base URL, credential via env-var name).
- **Alias** — a named model route (`fast → openrouter:gpt-4o-mini`), global, defined once. Clients only ever send alias names.
- **Key** — a downstream credential entitled to a *set* of aliases.

The whole UI is organized around these three nouns: one page each, plus the Dashboard. No fourth concept is ever shown.

## 3. Pages & wireframe-level specs

### 3.1 Dashboard (`/admin`, `index.html` in the sketch)

Purpose: answer Danilo's question in one glance.

- **Health strip** (header): gateway status pill (● healthy / ⚠ degraded), compression engine status, uptime.
- **Left rail**: read-only Keys (key → first aliases preview) and Providers (dot + name, from `/admin/health/providers`). Links to their pages.
- **KPI row**: requests/24h, latency p50/p95, tokens in/out, compression tokens saved + applied count.
- **Request stream table**: last N entries from the ring buffer. Columns: time, key (name), alias, resolved `provider:model`, status, latency, tokens in/out, compression (▼ + saved when applied). Filter chips: all / compressed / errors. Pause/resume toggle (`aria-pressed`).
- **Live polling**: `GET /admin/log?after=seq` cursor polling (~2s, back off when tab hidden). No websockets — polling only, per architecture.md §5.

### 3.2 Keys & Aliases page (`routes.html` in the sketch)

Two sections, Aliases above Keys (the menu before the diners).

**Aliases** — chip cards: `name → provider:model` + reverse usage count ("2 keys") so Olga sees dependencies before deleting. "+ Add alias" stages a new card (name, provider, model — model is a free-text input, not a dropdown; provider list comes from config).

**Keys** — one card per key:

- Key name (display only; the value is generated on Apply and shown exactly once — never retrievable again. Hard requirement, INV-5's sibling).
- **Alias grants as chips**: click ✕ to revoke (staged), "+ grant alias" to add. This is the most common Olga action; it must be one click.
- Metadata: usage (req/24h), last seen, created-by.
- States: ● live (saved config) / ◐ staged (pending edit).

**Stage → Validate → Apply bar** (sticky): shows count of staged changes; Validate calls `POST /admin/config/validate` and surfaces the server's errors inline — the **client computes the display diff** (staged vs. last-saved config, both already in hand); the server's `{valid, errors}` carries errors only (API stays minimal). Discard reverts staging to last-saved config; Apply calls `PUT /admin/config` (which re-validates server-side — the client never assumes its own validation is authoritative). **After Apply, re-fetch `GET /admin/config` and re-render from it** (provenance/migrated flags may have changed server-side); never merge the PUT response into local state.

**Migration grants (INV-8):** after migrating an old config, synthesized per-key alias grants are marked in the UI with a "migrated" badge and a hint to review/prune. Migrate keys to explicit per-key alias grants (visible in the admin UI for pruning) — never a silent runtime fallback.

**Validation rules surfaced as friendly copy** (all from `validate_config()`): key with empty alias set ("every key needs at least one alias"), alias name collision, alias referencing an unknown provider, duplicate key values.

### 3.3 Providers page (`providers.html` in the sketch)

- Card per provider: name, type dropdown, base URL, **API key** — write-only: paste a plain key once ("stored server-side, never displayed") *or* an env-var name; pasted values are never shown back (GET returns only "set / not set"), and leaving the field blank on edit keeps the existing credential. *(Plain-key support implemented in `acb5ee2`; env-var name remains supported.)*
- **+ Add provider** stages a new card (name, type dropdown — `KNOWN_PROVIDERS` presets plus `openai-compat` custom — free-text base URL, env-var name). **✎ edit in place** (rename auto-re-points aliases). **✕ remove** confirms blast radius ("N aliases affected") before staging. Provider staging rides the same shared stage→validate→apply bar as Keys & Aliases (§3.2 semantics apply verbatim: client diff, server errors inline, staging preserved on rejection, re-fetch after Apply). Renaming/removing a provider that aliases point to shows the blast radius before Apply; dangling-alias applies surface the server's validation error inline rather than being pre-blocked. *(Implemented in `ba45394`.)*
- **Test connection** button → `/admin/health/providers` refresh for that provider; inline result.
- Health line includes blast radius: "unreachable since 14:02 — N aliases affected: fast, smart".
- Keys bound via aliases are listed (read-only; edit grants on the Keys page).

### 3.4 Error copy — deny case (INV-2)

When a key requests an alias it doesn't have, the client sees an OpenAI-compatible 404-style error. The `message` **must enumerate exactly the same alias set that `GET /v1/models` would list for that key** (single source function, per architecture.md §3).

Suggested wording:

> `Model 'X' is not available for this key. Available models: fast, local. See GET /v1/models.`

This is a spec'd string, not a nice-to-have: it's how a developer on team-b self-serves instead of filing a ticket to Olga.

## 4. Flows

**Issue a scoped key (Olga, < 1 min):** Keys page → "+ Issue key" → staged card appears → grant alias chips → Validate (diff shown: `+ issue key, granted: fast`) → Apply → **one-time modal shows the generated key value with a copy button and the warning "shown once, never again"** → modal dismiss is the only way out (no breadcrumb back to the value).

**Revoke an alias from a key:** chip ✕ on the key card → staged → Apply. In-flight requests finish on the old snapshot (INV-4); the UI notes this on Apply: "applied — in-flight requests may still complete".

**Diagnose a slow afternoon (Danilo):** Dashboard → filter "compressed" or "errors" → click a row (future: detail pane per sketch 003; v1 = row columns only) → see provider, latency, compression outcome.

**Swap a model behind an alias:** Aliases → edit `fast → provider:model` → Validate shows the diff and affected keys count → Apply. No client changes ever.

**Add/edit a provider (Olga):** Providers → "+ Add provider" (or ✎ on a card) → staged form (name, type, base URL, env-var name per §3.3) → Validate (diff includes provider changes + dangling-alias callouts) → Apply. Removing or renaming a provider that aliases point to shows blast radius before Apply. *(Implemented in `ba45394`.)*

## 5. States to design (all pages)

- Empty (no keys yet / no aliases / fresh install): friendly setup nudge, "+ Add" prominent.
- Loading (first config fetch): skeleton, not spinner-blank.
- Validation error: inline on the offending field/card, staged bar turns error-red, Apply disabled.
- **Failed inline-form action (negative path, all forms)**: a Stage/Add/Grant that fails validation must keep the form open with the user's entered values, show a **visible, specific** `role="alert"` error line — naming the offending field or the concrete conflict (e.g. "provider 'or' already exists"), never a generic "invalid input" — and never silently discard input. No destructive action (clear, close, remove) runs before its validation passes. Only one staging form is open at a time.
- Apply failure (server rejected): banner with the server's validation body verbatim; staging preserved.
- One gateway-auth failure on admin API: on first 401, the page shows a visible **"enter admin key"** field (password-type, labelled) — key kept in memory/sessionStorage only, sent as `Authorization: Bearer` on every admin fetch; wrong key re-shows the field with a specific error. The gate requires an **admin** key (scoped keys get 403): help text says *"Use an admin key from your providers.json `keys` section (or `GATEWAY_API_KEYS`) — scoped keys don't have admin access."* A **403** (authenticated but not admin) is shown as its own specific state — "this key is valid but lacks admin access" — distinct from a wrong-key 401, and never retried in a loop. Repeated 401s *with* a key present escalate to the full-page "unauthorized — check your gateway API key" state (no retry loop).
- Provider unreachable: red dot + blast radius (never just a bare badge).

## 6. Accessibility requirements (hard)

- Status always conveyed by shape + text (● live / ◐ staged / ⚠ warning), never color alone.
- `aria-live="polite"` region announcing staging actions (grant, revoke, validate result, apply).
- Sticky bar as `role="alertdialog"` when it turns error-state.
- All inputs labelled; visible focus rings; full keyboard path through chip grant/revoke and the flows in §4.
- One-time key modal: focus trapped, Escape = dismiss-with-confirmation, copy button announced.

## 7. Security posture in the UI

- The browser never *receives* key or provider-credential values: generated keys are shown once (§4 flow), pasted provider keys are write-only (server stores; GET returns "set / not set" only). Env-var names remain the alternative path.
- Provider credentials may be pasted once into the admin form; they are never displayed again, never stored client-side, and blank-on-edit preserves the existing value. *(INV-5 two-clause split per architecture.md; implemented in `acb5ee2`.)*
- Generated keys shown once (§4 flow), never stored client-side (no localStorage).
- Admin surface inherits gateway auth; no separate login in v1. The page may hold the gateway key in **sessionStorage for the tab session only** (memory-only preferred; never localStorage) — the §7 localStorage ban targets generated *key values*, not the operator's own session credential.

## 8. Evolution (explicitly deferred, matches architecture.md §7)

Per-key spend/rate limits; click-to-inspect request detail pane (sketch 003); config history/undo; a calm public status page (sketch 002 direction); alias versioning.

## 9. Acceptance checklist (UX-side, for Amelia's A2/A5)

1. Deny error message lists exactly the key's granted aliases — same list as `/v1/models` for that key (INV-2 test).
2. A key's value is displayed exactly once, at generation; no admin API response ever contains it again.
3. Granting/revoking an alias is achievable in ≤ 2 interactions from Keys page load.
4. Applying config never loses staged work on server rejection; the server's validation errors are shown verbatim.
5. Ring-buffer log renders `metadata-only` fields and never request bodies (INV-6).
6. All §6 accessibility checks pass (shape+text status, labelled inputs, keyboard flow, live regions).
