# CLAUDE.md — Agora Data Driven (canonical agent fast-path)

This is the file Claude Code auto-loads. It is the single source of truth for fixed facts, the
data contract, the deploy procedure, and the guardrails. The pointer at `.claude/CLAUDE.md` defers
to this file; if they ever disagree, **this file wins — update both so they agree again.**

## Overview

Agora Data Driven is a marketing agency that self-hosts password-gated client marketing dashboards
on Google Cloud Platform, fronted by a client portal that is growing into a full CRM.

- **One repeatable pattern, many clients.** Every client is fully derived from a short key `<c>`
  (see the derivation rule below). One GCP project, one region, one shared Artifact Registry repo.
- **`template` is the worked example.** `clients/client_template/` is the canonical pattern every
  new client copies — three SQL views, an export job, and a dashboard web service.
- **The portal/CRM front-door** (`agora-platform/`, served at `portal.agoradatadriven.com`) is a
  reverse proxy + single login over all dashboards, with a registry stored as one private JSON in
  GCS. It is designed to grow into a CRM (see the `# CRM:` markers in `agora-platform/dash/main.py`).
  **Agora Atrium** — the co-branded client workspace — is built into this same `platform-dash`
  service (see the Agora Atrium section below).
- **Windsor.ai is the only data source.** Connector loaders in `ingest/windsor_data_pull/` land
  source data into the shared `raw_windsor` BigQuery dataset; per-client SQL views read from there.

## Fixed facts (use literally — never invent alternatives)

| Fact | Value |
|------|-------|
| GCP project | `agora-data-driven` |
| Region | `asia-southeast1` (Singapore) — **everything lives here, one region, never another** |
| Artifact Registry repo | `agora` |
| Shared raw dataset | `raw_windsor` (the only raw layer; written by Windsor connectors) |
| Portal host | `portal.agoradatadriven.com` |
| Client dashboards | `<c>.agoradatadriven.com` |
| SSO cookie scope | `.agoradatadriven.com` (leading dot) |
| Local dev | Windows + PowerShell; repo venv python at `.\.venv\Scripts\python.exe` |

`PROJECT_NUMBER` is **never hardcoded** — resolve it at runtime:
`gcloud projects describe agora-data-driven --format='value(projectNumber)'`.

**Per-client derivation rule** (derive, never re-type) for a key `<c>`: dataset `client_<c>`,
bucket `agora-data-driven-<c>-dash`, export job `<c>-export`, web service `<c>-dash`, job SA
`<c>-dash-job@agora-data-driven.iam.gserviceaccount.com`, web SA `<c>-dash-web@…`, password secret
`<c>-dash-password`, session secret `<c>-dash-session-key`, subdomain `<c>.agoradatadriven.com`,
data object `<c>.json` + freshness sidecar `_freshness.json` in the client's bucket.

## Repo layout

- `clients/` — one folder per client; `client_template/` is the worked pattern (`sql/`, `job/`,
  `dash/`, deploy scripts, README).
- `ingest/windsor_data_pull/` — Windsor connector loaders (`ga4`, `google_ads`, `meta`, `tradedesk`,
  `reddit`, `hubspot`, `fields`) that write `raw_windsor.*`. Scheduled API pulls, not self-gating.
- `agora-platform/` — the portal/CRM front-door (`platform-dash`). Also hosts **Agora Atrium**
  (`dash/workspace.py`, `seed_workspace.py`, `notify.py`, `atrium_view.py`, `templates/atrium.html`
  + `admin_atrium.html`). The brand kit lives in `Creatives/` (logo set, `brand.json`/`brand.md`);
  `dash/brand.py` is the bundled runtime copy of the AGORA mark + official palette (the container
  can't read `Creatives/`), used by the portal/login chrome and as `seed_workspace.py`'s fallback.
- `status_dashboard/` — meta dashboard monitoring every client's freshness (no dataset/views).
- `scripts/` — operator tooling: `setup.ps1` (one-time laptop setup), `start_day.ps1` (per-session
  preflight), `deploy_ingest_jobs.ps1` (the one script that touches production ingest),
  `enable_platform_sso.ps1`, `enable_super_admin.ps1`, `_validate_dash_js.py`.

## Dashboard edits

Each dashboard is **one big self-contained `dash/dashboard.html`** (no build step, no external JS).
Grep for the metric or label you want to change and edit in place. Theme colors are CSS custom
properties in `:root` (the `--ag-*` palette). Inline JS must stay **esprima-4.x-safe**: no optional
chaining `?.` and no nullish coalescing `??` (the pre-deploy gate `scripts/_validate_dash_js.py`
parses it with esprima, which predates those tokens). Use classic `&&`/`||` guards.

## Agora Atrium (client workspace in the portal)

Atrium is the co-branded client workspace built **into** `platform-dash` — **additive**, reusing the
existing session auth, bucket, and runtime SA. **No new infra/IAM/bucket/secret/service.** Product
name is one constant: `WORKSPACE_NAME` in `agora-platform/dash/main.py`.

- **State = one private JSON per client (no database):** `workspace/<c>.json` in the **registry
  bucket** `agora-data-driven-platform-dash`. `dash/workspace.py` is the only reader/writer
  (last-write-wins, mirrors `store.py`); it imports `google-cloud-storage` lazily and supports a
  local-fs backend via `WORKSPACE_LOCAL_DIR` (+ `WORKSPACE_BUCKET`/`WORKSPACE_PREFIX`) so it is
  testable off-cloud. Shape: `metrics`, `today`, `split`, `series`, `activity`, `campaigns[]`
  (`strategy`/`ai_summary` + `content[]` with status `awaiting|approved|changes` + `client_note`),
  `calendar[]`, `conversations[]` (`client`/`agora` messages), per-user `notify` prefs.
- **Routes (all behind existing session auth):** client `GET /w/<c>/` + `/w/<c>/<tab>` (overview,
  dashboard, leadgen, organic, calendar, conversations, settings) gated `authed()`+`can_open(<c>)`;
  POSTs `/w/<c>/{approve,request-changes,save-note,send-message,save-notify}`. Team console
  `/admin/atrium[/<c>][/campaign|content|conversation|reply|metrics]` gated `is_superadmin()`. The
  portal landing shows **Open workspace** beside **Open dashboard**.
- **Notifications are optional & graceful** (`dash/notify.py`, mirrors `feedback_ai.py`): default
  records an activity entry + logs to stdout; real email only when **both** `ATRIUM_EMAIL_ENABLED=1`
  and `ATRIUM_EMAIL_API_KEY` (Secret-Manager) are set, SDK imported lazily. **No provider key
  committed.** Team inbox `ATRIUM_TEAM_EMAIL` (default `info@agoradatadriven.com`).
- **Theme/JS:** the official brand **light** theme — Data Green `#4FAB4A` + Accent Purple `#9484FB`
  (deep companion `#5C4BD0` for white-text fills), on a white canvas with bold black type. The whole
  front-door (login, portal, team console) shares it; Atrium scopes every selector under `.atrium` so
  it stays self-contained. The logo is `ws.brand.agora_logo` (seeded) in Atrium and `dash/brand.py`
  elsewhere. Inline JS is esprima-4.x-safe and reads state from the DOM (no Jinja in any script block).
- **Ships via the SAME deploy as the portal:** `agora-platform/dash/deploy_dash_platform.ps1` (build
  as yourself → `gcloud run deploy platform-dash --no-invoker-iam-check`). Validate templates with
  `scripts/_validate_dash_js.py` first. Seed the demo once:
  `.\.venv\Scripts\python.exe agora-platform\dash\seed_workspace.py` (idempotent; writes
  `workspace/riverdance.json`, refuses to clobber). Local tests: `dash/_workspace_localtest.py`
  (data) and `dash/_atrium_smoketest.py` (full route+template, stubs GCS).

## The data contract (three stages, matched BY NAME)

```
sql/*.sql  (view column)  ->  job/main.py  (assembled `data` dict key)  ->  dash/dashboard.html  (data.* key)
```

Adding a metric is usually three edits, one per stage. **Renaming a key in one stage breaks the
next** — the names must match exactly. For `template` the chain is: `kpi_overview` /
`daily_performance` columns → `data["kpis"].*` / `data["daily"][].*` → `data.kpis.*` / `data.daily`.

## Redeploy after an edit — MANUAL, never cloudbuild from a laptop

Deploys are manual: build the image as yourself, then deploy. A laptop must **never** trigger Cloud
Build to deploy, because the Cloud Build SA cannot `iam.serviceAccounts.actAs` the runtime SA
(`gcloud builds submit --tag` to build an image is fine; it is the *deploy-as-the-runtime-SA* step
that fails). Use the per-stage scripts (all resolve paths from `$PSScriptRoot`, all idempotent):

- **View/SQL change** → `clients/client_template/sql/deploy_views_template.ps1`
  (reapplies views via `create_views.py`, then re-runs the export job with `FORCE_REBUILD=1`).
- **Job / data-assembly change** → `clients/client_template/job/deploy_job_template.ps1`
  (build image → `gcloud run jobs deploy template-export` → execute with `FORCE_REBUILD=1`).
- **Dashboard / web change** → `clients/client_template/dash/deploy_dash_template.ps1`
  (validate JS → build → `gcloud run deploy template-dash … --no-invoker-iam-check`).
- **Full standup of a new client** → copy `client_template`, then `deploy_template.ps1`.

`FORCE_REBUILD=1` is mandatory for view-only / code / seed changes: they do **not** advance the
upstream watermark, so without it the freshness gate no-ops and keeps serving stale JSON.

Org policy (Domain Restricted Sharing) rejects `--allow-unauthenticated`; all web services deploy
with `--no-invoker-iam-check` and do their own password/SSO auth in-process.

## Freshness contract (binding)

1. **Self-gating on a tick.** Each client export job (and the status dashboard) runs on its Cloud
   Scheduler tick (`*/10 * * * *` for exports, `*/15` for status) but only rebuilds when the shared
   `raw_windsor` mirror tables it reads advanced past a stored watermark. The Windsor ingest jobs
   are NOT self-gating — they are scheduled API pulls that WRITE `raw_windsor`.
2. **The watermark is a sidecar in the client's OWN bucket** — a `_freshness.json` object in
   `agora-data-driven-<c>-dash`. There is no separate freshness store and no database.
3. **Probe the BASE/MIRROR tables the views read — never watermark a VIEW.** A view has no
   last-modified time; watermark the `raw_windsor` mirror/base tables the views select from.
4. **`is_stale(observed, watermark)` returns True** if any observed upstream timestamp is newer than
   the watermark OR a probed key is absent. An **empty** observation set returns **False**, so a
   broken/empty probe never burns a rebuild.
5. **Write the watermark only AFTER a successful data upload.** `FORCE_REBUILD=1` bypasses the gate.

`freshness.py` signature (vendored identically into every export job):

```python
probe_bq_last_modified(bq, tables, location)       # __TABLES__.last_modified_time, keyed "dataset.table"
read_watermark(bucket, object_name)                # GCS JSON sidecar -> dict
write_watermark(bucket, object_name, observed)     # GCS JSON sidecar <- dict
is_stale(observed, watermark)                       # True if anything advanced or a key is missing
```

## Never

- **Never commit secrets.** Keys, `.p8`/`.pem`, `*credentials*.json`, `.env` are gitignored — keep
  it that way. Write secret material via UTF-8 (no BOM, no trailing newline) temp files.
- **Never make the data JSON public.** It is served only through the authenticated `/data.json`
  proxy. Buckets stay private.
- **Never edit views in the BigQuery console.** Views are code: edit `sql/*.sql` and reapply with
  `create_views.py`. The console is not the source of truth.
- **Never deploy via Cloud Build from a laptop**, and never use `--allow-unauthenticated`.

## Keep this file current

Updating docs is part of finishing a task — if a change alters the contract, the layout, or the
deploy steps, update this file (and the `.claude/CLAUDE.md` pointer) in the same change. **Volatile
status** (live URLs, dates, per-client deploy state) belongs in a README, never in CLAUDE.md.
