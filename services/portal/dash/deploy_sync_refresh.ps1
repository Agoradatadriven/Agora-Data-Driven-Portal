# =============================================================================
# deploy_sync_refresh.ps1 -- build/deploy/schedule the AUTOMATIC "sync all dashboards"
#   Cloud Run JOB `sync-refresh` (replaces the console's manual Sync button).
#
# Mirrors deploy_mail_refresh.ps1 / deploy_intel_refresh.ps1: ADDITIVE and infra-light.
#   * REUSES the platform-dash image -- it just runs `python sync_refresh.py`.
#   * RUNS AS the existing platform-dash-web SA, which ALREADY holds the run.jobs.list /
#     run.jobs.run permissions the manual button used (so no new data-plane IAM).
#   * The ONE new piece is the Cloud Scheduler job + its IAM (same shape as intel/mail).
#
# WHY: syncing every client's <c>-export job makes PAID Windsor/Meta API pulls. Triggering
# that from the browser (a button, or worse, on every refresh) is costly and rate-limited.
# A scheduled server-side job makes it automatic, bounded, and decoupled from the console.
#
# GATED: the job is a logged no-op unless SYNC_AUTO_ENABLED=1, which this script sets.
# Turn the feature OFF with -Disable (or delete the scheduler job).
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop (build-only submit is fine).
#
# USAGE
#   .\deploy_sync_refresh.ps1            # build, deploy, schedule (every 6 hours, SGT)
#   .\deploy_sync_refresh.ps1 -SkipBuild # reuse current image, redeploy + reschedule
#   .\deploy_sync_refresh.ps1 -Run       # also execute the job once now
#   .\deploy_sync_refresh.ps1 -Disable   # deploy with the feature OFF (SYNC_AUTO_ENABLED=0)
# =============================================================================

param([switch]$SkipBuild, [switch]$Run, [switch]$Disable)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT  = "agora-data-driven"
$REGION   = "asia-southeast1"   # Singapore. One region, never another.
$REPO     = "agora"             # shared Artifact Registry docker repo
$PLATFORM = "platform-dash"     # we reuse this service's image for the job
$JOB      = "sync-refresh"
$WEB_SA   = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
$BUCKET   = "agora-data-driven-platform-dash"   # PRIVATE registry bucket (holds sync_state.json)
$CRON     = "0 */6 * * *"       # every 6 hours (SGT) -- bounded, predictable API spend

$ENABLED = if ($Disable) { "0" } else { "1" }

function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) { if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" } }

$DASH_DIR = $PSScriptRoot

# =============================================================================
# Step 1 -- Image tag + build (build ONLY; we deploy ourselves below).
# =============================================================================
Write-Host "[..] Resolving image tag" -ForegroundColor Cyan
$SHA = (git -C $DASH_DIR rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) {
    $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss")
    Write-Host "    not a git repo; using fallback tag $SHA" -ForegroundColor Yellow
}
$SHA = $SHA.Trim()
$AR_HOST = "$REGION-docker.pkg.dev"
$IMG = "$AR_HOST/$PROJECT/$REPO/${PLATFORM}:$SHA"
Write-Host "[OK] image = $IMG"

if (-not $SkipBuild) {
    Write-Host "[..] Building image $IMG" -ForegroundColor Cyan
    gcloud builds submit $DASH_DIR --tag $IMG --project=$PROJECT
    Must "build image for $JOB"
    Write-Host "[OK] built $IMG"
} else {
    Write-Host "[..] -SkipBuild: deploying existing image $IMG" -ForegroundColor Yellow
}

# =============================================================================
# Step 2 -- Project number (NEVER hardcode) + the scheduler agent SA.
# =============================================================================
Write-Host "[..] Resolving project number" -ForegroundColor Cyan
$PNUM = (gcloud projects describe $PROJECT --format='value(projectNumber)'); Must "resolve project number"
$PNUM = ($PNUM | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($PNUM)) { Die "project number came back empty" }
$SCHED_AGENT = "service-$PNUM@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
Write-Host "[OK] project number = $PNUM ; scheduler agent = $SCHED_AGENT"

# =============================================================================
# Step 2.5 -- Env. sync_dash reads GOOGLE_CLOUD_PROJECT / REGION / REGISTRY_BUCKET (all have
#             sensible defaults) and SYNC_AUTO_ENABLED (the on/off gate this script owns).
# =============================================================================
$ENV_VARS = "GOOGLE_CLOUD_PROJECT=$PROJECT,REGION=$REGION,REGISTRY_BUCKET=$BUCKET,SYNC_AUTO_ENABLED=$ENABLED"

# =============================================================================
# Step 3 -- Deploy the Cloud Run JOB AS YOURSELF, overriding the entrypoint.
# =============================================================================
Write-Host "[..] Deploying Cloud Run job $JOB (SYNC_AUTO_ENABLED=$ENABLED)" -ForegroundColor Cyan
$deployArgs = @(
    "run", "jobs", "deploy", $JOB,
    "--image", $IMG,
    "--region", $REGION,
    "--project", $PROJECT,
    "--service-account", $WEB_SA,
    "--command", "python",
    "--args", "sync_refresh.py",
    "--memory", "512Mi",
    "--cpu", "1",
    "--max-retries", "1",
    "--task-timeout", "600",
    "--set-env-vars", $ENV_VARS
)
gcloud @deployArgs
Must "deploy Cloud Run job $JOB"
Write-Host "[OK] deployed $JOB"

# =============================================================================
# Step 4 -- Scheduler IAM (identical shape to intel/mail-refresh: the scheduler POSTs the
#           :run URI AS the web SA; the web SA impersonates itself, owners can't actAs the agent).
# =============================================================================
$DEPLOYER = (gcloud config get-value account 2>$null); $DEPLOYER = ($DEPLOYER | Out-String).Trim()

Write-Host "[..] Granting scheduler agent tokenCreator on $WEB_SA" -ForegroundColor Cyan
gcloud iam service-accounts add-iam-policy-binding $WEB_SA `
    --project $PROJECT `
    --member "serviceAccount:$SCHED_AGENT" `
    --role "roles/iam.serviceAccountTokenCreator"
Must "grant serviceAccountTokenCreator to scheduler agent on $WEB_SA"

Write-Host "[..] Granting run.invoker to the web SA on $JOB" -ForegroundColor Cyan
gcloud run jobs add-iam-policy-binding $JOB `
    --region $REGION `
    --project $PROJECT `
    --member "serviceAccount:$WEB_SA" `
    --role "roles/run.invoker"
Must "grant run.invoker on $JOB"

if ($DEPLOYER) {
    Write-Host "[..] Granting $DEPLOYER actAs on $WEB_SA (needed to create the scheduler job)" -ForegroundColor Cyan
    gcloud iam service-accounts add-iam-policy-binding $WEB_SA `
        --project $PROJECT `
        --member "user:$DEPLOYER" `
        --role "roles/iam.serviceAccountUser" *> $null
}

# =============================================================================
# Step 5 -- Create-or-update the 6-hourly Cloud Scheduler HTTP job.
# =============================================================================
$sched   = "$JOB-6h"
$run_uri = "https://$REGION-run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/${JOB}:run"

gcloud scheduler jobs describe $sched --location $REGION --project $PROJECT *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[..] Updating scheduler job $sched ($CRON SGT)" -ForegroundColor Cyan
    gcloud scheduler jobs update http $sched `
        --location $REGION --project $PROJECT `
        --schedule "$CRON" --time-zone "Asia/Singapore" `
        --uri $run_uri --http-method POST `
        --oauth-service-account-email $WEB_SA
    Must "update scheduler job $sched"
} else {
    Write-Host "[..] Creating scheduler job $sched ($CRON SGT)" -ForegroundColor Cyan
    gcloud scheduler jobs create http $sched `
        --location $REGION --project $PROJECT `
        --schedule "$CRON" --time-zone "Asia/Singapore" `
        --uri $run_uri --http-method POST `
        --oauth-service-account-email $WEB_SA
    Must "create scheduler job $sched"
}
Write-Host "[OK] scheduled $sched"

# =============================================================================
# Step 6 -- -Run: execute the job once now (smoke run / first sync).
# =============================================================================
if ($Run) {
    Write-Host "[..] Executing $JOB once" -ForegroundColor Cyan
    gcloud run jobs execute $JOB --region $REGION --project $PROJECT
    Must "execute job $JOB"
    Write-Host "[OK] executed $JOB"
}

Write-Host ""
Write-Host "[OK] sync-refresh deploy complete (tag $SHA, enabled=$ENABLED, cron '$CRON')" -ForegroundColor Green
