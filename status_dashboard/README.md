# Status dashboard — agency-wide freshness monitor

The status dashboard is Agora Data Driven's **meta** dashboard. It does not show marketing data; it
shows how fresh every client's marketing data is. It reuses the exact same serving pattern as a
client dashboard (private bucket + Flask password gate, deployed `--no-invoker-iam-check`, SSO
additive) but it has **no BigQuery dataset and no SQL views of its own** — there is nothing to
transform, only other dashboards to watch.

```
client export jobs  --write-->  agora-data-driven-<c>-dash/<c>.json     (per client)
status export job   --scans-->  every client bucket
                    --writes->  agora-data-driven-status-dash/status.json
status dash service --proxies-> status.json to authed sessions only
```

## Pieces

| Piece                | What it is                                                                 |
|----------------------|---------------------------------------------------------------------------|
| `job/`               | the `status-export` Cloud Run job — the monitor that builds `status.json`. |
| `dash/`              | the `status-dash` Cloud Run web service — private, app-level auth.         |
| `deploy_status.ps1`  | one-shot idempotent standup of the whole stack (run as yourself).          |
| `scheduler.ps1`      | create/refresh the `*/15` self-gating scheduler `status-export-daily`.     |

## Self-gating on a `*/15` tick

Like the client export jobs, the status job is **self-gating**. It runs on a `*/15` Cloud Scheduler
tick but only rebuilds when the shared `raw_windsor` mirror table it gates on
(`raw_windsor.metrics_daily`) advances past the `_freshness.json` watermark stored in the **status**
bucket. Most ticks therefore no-op cheaply; the page refreshes on roughly the same cadence the client
data does. `FORCE_REBUILD=1` bypasses the gate for code/seed changes that do not advance upstream.

We probe the BASE Windsor mirror table — **never a view** — because a view has no last-modified time
of its own. The status dash owns no views regardless; it gates on the same base mirror that drives
the client exports.

## How it monitors every client (no registry, no database)

The status job enumerates clients **by listing GCS buckets**, not from any registry or database. It
lists `agora-data-driven-*-dash`, **excludes** the platform bucket
(`agora-data-driven-platform-dash`) and its own status bucket (`agora-data-driven-status-dash`), and
derives each client key from the bucket name. For each client it reads the `<key>.json` data blob
(`last_updated`, `data_through`), the blob's GCS `updated` time (`last_json_update`), and the client's
own `_freshness.json` watermark, then computes `lag_minutes` and a `stale` flag.

Because the job reads **every** client bucket, its service account
(`status-dash-job@agora-data-driven.iam.gserviceaccount.com`) needs
**`roles/storage.objectViewer` on every client bucket**. `deploy_status.ps1` grants that by iterating
the existing `agora-data-driven-*-dash` buckets. **Re-run `deploy_status.ps1` whenever a new client
bucket is created** so the monitor's job SA gains read on it — otherwise the new client is silently
missing from `status.json`.

## Security

`status.json` is **private** — it lives in a private, public-access-prevented bucket and the
`status-dash` service proxies it at `/data.json` only to authenticated sessions. The org forbids
public Cloud Run, so the service is deployed with `--no-invoker-iam-check` (never
`--allow-unauthenticated`) and does its own password/SSO auth in-process.

## Deploy

Run as yourself (never Cloud Build from a laptop):

```powershell
.\deploy_status.ps1                    # full standup: prompts for the status dashboard password
.\deploy_status.ps1 -Password "s3cret" # pass it inline (or set $env:DASH_PASSWORD)
.\scheduler.ps1                        # refresh just the */15 scheduler
```
