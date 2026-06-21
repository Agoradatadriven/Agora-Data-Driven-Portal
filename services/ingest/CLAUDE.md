# CLAUDE.md — services/ingest (Windsor connector loaders)

**Rules live in the repo-root [`/CLAUDE.md`](../../CLAUDE.md)** — read it first; this file only adds
local context. If they disagree, root wins.

These are the **writers of the shared `raw_windsor` BigQuery dataset** — the only raw layer. Each
connector (`ga4`, `google_ads`, `meta`, `tradedesk`, `reddit`, `hubspot`, `fields`) is a Cloud Run
job that pulls from the Windsor.ai REST API and loads `raw_windsor.*`.

- **Scheduled daily pulls, NOT self-gating.** They WRITE `raw_windsor`; the self-gating lives
  downstream in the client export jobs (`*/10`) and the status dashboard (`*/15`), which probe whether
  `raw_windsor` advanced before rebuilding.
- **`tools/deploy_ingest_jobs.ps1` is the only script that touches production ingest.** Its `$JOBS`
  array is the **single source of truth** for which connectors exist + their cron — the directories
  here must match it. Uncomment a `$JOBS` row as its loader is built.
- Each connector dir is self-contained (`<x>_loader.py`, `create_<x>_table.py`, `Dockerfile`,
  `requirements.txt`). The Windsor API key is mounted from Secret Manager (`windsor-api-key`).
