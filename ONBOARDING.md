# Onboarding — Agora Data Driven

Welcome! This guide takes you from a blank Windows laptop to "I just shipped a dashboard change."
You drive almost everything through **Claude Code** (an AI pair-programmer in your terminal) — you
describe what you want in plain English, Claude makes the edits and runs the deploys.

You do not need to be a developer. You do need to be able to copy-paste a command and read what
comes back.

---

## 1. One-time machine setup (do this once)

1. **Install Git** — download from <https://git-scm.com/download/win>, accept the defaults.
2. **Clone this repo** — open *PowerShell* and run (pick any folder you like):
   ```powershell
   cd $HOME\Desktop
   git clone <the repo URL your team gives you> "Agora Data Driven"
   cd "Agora Data Driven"
   ```
3. **Run the setup script** — double-click `scripts\setup.cmd` (or run
   `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`). It installs Python 3.12 and the
   Google Cloud SDK if missing, builds the project `.venv`, and then asks you to **log in to Google
   Cloud twice**:
   - `gcloud auth login` — the **CLI** credentials.
   - `gcloud auth application-default login` — the **Application Default Credentials (ADC)** used by
     the Python tools.

   Log in as **`info@agoradatadriven.com`** both times. (If a winget install can't find `python` or
   `gcloud` afterwards, open a **new** terminal and re-run setup — the PATH just needs refreshing.)
4. **Install Claude Code** — follow your team's link, then run `claude` once and sign in.

That's it. You won't repeat step 1.

---

## 2. Every working session (≈30 seconds)

```powershell
cd "$HOME\Desktop\Agora Data Driven"
powershell -ExecutionPolicy Bypass -File scripts\start_day.ps1   # or double-click scripts\start_day.cmd
claude
```

`start_day.ps1` is a fast preflight. Google enforces periodic re-login, and gcloud keeps **two
independent logins** (CLI creds and ADC) that can expire separately — the script checks both and
re-prompts only the one that lapsed, then confirms it can read a secret and reach BigQuery. When it
prints its green checks, you're ready. Then `claude` starts your AI session.

---

## 3. Making edits with Claude

Two kinds of change cover almost everything:

- **"Change what the dashboard shows / how it looks."** That's the one big self-contained
  `dash/dashboard.html`. Tell Claude e.g. *"on the template dashboard, rename the 'Conversions' card
  to 'Leads' and make the accent color teal."*
- **"Add or change a metric."** Metrics flow through a **three-stage contract, matched by name**:
  the SQL view column → the export job's `data` dict key → the `dashboard.html` `data.*` key. Adding
  a metric is usually one edit in each stage. Just describe the metric — *"add a 'cost per
  conversion' metric to the template dashboard"* — and Claude makes all three edits consistently.

When you're happy, **just say "deploy it."** Claude runs the right per-stage script
(`deploy_views_template.ps1`, `deploy_job_template.ps1`, or `deploy_dash_template.ps1`) and reports
back. For code/view changes it passes `FORCE_REBUILD=1` for you, so the freshness gate doesn't keep
serving the old data.

---

## 4. Doing GCP work through Claude

You can ask Claude to stand up a new client, deploy the portal, refresh the Windsor ingest jobs, or
rotate a password — it uses the scripts in `scripts/` and the per-client deploy scripts. Examples:

- *"Stand up a new client called `acme`."* (Claude copies `client_template`, derives every name from
  the key, and runs the standup.)
- *"Deploy the Windsor ingest jobs."* → `scripts\deploy_ingest_jobs.ps1`
- *"Turn on the portal super-admin console."* → `scripts\enable_super_admin.ps1`

Deploys are **manual and run as you** — Claude builds the image and deploys it, never via Cloud
Build from your laptop.

---

## 5. The guardrails (Claude follows these; you should know them)

- **Never commit secrets.** Keys and credentials are gitignored; secret values are written to Secret
  Manager, never to the repo.
- **The data JSON is never public.** Dashboards are private buckets behind a login.
- **Views are code.** They live in `sql/*.sql` and are reapplied with `create_views.py` — never
  hand-edited in the BigQuery console.
- **One region, one project.** Everything is `agora-data-driven` in `asia-southeast1`.
- **No `--allow-unauthenticated`.** Org policy forbids it; the apps do their own auth.

---

## 6. Smoke test (prove your setup works)

Ask Claude: *"Run the start-of-day preflight and then read me the freshness watermark for the
template client."* A healthy result: the preflight passes both credential checks, and Claude can
reach the `agora-data-driven-template-dash` bucket. If anything fails, paste the red line back to
Claude — it will tell you exactly which login to refresh.

---

## 7. Where to read more

- [`README.md`](README.md) — the architecture and folder map.
- [`CLAUDE.md`](CLAUDE.md) — the canonical facts, data contract, and deploy procedure.
- [`scripts/README.md`](scripts/README.md) — every operator script and when to run it.
- [`clients/client_template/README.md`](clients/client_template/README.md) — the data contract end
  to end.
