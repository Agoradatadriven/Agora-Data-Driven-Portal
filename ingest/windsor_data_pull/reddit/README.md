# windsor-reddit ingest

Daily scheduled Windsor.ai pull that lands Reddit Ads spend/delivery data into the shared raw layer at **`raw_windsor.reddit`**. The loader reads the shared `windsor-api-key` from Secret Manager (via the `ingest-runner@` service account / ADC), calls the Windsor.ai REST API for the Reddit Ads connector, and WRITE_TRUNCATE-loads the result into BigQuery; per-client SQL views read from `raw_windsor.reddit` downstream. It is a plain writer, not self-gating — the freshness gate lives in the downstream export jobs and status dashboard.

This connector is not yet wired: its row in `scripts/deploy_ingest_jobs.ps1` `$JOBS` (`windsor-reddit` -> `windsor-reddit-ingest`, `512Mi`/`1`, cron `30 1 * * *`) is **commented out** and gets uncommented once the loader is finished and the table is created (`create_reddit_table.py`).
