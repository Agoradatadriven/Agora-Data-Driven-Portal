# =============================================================================
# scheduler.ps1 -- create / refresh the */10 self-gating Cloud Scheduler trigger
#                  for the tcs client export job.
#
# Creates (or updates, idempotently) the Cloud Scheduler HTTP job
# `tcs-export-daily`, which POSTs the Run Admin API :run URI of the
# `tcs-export` Cloud Run job every 10 minutes. The export job is
# SELF-GATING: it runs on this */10 tick but only rebuilds when the shared
# raw_windsor mirror tables it reads advanced past its _freshness.json watermark
# in the client bucket. So a frequent tick is cheap -- most ticks no-op.
#
# RUN AS YOURSELF. Idempotent: re-running converges to the desired state
# (describe is the create-vs-update probe).
# =============================================================================

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$CLIENT  = "tcs"

# Derived names (DERIVE from the client key; never re-type).
$JOB   = "$CLIENT-export"         # Cloud Run export job
$SCHED = "$CLIENT-export-daily"   # Cloud Scheduler trigger
$CRON  = "*/10 * * * *"           # export tick; the job self-gates on the watermark

# NOTE: This script stays on the default $ErrorActionPreference = "Continue".
# gcloud writes ordinary progress to stderr; under "Stop" PowerShell wraps that
# stderr as a terminating NativeCommandError and aborts mid-script EVEN ON
# SUCCESS. We therefore gate on $LASTEXITCODE explicitly via Must instead.

# --- Helpers (Die / Must) ----------------------------------------------------
function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) {
    # Call AFTER a native command; fails the script if that command returned non-zero.
    if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" }
}

# =============================================================================
# Step 1 -- Resolve the project number at runtime (NEVER hardcode) and build the
#           Cloud Scheduler service-agent SA.
# =============================================================================
Write-Host "[..] Resolving project number" -ForegroundColor Cyan
$PNUM = (gcloud projects describe $PROJECT --format='value(projectNumber)'); Must "resolve project number"
$PNUM = ($PNUM | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($PNUM)) { Die "project number came back empty" }
# The Cloud Scheduler service agent is the identity Scheduler uses to mint an
# OAuth token and invoke the Run job. It must already hold run.invoker on the job
# (deploy_tcs.ps1 grants that). We resolve it from the project number rather
# than hardcoding it, because the number differs per project.
$SCHED_AGENT = "service-$PNUM@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
Write-Host "[OK] project number = $PNUM ; scheduler agent = $SCHED_AGENT"

# =============================================================================
# Step 2 -- Create-or-update the */10 Cloud Scheduler HTTP job.
#           It POSTs the Run Admin API :run URI and authenticates as the
#           scheduler agent SA via OAuth. The Run jobs:run endpoint expects an
#           OAuth token (it is a Google API), NOT an OIDC token.
# =============================================================================
$RUN_URI = "https://$REGION-run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/${JOB}:run"

# describe is the create-vs-update probe (idempotent).
gcloud scheduler jobs describe $SCHED --location $REGION --project $PROJECT *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[..] Updating scheduler job $SCHED ($CRON)" -ForegroundColor Cyan
    gcloud scheduler jobs update http $SCHED `
        --location $REGION `
        --project $PROJECT `
        --schedule "$CRON" `
        --time-zone "Asia/Singapore" `
        --uri $RUN_URI `
        --http-method POST `
        --oauth-service-account-email $SCHED_AGENT
    Must "update scheduler job $SCHED"
} else {
    Write-Host "[..] Creating scheduler job $SCHED ($CRON)" -ForegroundColor Cyan
    gcloud scheduler jobs create http $SCHED `
        --location $REGION `
        --project $PROJECT `
        --schedule "$CRON" `
        --time-zone "Asia/Singapore" `
        --uri $RUN_URI `
        --http-method POST `
        --oauth-service-account-email $SCHED_AGENT
    Must "create scheduler job $SCHED"
}

Write-Host "[OK] scheduled $SCHED -> $JOB (every 10 min; job self-gates on _freshness.json)" -ForegroundColor Green
