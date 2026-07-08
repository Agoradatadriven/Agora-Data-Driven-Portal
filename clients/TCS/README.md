# TCS — Business Quiz dashboard (Agora Atrium client)

TCS (Contract Shop) rebuilt on the Atrium three-stage contract. This first dashboard is
**diagnostic**: understand the behaviour of quiz-takers — especially those who bought — and
explain why this year's leads aren't converting (**are they opening less? clicking less?**).

The old pipeline (a Colab notebook, preserved read-only in [`archive_code/analytics.py`](archive_code/analytics.py))
pulled everything by direct API into a personal BigQuery project. This rebuild keeps the proven
pull logic but lands it on the shared platform and models it as views → export job → dashboard.

## Data source — a documented exception to "Windsor is the only source"

TCS's three sources (Shopify orders, **per-recipient** Klaviyo events, the Business-Quiz sheet)
do not flow through Windsor, and the diagnostic needs per-recipient open/click events — a grain
Windsor's Klaviyo connector does not serve. So TCS uses **direct-API ingest loaders** that write
the shared raw layer, `raw_windsor.tcs_*`. See [`services/ingest/tcs_shopify`](../../services/ingest/tcs_shopify),
[`tcs_klaviyo`](../../services/ingest/tcs_klaviyo), [`tcs_quiz`](../../services/ingest/tcs_quiz).

## The three-stage contract (matched BY NAME)

```
services/ingest/tcs_*  ->  raw_windsor.tcs_*   (direct-API mirrors)
        |
   sql/*.sql (view column)  ->  job/main.py (data dict key)  ->  dash/dashboard.html (data.* key)
```

### Stage 1 — SQL views (`sql/`, applied in NN_ order by `create_views.py`)

| view | what it is |
|------|-----------|
| `01_stg_quiz`          | one row per LEAD (email), first quiz submission + cohort fields |
| `02_stg_orders`        | typed Shopify orders, keyed on buyer email |
| `03_stg_email_events`  | per-recipient Klaviyo sends, flagged is_open / is_click |
| `04_quiz_conversion`   | per lead: orders at/after quiz (5-min buffer), revenue, days-to-buy |
| `05_quiz_engagement`   | per lead: post-quiz sends/opens/clicks + rates |
| `06_quiz_leads`        | the FACT view (one row per lead) = old DASHBOARD_quiz_nurture_analysis |
| `07_engagement_monthly`| **the diagnostic time series**: open/click rate by month, split by converted |
| `08_cohort_performance`| per quiz-cohort year: conversion + engagement |
| `09_kpi_overview`      | single-row headline KPIs (incl. this-year-vs-prior open/click rate) |

> Grain note: leads are keyed to their FIRST quiz submission (unlike the old per-submission
> model), so repeat submitters don't double-count engagement.

### Stage 2 — export job (`job/main.py`)

Reads the views and assembles `tcs.json` (`kpis`, `monthly`, `cohorts`, `leads`), uploaded to the
private bucket `agora-data-driven-tcs-dash`. Self-gates on `_freshness.json` vs the
`raw_windsor.tcs_*` GATING_TABLES; `FORCE_REBUILD=1` bypasses for view/code changes.

### Stage 3 — dashboard (`dash/dashboard.html`)

Self-contained, dark `--ag-*` theme, esprima-4.x-safe inline JS (no `?.` / `??`). Panels: KPI strip
+ this-year callout; **open/click-rate-over-time** (two lines, one % axis) with a volume bar strip
below; **buyers vs non-buyers** open-rate lines; cohort table; leads drill-down.

## Prerequisites before deploy

1. **Secrets:** run [`services/ingest/tcs_provision_secrets.ps1`](../../services/ingest/tcs_provision_secrets.ps1)
   to create `tcs-shopify-token` + `tcs-klaviyo-key` and grant `ingest-runner@` access.
2. **Quiz sheet:** enable Sheets + Drive APIs and share the Business-Quiz sheet (Viewer) with
   `ingest-runner@agora-data-driven.iam.gserviceaccount.com`.
3. **Register ingest jobs:** add the three `tcs-*` rows to `$JOBS` in
   [`tools/deploy_ingest_jobs.ps1`](../../tools/deploy_ingest_jobs.ps1) (see that file's array).

## Deploy (manual, run-as-yourself)

```powershell
# 0. secrets + sheet share (once)               -> services/ingest/tcs_provision_secrets.ps1
# 1. land the raw layer
.\tools\deploy_ingest_jobs.ps1 -Only tcs-shopify -Run
.\tools\deploy_ingest_jobs.ps1 -Only tcs-klaviyo -Run
.\tools\deploy_ingest_jobs.ps1 -Only tcs-quiz    -Run
# 2. full client standup (dataset, bucket, SAs, secrets, views, job, scheduler, dash)
.\clients\TCS\deploy_tcs.ps1
# per-stage iteration afterwards:
.\clients\TCS\sql\deploy_views_tcs.ps1     # views changed  (FORCE_REBUILD)
.\clients\TCS\job\deploy_job_tcs.ps1       # data logic changed
.\clients\TCS\dash\deploy_dash_tcs.ps1     # dashboard changed (runs the JS gate first)
```

Then map `tcs.agoradatadriven.com` to the `tcs-dash` service and (optionally) wire SSO with
`tools\enable_platform_sso.ps1 -Keys tcs`.

## Later

Orders / Sessions / full-email dashboards from the old notebook are a later standup on this same
`tcs` client (more views + more `data.*` keys + more panels — same contract).
