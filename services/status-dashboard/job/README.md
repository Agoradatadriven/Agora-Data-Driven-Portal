# Status dashboard export job (agency-wide freshness monitor)

This Cloud Run job is the **meta** dashboard's data builder. Unlike a per-client export job it has
**no BigQuery dataset and no SQL views of its own** — it does not transform marketing data. Instead
it watches every client's exported data JSON and reports how fresh each one is, then writes a single
`status.json` to the status bucket.

```
client export jobs  --write-->  agora-data-driven-<c>-dash/<c>.json   (per client)
status export job   --scans-->  every client bucket
                    --writes->  agora-data-driven-status-dash/status.json
status dash service --proxies-> status.json to authed sessions only
```

## What it produces

A single private JSON object, `gs://agora-data-driven-status-dash/status.json`:

```jsonc
{
  "generated_at": "<ISO build time, UTC second precision>",
  "clients": [
    {
      "client":          "<client key derived from the bucket name>",
      "last_updated":     "<data.last_updated from that client's <key>.json>",
      "data_through":     "<data.data_through from that client's <key>.json>",
      "last_json_update": "<GCS updated time of the <key>.json blob>",
      "lag_minutes":      "<int minutes between last_json_update and now>",
      "stale":            "<bool: lag exceeds the stale threshold>"
    }
  ]
}
```

The object is uploaded with `Cache-Control: no-store`. The status JSON is **never** public — the
status dash web service proxies it at `/data.json` only to authenticated sessions.

## How it monitors (no registry, no database)

The job enumerates clients **by listing GCS buckets**, not from any registry or database:

1. It lists buckets matching `agora-data-driven-*-dash` and **excludes** the platform bucket
   (`agora-data-driven-platform-dash`) and its **own** status bucket
   (`agora-data-driven-status-dash`) — those are not clients.
2. It derives each **client key** from the bucket name (`agora-data-driven-<c>-dash` → `<c>`).
3. For each client bucket it reads:
   - the `<key>.json` data blob → `last_updated`, `data_through`;
   - that blob's GCS `updated` time → `last_json_update` (the truest "last refresh" signal — it is
     independent of whatever the client wrote inside `last_updated`);
   - the client's own `_freshness.json` watermark sidecar (confirms the export ran at least once).
4. It computes `lag_minutes` (now − `last_json_update`) and a `stale` flag (lag over the threshold,
   or no JSON at all). A missing or mid-write/corrupt blob never crashes the build — that client
   simply shows blank fields and a stale flag.

## The freshness gate (self-gating)

Like the client export jobs, this job **self-gates**. It runs on a `*/15` Cloud Scheduler tick but
only rebuilds when the upstream it gates on has actually advanced:

1. It probes the **base** `raw_windsor` mirror table(s) — `GATING_TABLES =
   ["raw_windsor.metrics_daily"]` — for their `last_modified_time`. It probes the BASE/MIRROR
   tables, **never a view**. We gate on the same base mirror that drives the client exports, so the
   status page advances on roughly the same cadence the client data does.
2. It compares those timestamps against the watermark in the **status** bucket: the `_freshness.json`
   sidecar. There is no separate freshness store and no database.
3. `is_stale(observed, watermark)` is True if any observed timestamp is newer, or a probed table is
   absent from the watermark. An **empty** observation set returns False, so a broken/empty probe
   never burns a rebuild.
4. If not stale, the job prints a "fresh, no rebuild" line and exits without touching the bucket.
5. The watermark is advanced **only after a successful upload**. If the upload fails, the watermark
   is left untouched and the next tick retries.

Set `FORCE_REBUILD=1` to bypass the gate — used for code/view-only/seed changes that do not advance
the upstream watermark and would otherwise no-op while serving stale JSON. `deploy_job_status.ps1`
runs the job once with `FORCE_REBUILD=1` right after deploying, precisely because a fresh deploy is a
code change, not an upstream-data change.

## IAM requirement — objectViewer on EVERY client bucket

The status job runs as **`status-dash-job@agora-data-driven.iam.gserviceaccount.com`**. To read each
client's `<key>.json` + `_freshness.json` it needs **`roles/storage.objectViewer` on every client
bucket** (`agora-data-driven-<c>-dash`). The top-level `deploy_status.ps1` standup grants that by
**iterating the existing `agora-data-driven-*-dash` client buckets** and adding the binding to each.

It also needs:
- `roles/bigquery.jobUser` (project) to run the `__TABLES__` probe query against `raw_windsor`, and
- `roles/storage.objectAdmin` on its **own** status bucket to write `status.json` + `_freshness.json`.

**When a NEW client bucket is created later, re-run `deploy_status.ps1`** (or grant
`status-dash-job@` `objectViewer` on the new bucket directly) — otherwise the monitor cannot see the
new client and it will be missing from `status.json`.

## Deploy

Deploy manually, as yourself (never Cloud Build from a laptop — the Cloud Build SA cannot
`iam.serviceAccounts.actAs` the runtime SA):

```powershell
.\deploy_job_status.ps1            # build + deploy + run once (FORCE_REBUILD=1)
.\deploy_job_status.ps1 -SkipBuild # reuse the current image; redeploy + run once
```
