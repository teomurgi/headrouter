# Security Policy

## Supported Versions

| Version | Supported |
| --- | --- |
| 0.1.x (latest release) | ✅ |
| < 0.1 | ❌ |

## Reporting a Vulnerability

Headrouter is a network-facing proxy that handles API keys — we take security
reports seriously.

**Please do NOT open a public GitHub issue for security vulnerabilities.**

Instead, report privately via
[GitHub private vulnerability reporting](../../security/advisories/new)
(Security tab → "Report a vulnerability").

Please include:

- A description of the vulnerability and its impact
- Steps to reproduce (config shape, request sequence)
- Affected version / commit
- Whether you have a suggested fix

**Response targets:** acknowledgement within 72 hours; assessment and a fix or
mitigation plan within 14 days for confirmed issues.

## Scope notes

Particularly security-sensitive surfaces in this codebase:

- **Key handling** — scoped keys in `providers.json`, admin keys via
  `GATEWAY_API_KEYS`, closed alias resolution (INV-9 in
  [docs/architecture.md](docs/architecture.md)). Key values must never appear
  in admin API responses or logs.
- **Admin API** (`/admin/*`) — the API rejects inline `api_key` values in
  staged configs by design; only env-var *names* cross the wire.
- **Proxy passthrough** — arbitrary paths are proxied to the resolved provider
  with the provider's credentials; auth and closed resolution are the only
  boundaries.
- **Request log** — metadata only (key name, alias, status, latency, tokens);
  never bodies or key material.

If you find a way to cross any of these boundaries, it is in scope.

## Operational security recommendations

- Always set `GATEWAY_API_KEYS` when binding to anything other than
  `127.0.0.1`; empty means **no auth**.
- Keep `providers.json` and `.env` out of version control (they are
  git-ignored by default) and mode `600`.
- Prefer `api_key_env` references over inline `api_key` values in
  `providers.json`.
- Rotate gateway keys by editing via the admin UI (stage → validate → apply);
  revocation takes effect immediately without restart.
