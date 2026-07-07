# =============================================================================
# deploy_intel_refresh.ps1 -- build/deploy/schedule the DAILY Market Intelligence
#   auto-refresh Cloud Run JOB `intel-refresh`.
#
# This job pulls REAL news (Google News RSS + publisher feeds, via intel_feed.py)
# into every client's Atrium intel tab once a day. It is ADDITIVE and infra-light:
#   * REUSES the platform-dash image (same Dockerfile/dir as deploy_dash_platform.ps1)
#     -- it just runs `python intel_refresh.py` instead of gunicorn.
#   * RUNS AS the existing platform-dash-web SA, which already has objectAdmin on the
#     registry bucket (it writes the SAME workspace/<c>.json objects the app does).
#   * NO new bucket / secret / service. The ONE new piece is a Cloud Scheduler job +
#     the scheduler-agent IAM to invoke it (mirrors tools/deploy_ingest_jobs.ps1).
#
# The feature is GATED: the job is a logged no-op unless INTEL_AUTO_ENABLED=1, which
# this script sets on the job. To turn the feature OFF, redeploy with -Disable (or
# just delete the scheduler job: `gcloud scheduler jobs delete intel-refresh-daily`).
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop (the Cloud Build SA cannot
# actAs the runtime SA). `gcloud builds submit --tag` only BUILDS the image; we deploy
# the job from this laptop AS YOU.
#
# USAGE
#   .\deploy_intel_refresh.ps1            # build, deploy, schedule (daily 07:00 SGT)
#   .\deploy_intel_refresh.ps1 -SkipBuild # reuse current image, redeploy + reschedule
#   .\deploy_intel_refresh.ps1 -Run       # also execute the job once now
#   .\deploy_intel_refresh.ps1 -Disable   # deploy with the feature OFF (INTEL_AUTO_ENABLED=0)
# =============================================================================

param([switch]$SkipBuild, [switch]$Run, [switch]$Disable)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"   # Singapore. One region, never another.
$REPO    = "agora"             # shared Artifact Registry docker repo
$PLATFORM = "platform-dash"    # we reuse this service's image for the job
$JOB     = "intel-refresh"
$WEB_SA  = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
$BUCKET  = "agora-data-driven-platform-dash"   # PRIVATE registry bucket (workspaces live here)
$CRON    = "0 7 * * *"         # 07:00 Asia/Singapore -- fresh news ready before clients log in

$ENABLED = if ($Disable) { "0" } else { "1" }

# Stay on the default $ErrorActionPreference = "Continue" (gcloud writes progress to stderr, which
# "Stop" would treat as fatal even on success). Gate on $LASTEXITCODE via Must.
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
# Step 2 -- Resolve the project number (NEVER hardcode) + the scheduler agent SA.
# =============================================================================
Write-Host "[..] Resolving project number" -ForegroundColor Cyan
$PNUM = (gcloud projects describe $PROJECT --format='value(projectNumber)'); Must "resolve project number"
$PNUM = ($PNUM | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($PNUM)) { Die "project number came back empty" }
$SCHED_AGENT = "service-$PNUM@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
Write-Host "[OK] project number = $PNUM ; scheduler agent = $SCHED_AGENT"

# =============================================================================
# Step 2.5 -- The AI 'brain'. The refresh job picks a per-client model (intel_ai.py)
#             and curates real news with it. There is NO news-feed fallback: a client
#             with no model (or a failing model) is simply left as-is. Two providers:
#               * Gemini via VERTEX AI -- GCP-billed (no key). Enable the API + grant the
#                 runtime SA aiplatform.user, then set VERTEX_GEMINI_ENABLED=1.
#               * DeepSeek via its API-key secret (mounted if present).
# =============================================================================
Write-Host "[..] Enabling Vertex AI + granting $WEB_SA aiplatform.user" -ForegroundColor Cyan
gcloud services enable aiplatform.googleapis.com --project=$PROJECT *> $null
gcloud projects add-iam-policy-binding $PROJECT `
    --member "serviceAccount:$WEB_SA" --role "roles/aiplatform.user" *> $null
Write-Host "[OK] Vertex Gemini available (project $PROJECT, location $REGION)"

$secretPairs = @()
gcloud secrets describe "DEEPSEEK_API_KEY" --project $PROJECT *> $null
if ($LASTEXITCODE -eq 0) {
    gcloud secrets add-iam-policy-binding "DEEPSEEK_API_KEY" `
        --project $PROJECT `
        --member "serviceAccount:$WEB_SA" `
        --role "roles/secretmanager.secretAccessor" *> $null
    Must "grant secretAccessor on DEEPSEEK_API_KEY"
    $secretPairs += "DEEPSEEK_API_KEY=DEEPSEEK_API_KEY:latest"
    Write-Host "[OK] will mount DEEPSEEK_API_KEY"
} else {
    Write-Host "[..] DEEPSEEK_API_KEY not found -- DeepSeek models unavailable (Gemini still works)" -ForegroundColor Yellow
}

# =============================================================================
# Step 3 -- Deploy the Cloud Run JOB AS YOURSELF, overriding the image entrypoint to
#           run the refresh script. INTEL_AUTO_ENABLED gates the feature; the web SA
#           already has objectAdmin on the registry bucket, so no new IAM is needed
#           for the job to read clients + write workspaces.
# =============================================================================
Write-Host "[..] Deploying Cloud Run job $JOB (INTEL_AUTO_ENABLED=$ENABLED)" -ForegroundColor Cyan
$deployArgs = @(
    "run", "jobs", "deploy", $JOB,
    "--image", $IMG,
    "--region", $REGION,
    "--project", $PROJECT,
    "--service-account", $WEB_SA,
    "--command", "python",
    "--args", "intel_refresh.py",
    "--memory", "512Mi",
    "--cpu", "1",
    "--max-retries", "1",
    "--task-timeout", "900",
    "--set-env-vars", "REGISTRY_BUCKET=$BUCKET,REGISTRY_OBJECT=platform.json,WORKSPACE_BUCKET=$BUCKET,INTEL_AUTO_ENABLED=$ENABLED,VERTEX_GEMINI_ENABLED=1,VERTEX_PROJECT=$PROJECT,VERTEX_LOCATION=$REGION"
)
if ($secretPairs.Count -gt 0) {
    $deployArgs += @("--set-secrets", ($secretPairs -join ","))
}
gcloud @deployArgs
Must "deploy Cloud Run job $JOB"
Write-Host "[OK] deployed $JOB"

# =============================================================================
# Step 4 -- Scheduler IAM. The scheduler POSTs the job's :run URI authenticated AS the
#           WEB SA (NOT the cloudscheduler service agent -- creating/updating a job that
#           impersonates the service agent needs actAs ON that agent, which a normal
#           project owner lacks, so that path fails PERMISSION_DENIED). So instead:
#             * the scheduler agent may MINT a token as the web SA (tokenCreator),
#             * the web SA may RUN the job (run.invoker),
#             * the deploying user may actAs the web SA (serviceAccountUser).
#           All idempotent.
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
# Step 5 -- Create-or-update the daily Cloud Scheduler HTTP job (POSTs the Run :run URI).
# =============================================================================
$sched   = "$JOB-daily"
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
# Step 6 -- -Run: execute the job once now (smoke run / first fill).
# =============================================================================
if ($Run) {
    Write-Host "[..] Executing $JOB once" -ForegroundColor Cyan
    gcloud run jobs execute $JOB --region $REGION --project $PROJECT
    Must "execute job $JOB"
    Write-Host "[OK] executed $JOB"
}

Write-Host ""
Write-Host "[OK] intel-refresh deploy complete (tag $SHA, enabled=$ENABLED)" -ForegroundColor Green
