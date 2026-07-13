# CLAUDE.md — clients/client_template (the worked client pattern)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)** — read it first; this file only adds
local context. If they disagree, root wins.

`client_template/` is the canonical pattern **every new client copies**. A client is fully derived
from its short key `<c>` (see the derivation rule in root `CLAUDE.md`) — never re-type resource names.

**The data contract (matched BY NAME across three stages):**

```
sql/*.sql (view column) -> job/main.py (data dict key) -> dash/dashboard.html (data.* key)
```

Renaming a key in one stage breaks the next. Adding a metric is usually three edits, one per stage.

- **`sql/`** — the three views. Edit the `.sql`, reapply with `create_views.py` (never the BQ console).
- **`job/`** — the export job that assembles `<c>.json`; self-gates on `_freshness.json` (see the
  freshness contract in root). `freshness.py` is vendored identically here.
- **`dash/`** — one self-contained `dashboard.html`; inline JS must be **esprima-4.x-safe**.

**Deploy (per stage, all idempotent):** `sql/deploy_views_template.ps1`, `job/deploy_job_template.ps1`,
`dash/deploy_dash_template.ps1`; full standup `deploy_template.ps1`. Use `FORCE_REBUILD=1` for
view/code/seed changes (they don't advance the watermark).
