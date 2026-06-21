# Windsor ingest (`services/ingest/`)

## What Windsor.ai is

[Windsor.ai](https://windsor.ai) is a **marketing-data connector platform**: it
authenticates to the marketing sources an agency runs (Google Analytics 4, Google
Ads, Meta/Facebook Ads, and many more) and exposes their metrics through a single
REST API. Instead of integrating each ad platform's bespoke API ourselves, we pull
everything through Windsor with one shared API key.

Windsor is the **only** ingest source in this monorepo. If a new data source is needed,
it arrives as a new Windsor *connector*, not a new ingest mechanism.

## The shared raw layer: `raw_windsor`

Every connector loader lands its rows into the **one shared** BigQuery dataset
`raw_windsor` (project `agora-data-driven`, location `asia-southeast1`). One table per
connector:

| connector    | raw target               |
|--------------|--------------------------|
| `ga4`        | `raw_windsor.ga4`        |
| `google_ads` | `raw_windsor.google_ads` |
| `meta`       | `raw_windsor.meta`       |

`create_dataset.py` creates the `raw_windsor` dataset itself (idempotent). Each
connector sub-directory owns a `create_<x>_table.py` that creates its own table.

Per-client SQL views read **downstream** from these mirror tables (for example a
client's `stg_source` view UNIONs `raw_windsor.ga4` + `raw_windsor.google_ads`). The
connector loaders never know about individual clients -- they only write the shared
raw layer.

## Per-connector sub-directory layout

Each connector `x` lives in `services/ingest/x/` and contains:

```
x/
  x_loader.py        # entrypoint: pull from Windsor REST API -> load raw_windsor.x
  create_x_table.py  # idempotent: create the raw_windsor.x table with its schema
  Dockerfile         # job image; CMD ["python","x_loader.py"]; non-root appuser
  .dockerignore
  requirements.txt   # Windsor ingest pins (google-cloud-* + requests)
  README.md          # one-paragraph purpose + how to run + raw target
```

The loaders read the shared Windsor API key from **Secret Manager** (secret
`windsor-api-key`) via Application Default Credentials -- there is no machine-specific
key path. The Windsor-specific request/parse logic is intentionally left as `# TODO:`
markers: this is a skeleton that the operator adapts to the agency's real Windsor
account, connector ids, and field selections.

## Cadence: daily scheduled pulls

All Windsor connectors are **daily** scheduled pulls, staggered just before the client
export window (so the freshest raw data is present when exports run). They are plain
*writers* of `raw_windsor` -- they are **not** self-gating. Now that the only source is
Windsor (a scheduled REST API), there is no `*/10` self-gating ingest job.

The self-gating lives **downstream in the consumers**: each client EXPORT job (on a
`*/10` tick) and the status dashboard (`*/15`) probe whether `raw_windsor` advanced
past their `_freshness.json` watermark before rebuilding. The ingest jobs just keep the
raw layer fresh on their daily schedule.

## Deploy / schedule

These jobs are built, deployed, and scheduled by
[`tools/deploy_ingest_jobs.ps1`](../../tools/deploy_ingest_jobs.ps1). Its `$JOBS`
array is the **single source of truth** for which connectors exist; the sub-directories
here must match it exactly. The currently-built rows are:

| key                  | dir                                    | job                         | mem   | cpu | cron          |
|----------------------|----------------------------------------|-----------------------------|-------|-----|---------------|
| `windsor-ga4`        | `services/ingest/ga4`         | `windsor-ga4-ingest`        | 1Gi   | 1   | `10 1 * * *`  |
| `windsor-google-ads` | `services/ingest/google_ads`  | `windsor-google-ads-ingest` | 1Gi   | 1   | `15 1 * * *`  |
| `windsor-meta`       | `services/ingest/meta`        | `windsor-meta-ingest`       | 1Gi   | 1   | `20 1 * * *`  |

Additional connector rows (tradedesk, reddit, hubspot, fields) live in the `$JOBS`
array **commented out** -- uncomment each row as its loader is built, rather than
dropping it, so the array stays the canonical list.

Run examples:

```powershell
.\tools\deploy_ingest_jobs.ps1                 # build + deploy + schedule all jobs
.\tools\deploy_ingest_jobs.ps1 -Only windsor-meta
.\tools\deploy_ingest_jobs.ps1 -Run            # also execute each job once after deploy
```

Create the shared dataset once (idempotent; safe to re-run):

```powershell
.\.venv\Scripts\python.exe services\ingest\create_dataset.py
```
