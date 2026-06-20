# client_template — BigQuery views

This client's dashboard is fed by three chained BigQuery views in dataset
`client_template` (project `agora-data-driven`, location `asia-southeast1`).
They form stage 1 of the three-stage data contract:

> `sql/*.sql` view column -> `job/main.py` `data` dict key -> `dash/dashboard.html` `data.*` key

## The three views

| file               | view                | what it is |
|--------------------|---------------------|------------|
| `01_stg_source.sql`| `stg_source`        | Typed/filtered rows selected from the shared Windsor mirror `raw_windsor.metrics_daily` (Windsor's blended daily export). For a real client, repoint this view at the actual Windsor connector table(s) for that client — e.g. a `UNION` of `raw_windsor.ga4` + `raw_windsor.google_ads`. Columns: `metric_date, channel, sessions, users, conversions, spend, revenue`. |
| `02_model.sql`     | `daily_performance` | Per-day rollup from `stg_source`. Columns: `metric_date, sessions, users, conversions, spend, revenue, roas` (`roas = revenue / NULLIF(spend, 0)`). |
| `03_kpi.sql`       | `kpi_overview`      | Single-row grand totals over the last 30 days. Columns: `sessions, users, conversions, spend, revenue, roas, days_covered`. |

## The `NN_` ordering rule

Files are applied in **filename order**. The two-digit `NN_` prefix encodes the
dependency chain: a view cannot be created before the view it selects from, so
the staging view (`01_`) must exist before the model (`02_`) reads it, and the
model before the KPI rollup (`03_`). The numeric prefix is the only thing
guaranteeing this — keep it zero-padded so the lexicographic sort matches the
intended order, and never reorder by hand.

## How views are applied

Run `create_views.py` (with the repo `.venv` Python). It reads every `*.sql`
file in this directory as UTF-8 (so non-ASCII characters in SQL filters
survive) and applies them in `NN_` order via the BigQuery client, printing
`[OK]` per applied view.

Views are **never** edited directly in the BigQuery console — the console copy
would drift from this repo. The repo is the single source of truth; re-apply
with `deploy_views_template.ps1`.
