# CLAUDE.md — tools (operator tooling)

**Rules live in the repo-root [`/CLAUDE.md`](../CLAUDE.md)** — read it first; this file only adds
local context. If they disagree, root wins.

Laptop-run PowerShell operator scripts. All resolve paths from `$PSScriptRoot` and gate on
`$LASTEXITCODE` (they stay on `$ErrorActionPreference = "Continue"` because gcloud writes progress to
stderr, which `Stop` would treat as a fatal error even on success).

- **`push-branch.ps1` / `merge-branches.ps1`** — the team workflow (see `docs/dev-workflow.md`).
  push-branch = one branch per machine; merge-branches = the SAFE hybrid integrate (stops for a human
  on conflict/red test, never auto-deletes).
- **`deploy_ingest_jobs.ps1`** — the ONE script that touches production ingest. Run as yourself.
- **`_validate_dash_js.py`** — the shared esprima JS gate CI and every dash deploy run.
- **`setup.ps1`** (one-time laptop setup) · **`start_day.ps1`** (per-session preflight) ·
  **`enable_platform_sso.ps1`** / **`enable_super_admin.ps1`** (additive portal wiring).

Never deploy via Cloud Build from a laptop; never `--allow-unauthenticated` (see root `CLAUDE.md`).
