# TCS Klaviyo email-events loader (direct-API) — the diagnostic core

Pulls TCS's **per-recipient** email engagement from the **Klaviyo Events API**
(Received / Opened / Clicked Email) once a day and loads it into the shared raw layer
table **`raw_windsor.tcs_klaviyo_events`** — **one row per send**, flagged `is_open` /
`is_click`. Scheduled writer of `raw_windsor` (not self-gating).

> **Why direct-API, not Windsor:** the Business-Quiz diagnostic asks whether *these quiz
> leads* are opening/clicking **less** over time. That needs per-recipient open/click
> events; Windsor's Klaviyo connector only serves campaign-level aggregates and cannot
> isolate the quiz-lead cohort. Ported from `clients/TCS/archive_code/analytics.py`
> ("Email Activity"). This is the sanctioned exception to "Windsor is the only source".

## How to run

```powershell
.\.venv\Scripts\python.exe services\ingest\create_dataset.py
.\.venv\Scripts\python.exe services\ingest\tcs_klaviyo\create_tcs_klaviyo_events_table.py
.\.venv\Scripts\python.exe services\ingest\tcs_klaviyo\tcs_klaviyo_loader.py
```

Reads the Klaviyo private key from Secret Manager (`tcs-klaviyo-key`) via ADC — provision
with [`services/ingest/tcs_provision_secrets.ps1`](../tcs_provision_secrets.ps1). Backfill
start via env `KLAVIYO_START_DATE` (default `2020-01-01`); the full window is re-pulled and
truncate-loaded each run. Opens/clicks windows extend +7 days past each month to catch
lagging interactions. In production: Cloud Run job `tcs-klaviyo-ingest`, deployed +
scheduled by [`tools/deploy_ingest_jobs.ps1`](../../../tools/deploy_ingest_jobs.ps1).

**Raw target:** `raw_windsor.tcs_klaviyo_events` (`message_id, email, subject, campaign,
flow, sent_at, opened_at, clicked_at, is_open, is_click`).
