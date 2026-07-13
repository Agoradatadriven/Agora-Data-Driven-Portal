# deploy_views_tcs.ps1 — re-apply this client's BigQuery views, then force
# a fresh export so the change actually reaches the served JSON.
#
# Two steps, in this order:
#   1. Re-apply the views via create_views.py (repo .venv Python).
#   2. Re-run the export job `tcs-export` with FORCE_REBUILD=1.
#
# Why FORCE_REBUILD=1 is mandatory here: a view-only change does NOT advance the
# upstream `raw_windsor` watermark. The export job self-gates on that watermark,
# so without FORCE_REBUILD=1 the freshness gate sees nothing new, no-ops, and the
# bucket keeps serving the STALE `tcs.json` built before the view changed.
# FORCE_REBUILD=1 bypasses the gate and rebuilds against the new view definitions.
#
# This script stays on the default $ErrorActionPreference ("Continue"): gcloud
# writes ordinary progress to stderr, and under "Stop" PowerShell would wrap that
# stderr as a terminating NativeCommandError and abort mid-script even on success.
# We gate on $LASTEXITCODE explicitly instead.

# --- constants ---------------------------------------------------------------
$PROJECT  = "agora-data-driven"
$REGION   = "asia-southeast1"
$JOB      = "tcs-export"

# --- helpers -----------------------------------------------------------------
function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) {
    # Call AFTER a native command; fails the script if that command returned non-zero.
    if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" }
}

# --- paths (resolved from this script's location) ----------------------------
# sql/  ->  client_tcs/  is the parent of $PSScriptRoot.
$ClientDir   = Split-Path -Parent $PSScriptRoot
$CreateViews = Join-Path $ClientDir "create_views.py"
# Repo root is clients/client_tcs/ -> clients/ -> <repo root>.
$RepoRoot    = Split-Path -Parent (Split-Path -Parent $ClientDir)
$Python      = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python))      { Die "repo .venv python not found at $Python" }
if (-not (Test-Path $CreateViews)) { Die "create_views.py not found at $CreateViews" }

# --- step 1: re-apply the views ----------------------------------------------
Write-Host "[..] applying views via create_views.py"
& $Python $CreateViews; Must "apply views"
Write-Host "[OK] views applied"

# --- step 2: force a fresh export --------------------------------------------
# FORCE_REBUILD=1 bypasses the watermark gate (a view-only change does not
# advance the upstream watermark; see header).
Write-Host "[..] running export job $JOB with FORCE_REBUILD=1"
gcloud run jobs execute $JOB `
    --project $PROJECT `
    --region $REGION `
    --update-env-vars FORCE_REBUILD=1 `
    --wait
Must "execute export job $JOB"
Write-Host "[OK] export job $JOB completed (FORCE_REBUILD=1)"
