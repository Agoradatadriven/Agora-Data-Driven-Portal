# Windsor Meta connector

Pulls Meta (Facebook Ads) metrics from the **Windsor.ai** `meta` connector once a day
and loads them into the shared raw layer table **`raw_windsor.meta`** (project
`agora-data-driven`, location `asia-southeast1`). This is a scheduled writer of
`raw_windsor` -- it is not self-gating; the client export jobs self-gate downstream.

## How to run

```powershell
# 1. Ensure the shared dataset exists (idempotent; once per project).
.\.venv\Scripts\python.exe ingest\windsor_data_pull\create_dataset.py

# 2. Ensure the raw_windsor.meta table exists (idempotent).
.\.venv\Scripts\python.exe ingest\windsor_data_pull\meta\create_meta_table.py

# 3. Run the loader (truncate-and-load of raw_windsor.meta).
.\.venv\Scripts\python.exe ingest\windsor_data_pull\meta\meta_loader.py
```

The loader reads the shared Windsor API key from Secret Manager (secret
`windsor-api-key`) via Application Default Credentials. In production it runs as the
Cloud Run job `windsor-meta-ingest`, deployed and scheduled (cron `20 1 * * *`) by
[`scripts/deploy_ingest_jobs.ps1`](../../../scripts/deploy_ingest_jobs.ps1).

> The Windsor request and field mapping are left as `# TODO:` markers -- align them to
> the agency's real Windsor Meta connector before the loader can pull live data.

**Raw target:** `raw_windsor.meta` (columns: `date`, `campaign`, `spend`,
`impressions`, `clicks`).
