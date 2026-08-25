## Variant: Dense + Config (winner iteration)

### Design stance
Variant 001 (utilitarian dense) extended into a three-page tool: Dashboard / Routes / Providers. Same dark GitHub-esque visual language, monospace, purple = compression.

### Pages
- `index.html` — dashboard (KPIs, chart, live log, left rail)
- `routes.html` — editable alias → provider:model table with staged changes, Validate/Discard/Apply bar
- `providers.html` — provider cards (type, base URL, key *env var name*), health, Test connection, staged state

### Key UX decisions
- **Stage → Validate → Apply** everywhere: edits are client-staged, validated (duplicate aliases, ambiguous key bindings), diffed, then written to providers.json. Never a blind write.
- **Secrets never in the browser**: only env-var *names* are edited here; values stay server-side.
- **Hot reload on apply** — no container restart to discover a typo.
- Provider health surfaces blast radius: "unreachable — 2 routes affected: fast".

### Accessibility
Status via shape+text (●/◐/⚠), aria-live regions for staging feedback, alertdialog banners, labelled inputs, visible focus rings.

### Best for
Dev-Danilo (local) and Ops-Olga (team) — monitoring and safe configuration in one tool.
