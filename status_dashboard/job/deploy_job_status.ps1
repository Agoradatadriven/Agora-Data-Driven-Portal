# =============================================================================
# deploy_job_status.ps1 -- build + deploy the status dashboard EXPORT job
#                          (the agency-wide freshness monitor) and run it once.
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop.
#   We use `gcloud builds submit --tag` ONLY to build the image (no actAs needed
#   for a build). The Cloud Build service account cannot iam.serviceAccounts.actAs
#   the runtime SA (status-dash-job@), so a cloudbuild-driven DEPLOY would fail on
#   iam.serviceAccounts.actAs. We therefore deploy the Cloud Run job from this
#   laptop AS YOU (you do have actAs on the runtime SA).
#
# NOTE: this script deploys the JOB only. The status-dash-job@ service account and
# its IAM (objectViewer on EVERY client bucket, bigquery.jobUser to probe
# raw_windsor, objectAdmin on the status bucket to write status.json) are created
# by the top-level deploy_status.ps1 standup; re-run that when a NEW client bucket
# appears so the monitor can read it.
# =============================================================================

param(
    [switch]$SkipBuild
)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"   # Singapore. One region, never another.
$REPO    = "agora"             # shared Artifact Registry docker repo
$JOB     = "status-export"     # Cloud Run job name
$SA      = "status-dash-job@agora-data-driven.iam.gserviceaccount.com"

# NOTE: This script stays on the default $ErrorActionPreference = "Continue".
# gcloud writes ordinary progress to stderr; under "Stop" PowerShell wraps that
# stderr as a terminating NativeCommandError and aborts mid-script EVEN ON
# SUCCESS. We instead gate on $LASTEXITCODE explicitly via Must.

# --- Helpers (Die / Must) ----------------------------------------------------
function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) {
    # Call AFTER a native command; fails the script if that command returned non-zero.
    if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" }
}

# Build paths from the script's own location so the script works regardless of the
# caller's current directory (the Dockerfile/main.py sit next to this script).
$HERE = $PSScriptRoot

# =============================================================================
# Step 1 -- Resolve a short git SHA for the image tag (manual fallback if no git).
# =============================================================================
Write-Host "[..] Resolving image tag" -ForegroundColor Cyan
$SHA = (git -C $HERE rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) {
    # Not a git repo (or no commits yet): fall back to a timestamped manual tag so
    # the image is still uniquely identifiable.
    $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss")
    Write-Host "    not a git repo; using fallback tag $SHA" -ForegroundColor Yellow
}
$SHA = $SHA.Trim()
Write-Host "[OK] image tag = $SHA"

$AR_HOST = "$REGION-docker.pkg.dev"
$IMG = "$AR_HOST/$PROJECT/$REPO/${JOB}:$SHA"

# =============================================================================
# Step 2 -- Build the image (build ONLY -- no actAs needed; we deploy ourselves).
# =============================================================================
if (-not $SkipBuild) {
    Write-Host "[..] Building image $IMG" -ForegroundColor Cyan
    gcloud builds submit $HERE --tag $IMG --project $PROJECT
    Must "build image for $JOB"
    Write-Host "[OK] built $IMG"
} else {
    Write-Host "[..] -SkipBuild: deploying existing image $IMG" -ForegroundColor Yellow
}

# =============================================================================
# Step 3 -- Deploy the Cloud Run job AS YOURSELF with the runtime SA. deploy is
#           create-or-update, so this is idempotent. The status job probes
#           raw_windsor, scans every client bucket, and writes status.json +
#           the watermark into the status bucket; it resolves project/bucket from
#           its own constants, so no env is required for normal runs.
# =============================================================================
Write-Host "[..] Deploying Cloud Run job $JOB" -ForegroundColor Cyan
gcloud run jobs deploy $JOB `
    --image $IMG `
    --region $REGION `
    --project $PROJECT `
    --service-account $SA `
    --max-retries 1 `
    --task-timeout 900
Must "deploy Cloud Run job $JOB"
Write-Host "[OK] deployed $JOB"

# =============================================================================
# Step 4 -- Execute the job once with FORCE_REBUILD=1.
#           A fresh deploy is a CODE change, not an upstream-data change: it does
#           NOT advance the raw_windsor watermark, so a normal (gated) run would
#           see "upstream unchanged" and no-op, leaving stale status.json in the
#           bucket. FORCE_REBUILD=1 bypasses the freshness gate so the first run
#           after a deploy always regenerates the status JSON. Routine scheduled
#           ticks run WITHOUT this flag and self-gate on the watermark.
# =============================================================================
Write-Host "[..] Executing $JOB once with FORCE_REBUILD=1" -ForegroundColor Cyan
gcloud run jobs execute $JOB `
    --region $REGION `
    --project $PROJECT `
    --update-env-vars "FORCE_REBUILD=1" `
    --wait
Must "execute job $JOB"
Write-Host "[OK] executed $JOB (forced rebuild)"

Write-Host ""
Write-Host "[OK] status export job deploy complete (tag $SHA)" -ForegroundColor Green
