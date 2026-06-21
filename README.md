# Agora Data Driven — dashboards + portal/CRM monorepo

Agora Data Driven self-hosts password-gated client marketing dashboards on Google Cloud Platform,
behind a single client portal that is growing into a CRM. This repo holds the whole platform: one
worked-example client, the shared Windsor ingest layer, the portal front-door, a status dashboard,
and the operator tooling that runs it all from a Windows laptop.

> New here? Read **[`ONBOARDING.md`](ONBOARDING.md)** for the step-by-step first-day setup.
> Working with Claude Code? **[`CLAUDE.md`](CLAUDE.md)** is the canonical fast-path.
> On a team? **[`docs/dev-workflow.md`](docs/dev-workflow.md)** is how we branch → PR → CI → merge.

## Architecture at a glance

```
Windsor.ai ──(ingest loaders)──▶ raw_windsor (shared BigQuery dataset)
                                        │
                          per-client SQL views (sql/*.sql)
                                        │
                          export job (job/main.py)  ──▶  <c>.json in agora-data-driven-<c>-dash (private)
                                        │
                          dash web service (dash/main.py)  ──▶  <c>.agoradatadriven.com  (login + /data.json proxy)
                                        │
                          portal (platform-dash)  ──▶  portal.agoradatadriven.com  (single login over all dashboards)
```

- **One repeatable pattern, many clients** — every client is derived from a short key `<c>`
  (dataset `client_<c>`, bucket `agora-data-driven-<c>-dash`, job `<c>-export`, service `<c>-dash`,
  subdomain `<c>.agoradatadriven.com`, etc.). One project (`agora-data-driven`), one region
  (`asia-southeast1`), one Artifact Registry repo (`agora`).
- **Three-stage data contract, matched by name** — `sql/*.sql` view column → `job/main.py` `data`
  dict key → `dash/dashboard.html` `data.*` key. See [`CLAUDE.md`](CLAUDE.md).
- **Security = private bucket + Flask password gate** — the data JSON is never public; the web
  service proxies it at `/data.json` only to authenticated sessions.
- **Self-gating freshness** — export jobs run on a `*/10` tick but only rebuild when `raw_windsor`
  advanced past a `_freshness.json` watermark in the client's own bucket (`*/15` for status).
- **Portal SSO** — `platform-dash` mints a signed cookie scoped to `.agoradatadriven.com`;
  dashboards trust it additively, so each dashboard's own password always still works.

## Repo layout

| Folder | What it is |
|--------|-----------|
| `services/portal/` | The portal/CRM front-door (`platform-dash`): reverse proxy + single login, registry-as-one-private-JSON, super-admin console, and **Agora Atrium** (the co-branded client workspace). `deploy.ps1` stands it up. |
| `services/ingest/` | Windsor connector loaders (`ga4`, `google_ads`, `meta`, `tradedesk`, `reddit`, `hubspot`, `fields`) that write the shared `raw_windsor` dataset. |
| `services/status-dashboard/` | Meta dashboard monitoring every client's freshness (no dataset/views of its own). |
| `clients/client_template/` | The worked-example client. Copy it to start a new one. `sql/` (3 views) · `job/` (self-gating export) · `dash/` (Flask + `dashboard.html`) · deploy scripts. |
| `assets/` | Brand kit — logo set, `brand.json`/`brand.md`, per-client `clients/<c>.svg`. The seed inlines these into each workspace. |
| `tools/` | Operator tooling for the Windows laptop — see [`tools/README.md`](tools/README.md). Includes `push-branch.ps1` / `merge-branches.ps1` (the team workflow). |
| `preview/` | Double-click local-preview launchers (super-admin on `:8080`, client-login on `:8081`). |
| `docs/` | Deeper docs — [`docs/dev-workflow.md`](docs/dev-workflow.md) is the branch → PR → CI → merge flow. |

## Quickstart (Windows operator)

```powershell
# 1. One-time machine setup (installs Python 3.12 + gcloud, builds .venv, logs in twice).
#    Double-click tools\setup.cmd, or:
powershell -ExecutionPolicy Bypass -File tools\setup.ps1

# 2. Every working session — ~30s preflight that reauths whichever gcloud login expired.
#    Double-click tools\start_day.cmd, or:
powershell -ExecutionPolicy Bypass -File tools\start_day.ps1

# 3. Run a Windsor ingest loader locally (writes raw_windsor.*), using the repo venv:
.\.venv\Scripts\python.exe services\ingest\ga4\ga4_loader.py
```

From there, deploy with the per-stage scripts (all idempotent, all run as yourself):

```powershell
# Stand up the whole template client (APIs, SAs, secrets, views, job, scheduler, dash):
powershell -ExecutionPolicy Bypass -File clients\client_template\deploy_template.ps1

# Deploy/refresh the shared Windsor ingest jobs (the one script that touches production ingest):
powershell -ExecutionPolicy Bypass -File tools\deploy_ingest_jobs.ps1

# Stand up the portal, then wire SSO + the super-admin console:
powershell -ExecutionPolicy Bypass -File services\portal\deploy.ps1
powershell -ExecutionPolicy Bypass -File tools\enable_platform_sso.ps1
powershell -ExecutionPolicy Bypass -File tools\enable_super_admin.ps1
```

> Deploys are **manual** — build the image, then deploy as yourself. Never trigger Cloud Build from
> a laptop to deploy (the Cloud Build SA cannot `actAs` the runtime SA), and never use
> `--allow-unauthenticated` (org policy rejects it; the apps do their own auth).

## Working on a team

Each developer pushes to their own per-machine branch and opens a PR; CI gates every merge to `main`.

```powershell
.\tools\push-branch.ps1 -Dev alex      # first time: sets your name -> branch alex/work
.\tools\push-branch.ps1                # thereafter: push your work, open a PR on GitHub
.\tools\merge-branches.ps1             # integrate everyone's branches safely (stops for conflicts)
```

Full process — including making the CI check required — is in [`docs/dev-workflow.md`](docs/dev-workflow.md).

## Cross-platform note (macOS / Linux)

The committed source is portable as-is — the per-unit Dockerfiles, Python, and SQL build and run
anywhere. Only the **operator scripts** are PowerShell `.ps1`/`.cmd` (the team runs Windows). The
manual equivalent of `setup.ps1` / `start_day.ps1` on a mac/Linux box is:

```bash
# Install the Google Cloud SDK and Python 3.12 with your package manager, then:
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r clients/client_template/job/requirements.txt

gcloud auth login                                   # CLI credentials
gcloud auth application-default login               # ADC, used by the python client libs
gcloud config set project agora-data-driven
gcloud auth application-default set-quota-project agora-data-driven

# Build an image and deploy a unit by hand (example: the template export job):
gcloud builds submit clients/client_template/job --tag asia-southeast1-docker.pkg.dev/agora-data-driven/agora/template-export:manual
gcloud run jobs deploy template-export --image …:manual --region asia-southeast1 \
  --service-account template-dash-job@agora-data-driven.iam.gserviceaccount.com
```

The two-login requirement (CLI creds *and* ADC) and `FORCE_REBUILD=1` for code/view/seed changes
apply identically on every platform.

## Where to read more

- [`CLAUDE.md`](CLAUDE.md) — fixed facts, the data contract, deploy procedure, the binding freshness
  contract, and the guardrails.
- [`docs/dev-workflow.md`](docs/dev-workflow.md) — branch → PR → CI → merge for the team.
- [`tools/README.md`](tools/README.md) — every operator script and when to run it.
- [`clients/client_template/README.md`](clients/client_template/README.md) — the three-stage contract
  end to end.
- [`services/ingest/README.md`](services/ingest/README.md) — the shared raw layer.
- [`services/portal/README.md`](services/portal/README.md) — the portal/CRM front-door.
