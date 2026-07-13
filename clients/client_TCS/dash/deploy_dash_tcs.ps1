# =============================================================================
# deploy_dash_tcs.ps1 -- build + deploy the `tcs` client DASH web service
#                             (Cloud Run service `tcs-dash`, Stage 3).
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop. We use `gcloud builds
# submit --tag` ONLY to build the image (no actAs needed for that), then deploy the
# Cloud Run service from this laptop AS YOU (you do have actAs on the runtime SA).
# A cloudbuild-driven deploy would fail: the Cloud Build SA cannot
# iam.serviceAccounts.actAs the runtime SA. The cloudbuild.yaml here is for a FUTURE
# push-to-main trigger only and is unused from this script.
#
# Idempotent: `gcloud run deploy` is create-or-update.
#
# NOTE on SSO: this script does NOT set SSO_SECRET / CLIENT_KEY. Those are wired
# separately (additively) by tools\enable_platform_sso.ps1 AFTER this service is
# deployed on its tcs.agoradatadriven.com custom domain. The dashboard's own
# password gate set here always works on its own regardless.
#
# USAGE
#   .\deploy_dash_tcs.ps1               # validate JS, build, deploy
#   .\deploy_dash_tcs.ps1 -SkipBuild    # reuse current image, redeploy only
# =============================================================================

param([switch]$SkipBuild)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT     = "agora-data-driven"
$REGION      = "asia-southeast1"
$REPO        = "agora"
$SERVICE     = "tcs-dash"
$SA          = "tcs-dash-web@agora-data-driven.iam.gserviceaccount.com"

# Private data bucket + object this dashboard proxies (DERIVED from the client key).
$GCS_BUCKET  = "agora-data-driven-tcs-dash"
$DATA_OBJECT = "tcs.json"

# Secrets mounted as env vars (Secret Manager, :latest).
$SESSION_SECRET = "tcs-dash-session-key"
$PASSWORD_SECRET = "tcs-dash-password"

# This script stays on the default $ErrorActionPreference = "Continue": gcloud writes
# ordinary progress to stderr, and under "Stop" PowerShell wraps that stderr as a
# terminating NativeCommandError and aborts mid-script EVEN ON SUCCESS. We gate on
# $LASTEXITCODE explicitly via Must.

# --- Helpers (Die / Must) ----------------------------------------------------
function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) {
    # Call AFTER a native command; fails the script if that command returned non-zero.
    if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" }
}

# Resolve paths relative to THIS script so it works from any working directory.
$DASH_DIR  = $PSScriptRoot
$REPO_ROOT = (Resolve-Path (Join-Path $DASH_DIR "..\..\..")).Path
$VENV_PY   = Join-Path $REPO_ROOT ".venv\Scripts\python.exe"
$VALIDATOR = Join-Path $REPO_ROOT "tools\_validate_dash_js.py"
$DASH_HTML = Join-Path $DASH_DIR "dashboard.html"

# =============================================================================
# Step 0 -- Pre-deploy JS gate. A JS syntax error in dashboard.html does not throw
#           a visible error -- the page just stays stuck forever on "Loading
#           dashboard...", because the inline script that fetches /data.json and
#           swaps the DOM never runs. Catch it here, before we ship a dead page.
# =============================================================================
Write-Host "[..] Validating dashboard.html inline JS (esprima gate)" -ForegroundColor Cyan
if (-not (Test-Path $VENV_PY))   { Die "repo .venv python not found at $VENV_PY (create the dev .venv first)" }
if (-not (Test-Path $VALIDATOR)) { Die "JS validator not found at $VALIDATOR" }
& $VENV_PY $VALIDATOR $DASH_HTML
Must "dashboard.html failed the JS syntax gate -- fix the inline <script> before deploying"
Write-Host "[OK] dashboard.html JS parsed"

# =============================================================================
# Step 1 -- Resolve a short git SHA for the image tag.
# =============================================================================
Write-Host "[..] Resolving image tag" -ForegroundColor Cyan
$SHA = (git rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) {
    # Not a git repo (or no commits yet): fall back to a timestamped manual tag so
    # the image is still uniquely identifiable.
    $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss")
    Write-Host "    not a git repo; using fallback tag $SHA" -ForegroundColor Yellow
}
$SHA = $SHA.Trim()
$AR_HOST = "$REGION-docker.pkg.dev"
$IMG = "$AR_HOST/$PROJECT/$REPO/${SERVICE}:$SHA"
Write-Host "[OK] image = $IMG"

# =============================================================================
# Step 2 -- Build the image (build ONLY -- no actAs needed; we deploy ourselves).
# =============================================================================
if (-not $SkipBuild) {
    Write-Host "[..] Building image $IMG" -ForegroundColor Cyan
    gcloud builds submit $DASH_DIR --tag $IMG --project $PROJECT
    Must "build image for $SERVICE"
    Write-Host "[OK] built $IMG"
} else {
    Write-Host "[..] -SkipBuild: deploying existing image $IMG" -ForegroundColor Yellow
}

# =============================================================================
# Step 3 -- Deploy the Cloud Run service AS YOURSELF with the runtime SA.
#
#   Org policy: Domain Restricted Sharing rejects --allow-unauthenticated. Deploy
#   with --no-invoker-iam-check instead; the Flask app does its OWN password/SSO
#   auth in-process, and the private data object is only ever proxied to an
#   authenticated session at /data.json.
# =============================================================================
Write-Host "[..] Deploying Cloud Run service $SERVICE" -ForegroundColor Cyan
gcloud run deploy $SERVICE `
    --image $IMG `
    --region $REGION `
    --project $PROJECT `
    --service-account $SA `
    --no-invoker-iam-check `
    --update-env-vars "GCS_BUCKET=$GCS_BUCKET,DATA_OBJECT=$DATA_OBJECT" `
    --update-secrets "SESSION_SECRET=${SESSION_SECRET}:latest,DASH_PASSWORD=${PASSWORD_SECRET}:latest"
Must "deploy Cloud Run service $SERVICE"
Write-Host "[OK] deployed $SERVICE (tag $SHA)" -ForegroundColor Green
Write-Host "     Next: map tcs.agoradatadriven.com, then run tools\enable_platform_sso.ps1 -Keys tcs"
