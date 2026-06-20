# `template` export job (Stage 2 of the data contract)

This Cloud Run job is the middle stage of Agora Data Driven's three-stage data contract. It reads
the per-client BigQuery views, assembles the dashboard `data` payload, and uploads it as the private
`template.json` object to the client's bucket.

```
Stage 1  sql/*.sql views        -- typed/filtered rows from the shared raw_windsor mirror
Stage 2  job/main.py (HERE)     -- assemble the `data` dict, upload template.json
Stage 3  dash/dashboard.html    -- read data.* and render the dashboard
```

The three stages are matched **by name**: the keys this job writes into `data` (and into each row of
`data["daily"]`) are exactly the `data.*` keys `dashboard.html` reads. Renaming a column in a view,
a key here, or a `data.*` reference in the dashboard breaks the chain silently — keep all three in
step.

## What it produces

A single private JSON object, `gs://agora-data-driven-template-dash/template.json`:

```jsonc
{
  "client": "template",
  "last_updated": "<ISO build time, UTC second precision>",
  "data_through": "<newest upstream timestamp, ISO>",
  "kpis": { "sessions", "users", "conversions", "spend", "revenue", "roas", "days_covered" },
  "daily": [ { "metric_date", "sessions", "users", "conversions", "spend", "revenue", "roas" }, ... ]
}
```

The object is uploaded with `Cache-Control: no-store`. The data JSON is **never** public — the web
service proxies it at `/data.json` only to authenticated sessions.

## The freshness gate (self-gating)

The job runs on a `*/10` Cloud Scheduler tick but does **not** rebuild on every tick. It only
rebuilds when the upstream data it reads has actually advanced:

1. It probes the **base** `raw_windsor` mirror table(s) the views read — `GATING_TABLES =
   ["raw_windsor.metrics_daily"]` — for their `last_modified_time`. It probes the BASE/MIRROR
   tables, **never a view** (a view has no last-modified time of its own).
2. It compares those observed timestamps against the watermark stored in this client's **own**
   bucket: the `_freshness.json` sidecar. There is no separate freshness store and no database.
3. `is_stale(observed, watermark)` is True if any observed timestamp is newer than the watermark, or
   a probed table is absent from the watermark. An **empty** observation set returns False, so a
   broken/empty probe never burns a rebuild — we would rather serve slightly stale data than rebuild
   blindly on a probe failure.
4. If not stale, the job prints a "fresh, no rebuild" line and exits without touching the bucket.
5. The watermark is advanced **only after a successful data upload**. If the upload fails, the
   watermark is left untouched and the next tick retries.

Why the consumer self-gates (and not the Windsor loaders): the Windsor ingest jobs are scheduled API
pulls — they are the *writers* of `raw_windsor`, so there is nothing upstream of them to gate
against. The self-gating lives in the consumers (this export job, and the status dashboard).

## `FORCE_REBUILD`

Set `FORCE_REBUILD=1` in the environment to **bypass** the freshness gate and rebuild
unconditionally. Use it for changes that do **not** advance the upstream watermark and would
otherwise no-op while serving stale JSON:

- a SQL view edit (Stage 1 logic changed, but `raw_windsor` did not),
- a code change to this job,
- a seed/manual reprocess.

`deploy_job_template.ps1` executes the job once with `FORCE_REBUILD=1` immediately after deploying,
precisely because a fresh deploy is a code change, not an upstream-data change. Routine scheduled
ticks run **without** the flag and self-gate on the watermark.

## Deploy

Deploy manually, as yourself (never Cloud Build from a laptop — the Cloud Build SA cannot
`iam.serviceAccounts.actAs` the runtime SA):

```powershell
.\deploy_job_template.ps1            # build + deploy + run once (FORCE_REBUILD=1)
.\deploy_job_template.ps1 -SkipBuild # reuse the current image; redeploy + run once
```

The `cloudbuild.yaml` in this directory is present only for a future push-to-main Cloud Build
trigger and is **unused** from a laptop.
