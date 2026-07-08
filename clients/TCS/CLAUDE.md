# CLAUDE.md — clients/TCS (Business Quiz dashboard)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)** — read it first; this file only adds
local context. If they disagree, root wins.

TCS is a real client built on the `client_template` pattern (client key `tcs` — every resource name
derives from it). It differs from the template in ONE structural way:

**TCS ingests via DIRECT-API loaders, not Windsor** (`services/ingest/tcs_shopify` /
`tcs_klaviyo` / `tcs_quiz` → `raw_windsor.tcs_*`). This is a *sanctioned, documented exception* to
"Windsor is the only ingest source": the Business-Quiz diagnostic needs per-recipient Klaviyo
open/click events, a grain Windsor does not serve. The pull logic is ported from
`archive_code/analytics.py` (the old Colab notebook, kept read-only for reference).

**The data contract (matched BY NAME across three stages):**

```
sql/*.sql (view column) -> job/main.py (data dict key) -> dash/dashboard.html (data.* key)
```

- **`sql/`** — NINE views (not the template's three): quiz → conversion → engagement → monthly →
  cohort → kpi. Leads are keyed to their FIRST quiz submission (one row per email). Reapply with
  `create_views.py` (never the BQ console).
- **`job/`** — assembles `tcs.json` (`kpis` / `monthly` / `cohorts` / `leads`); self-gates on the
  `raw_windsor.tcs_*` tables. `freshness.py` is vendored identically.
- **`dash/`** — one self-contained `dashboard.html`, dark `--ag-*` theme, inline JS **esprima-4.x-safe**
  (no `?.` / `??`). The engagement chart deliberately avoids a dual axis: rates share one % axis,
  volume is a separate bar strip.

**Deploy (per stage, all idempotent):** `sql/deploy_views_tcs.ps1`, `job/deploy_job_tcs.ps1`,
`dash/deploy_dash_tcs.ps1`; full standup `deploy_tcs.ps1`; ingest via
`tools/deploy_ingest_jobs.ps1 -Only tcs-*`. Use `FORCE_REBUILD=1` for view/code/seed changes.
See [`README.md`](README.md) for the secret + quiz-sheet-sharing prerequisites.
