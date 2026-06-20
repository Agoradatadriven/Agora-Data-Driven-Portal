# windsor-fields ingest

Daily scheduled pull of Windsor.ai's field/metadata helper connector — the helper that enumerates the available fields (metrics + dimensions) each Windsor connector exposes — landed into the shared raw layer at **`raw_windsor.fields`**. The loader reads the shared `windsor-api-key` from Secret Manager (via the `ingest-runner@` service account / ADC), calls the Windsor.ai fields helper, and WRITE_TRUNCATE-loads the catalogue into BigQuery so per-client SQL views can be validated/documented against the field names Windsor actually offers. It is a plain writer, not self-gating — the freshness gate lives in the downstream export jobs and status dashboard.

This connector is not yet wired: its row in `scripts/deploy_ingest_jobs.ps1` `$JOBS` (`windsor-fields` -> `windsor-fields-ingest`, `512Mi`/`1`, cron `40 1 * * *`) is **commented out** and gets uncommented once the loader is finished and the table is created (`create_fields_table.py`).
