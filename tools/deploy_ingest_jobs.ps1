# =============================================================================
# deploy_ingest_jobs.ps1 -- build / deploy / schedule the SHARED Windsor ingest
#                           Cloud Run jobs (the writers of the raw_windsor dataset).
#
# This is the ONE script in the repo that touches PRODUCTION ingest. It is
# idempotent: re-running it converges to the desired state (deploy = create-or-
# update, IAM bindings are add-if-missing, scheduler jobs are create-or-update).
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop.
#   A laptop build must NOT use `gcloud builds submit` to ALSO deploy, because the
#   Cloud Build service account cannot iam.serviceAccounts.actAs the runtime SA
#   (ingest-runner@). Cloud Build is org-blocked from acting as our runtime SAs, so
#   a cloudbuild-driven deploy fails on iam.serviceAccounts.actAs. We use
#   `gcloud builds submit --tag` ONLY to build the image (no actAs needed for that),
#   then deploy the Cloud Run job from this laptop AS YOU (you do have actAs on the
#   runtime SA). The per-unit cloudbuild.yaml files exist only for a FUTURE
#   push-to-main trigger and are unused from here.
#
# ALL WINDSOR CONNECTORS ARE DAILY PULLS, staggered just before the client export
# window. There is NO */10 self-gating ingest job: now that the only source is
# Windsor (a scheduled REST API), the ingest jobs are plain daily writers of
# raw_windsor. The self-gating moved DOWNSTREAM to the consumers -- the client
# EXPORT jobs (*/10) and the status dashboard (*/15) probe whether raw_windsor
# advanced past their _freshness.json watermark before rebuilding.
#
# The $JOBS array below is the SINGLE SOURCE OF TRUTH for which connectors exist;
# the services/ingest/<x> directories must match it exactly. Connector
# rows whose loader is not built yet stay COMMENTED-OUT here (rather than deleted)
# so the array remains the canonical list.
#
#   $JOBS mapping table (key / dir / job / mem / cpu / cron):
#     windsor-ga4         services/ingest/ga4         windsor-ga4-ingest         1Gi   1  10 1 * * *
#     windsor-google-ads  services/ingest/google_ads  windsor-google-ads-ingest  1Gi   1  15 1 * * *
#     windsor-meta        services/ingest/meta        windsor-meta-ingest        1Gi   1  20 1 * * *
#     (commented-out, not built yet:)
#     windsor-tradedesk   services/ingest/tradedesk   windsor-tradedesk-ingest   1Gi   1  25 1 * * *
#     windsor-reddit      services/ingest/reddit      windsor-reddit-ingest      512Mi 1  30 1 * * *
#     windsor-hubspot     services/ingest/hubspot     windsor-hubspot-ingest     512Mi 1  35 1 * * *
#     windsor-fields      services/ingest/fields      windsor-fields-ingest      512Mi 1  40 1 * * *
#
# USAGE
#   .\tools\deploy_ingest_jobs.ps1                 # build+deploy+schedule all jobs
#   .\tools\deploy_ingest_jobs.ps1 -Only windsor-meta
#   .\tools\deploy_ingest_jobs.ps1 -SkipBuild      # reuse current image, redeploy+reschedule
#   .\tools\deploy_ingest_jobs.ps1 -Run            # also execute each job once after deploy
# =============================================================================

param(
    [string]$Only = "",
    [switch]$SkipBuild,
    [switch]$Run
)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$REPO    = "agora"
$SA      = "ingest-runner@agora-data-driven.iam.gserviceaccount.com"

# Shared resources the ingest jobs touch.
$RAW_DATASET    = "raw_windsor"
$STAGING_BUCKET = "agora-data-driven-staging"
$WINDSOR_SECRET = "windsor-api-key"

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

# --- The canonical $JOBS table (single source of truth) ----------------------
# All Windsor connectors are DAILY scheduled pulls, staggered just before the
# client export window. There is NO */10 self-gating ingest job; self-gating
# lives in the downstream consumers. UNCOMMENT each row below as its loader is
# built -- leave the rest here, commented, so the array stays canonical.
$JOBS = @(
  # key                dir                                     job                       mem     cpu  cron
  @{ key="windsor-ga4";        dir="services/ingest/ga4";        job="windsor-ga4-ingest";        mem="1Gi";   cpu="1"; cron="10 1 * * *" }
  @{ key="windsor-google-ads"; dir="services/ingest/google_ads"; job="windsor-google-ads-ingest"; mem="1Gi";   cpu="1"; cron="15 1 * * *" }
  @{ key="windsor-meta";       dir="services/ingest/meta";       job="windsor-meta-ingest";       mem="1Gi";   cpu="1"; cron="20 1 * * *" }
  # Additional Windsor connectors -- UNCOMMENT each row as its loader is built (the array is the
  # single source of truth, so leave them here, commented, rather than dropping them):
  # @{ key="windsor-tradedesk"; dir="services/ingest/tradedesk"; job="windsor-tradedesk-ingest"; mem="1Gi";   cpu="1"; cron="25 1 * * *" }
  # @{ key="windsor-reddit";    dir="services/ingest/reddit";    job="windsor-reddit-ingest";    mem="512Mi"; cpu="1"; cron="30 1 * * *" }
  # @{ key="windsor-hubspot";   dir="services/ingest/hubspot";   job="windsor-hubspot-ingest";   mem="512Mi"; cpu="1"; cron="35 1 * * *" }
  # @{ key="windsor-fields";    dir="services/ingest/fields";    job="windsor-fields-ingest";    mem="512Mi"; cpu="1"; cron="40 1 * * *" }
)

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
Write-Host "[OK] image tag = $SHA"

# =============================================================================
# Step 2 -- Resolve the project number at runtime (NEVER hardcode) and build the
#           Cloud Scheduler service-agent SA.
# =============================================================================
Write-Host "[..] Resolving project number" -ForegroundColor Cyan
$PNUM = (gcloud projects describe $PROJECT --format='value(projectNumber)'); Must "resolve project number"
$PNUM = ($PNUM | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($PNUM)) { Die "project number came back empty" }
# The Cloud Scheduler service agent is the identity Scheduler uses to mint OAuth
# tokens and invoke the Run job. It must be granted serviceAccountTokenCreator on
# the runtime SA and run.invoker on each job.
$SCHED_AGENT = "service-$PNUM@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
Write-Host "[OK] project number = $PNUM ; scheduler agent = $SCHED_AGENT"

# =============================================================================
# Step 3 -- Ensure the shared ingest-runner@ SA exists + least-privilege IAM.
#           Every binding here is idempotent (create/grant is add-if-missing).
# =============================================================================
Write-Host "[..] Ensuring ingest-runner service account + IAM" -ForegroundColor Cyan

# 3a. The runtime SA itself (create only if absent; describe is the idempotency probe).
gcloud iam service-accounts describe $SA --project $PROJECT *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    creating $SA" -ForegroundColor Yellow
    gcloud iam service-accounts create "ingest-runner" `
        --project $PROJECT `
        --display-name "Windsor ingest runner (raw_windsor writer)"
    Must "create ingest-runner service account"
} else {
    Write-Host "    $SA already exists"
}

# 3b. secretAccessor on the shared Windsor API-key secret (read the ingest key only).
gcloud secrets add-iam-policy-binding $WINDSOR_SECRET `
    --project $PROJECT `
    --member "serviceAccount:$SA" `
    --role "roles/secretmanager.secretAccessor"
Must "grant secretAccessor on $WINDSOR_SECRET"

# 3c. Project-level BigQuery roles: dataEditor (write raw_windsor.* tables) +
#     jobUser (run load/query jobs).
gcloud projects add-iam-policy-binding $PROJECT `
    --member "serviceAccount:$SA" `
    --role "roles/bigquery.dataEditor" `
    --condition=None
Must "grant bigquery.dataEditor"

gcloud projects add-iam-policy-binding $PROJECT `
    --member "serviceAccount:$SA" `
    --role "roles/bigquery.jobUser" `
    --condition=None
Must "grant bigquery.jobUser"

# 3d. storage.objectAdmin on the shared staging bucket (Windsor loaders stage NDJSON
#     there before the BigQuery load job).
gcloud storage buckets add-iam-policy-binding "gs://$STAGING_BUCKET" `
    --member "serviceAccount:$SA" `
    --role "roles/storage.objectAdmin"
Must "grant storage.objectAdmin on $STAGING_BUCKET"

# 3e. serviceAccountTokenCreator for the scheduler agent SA ON the runtime SA, so
#     Cloud Scheduler can mint an OAuth token AS ingest-runner@ to invoke the job.
gcloud iam service-accounts add-iam-policy-binding $SA `
    --project $PROJECT `
    --member "serviceAccount:$SCHED_AGENT" `
    --role "roles/iam.serviceAccountTokenCreator"
Must "grant serviceAccountTokenCreator to scheduler agent on $SA"

Write-Host "[OK] ingest-runner SA + IAM in place"

# =============================================================================
# Step 4 -- Iterate $JOBS: build (unless -SkipBuild), deploy the Run job, grant
#           the scheduler agent run.invoker, and create-or-update the daily
#           Cloud Scheduler HTTP job that POSTs the job's :run URI.
# =============================================================================
$AR_HOST = "$REGION-docker.pkg.dev"

# Anchor build-context paths to the repo root (parent of tools/) so this script works
# regardless of the caller's current working directory.
$RepoRoot = Split-Path -Parent $PSScriptRoot

foreach ($J in $JOBS) {
    $key = $J.key
    $dir = Join-Path $RepoRoot $J.dir
    $job = $J.job
    $mem = $J.mem
    $cpu = $J.cpu
    $cron = $J.cron

    # Honor -Only: filter by the connector key.
    if ($Only -ne "" -and $Only -ne $key) { continue }

    Write-Host ""
    Write-Host "=== $key -> $job ===" -ForegroundColor Cyan

    $img = "$AR_HOST/$PROJECT/$REPO/${job}:$SHA"

    # 4a. Build the image (build ONLY -- no actAs needed; we deploy ourselves below).
    if (-not $SkipBuild) {
        if (-not (Test-Path $dir)) { Die "build dir not found for $key : $dir" }
        Write-Host "[..] Building image $img" -ForegroundColor Cyan
        gcloud builds submit $dir --tag $img --project $PROJECT
        Must "build image for $key"
        Write-Host "[OK] built $img"
    } else {
        Write-Host "[..] -SkipBuild: deploying existing image $img" -ForegroundColor Yellow
    }

    # 4b. Deploy the Cloud Run job AS YOURSELF with the runtime SA. deploy is
    #     create-or-update, so this is idempotent. The Windsor API key is mounted
    #     from Secret Manager; the loader resolves the project number / dataset at
    #     runtime, so we only pass what the loader needs as env.
    Write-Host "[..] Deploying Cloud Run job $job" -ForegroundColor Cyan
    gcloud run jobs deploy $job `
        --image $img `
        --region $REGION `
        --project $PROJECT `
        --service-account $SA `
        --memory $mem `
        --cpu $cpu `
        --max-retries 1 `
        --task-timeout 3600 `
        --set-env-vars "GCP_PROJECT=$PROJECT,RAW_DATASET=$RAW_DATASET,STAGING_BUCKET=$STAGING_BUCKET" `
        --set-secrets "WINDSOR_API_KEY=${WINDSOR_SECRET}:latest"
    Must "deploy Cloud Run job $job"
    Write-Host "[OK] deployed $job"

    # 4c. Grant the scheduler agent run.invoker on THIS job, so the daily Scheduler
    #     HTTP target is authorized to call its :run endpoint. Idempotent.
    Write-Host "[..] Granting run.invoker to scheduler agent on $job" -ForegroundColor Cyan
    gcloud run jobs add-iam-policy-binding $job `
        --region $REGION `
        --project $PROJECT `
        --member "serviceAccount:$SCHED_AGENT" `
        --role "roles/run.invoker"
    Must "grant run.invoker on $job"

    # 4d. Create-or-update the daily Cloud Scheduler HTTP job. It POSTs the Run
    #     Admin API :run URI and authenticates as the scheduler agent SA (OAuth).
    #     The Run jobs:run endpoint expects an OAuth token (a Google API), not OIDC.
    $sched   = "$job-daily"
    $run_uri = "https://$REGION-run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/${job}:run"

    # describe is the create-vs-update probe (idempotent).
    gcloud scheduler jobs describe $sched --location $REGION --project $PROJECT *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[..] Updating scheduler job $sched ($cron)" -ForegroundColor Cyan
        gcloud scheduler jobs update http $sched `
            --location $REGION `
            --project $PROJECT `
            --schedule "$cron" `
            --time-zone "Asia/Singapore" `
            --uri $run_uri `
            --http-method POST `
            --oauth-service-account-email $SCHED_AGENT
        Must "update scheduler job $sched"
    } else {
        Write-Host "[..] Creating scheduler job $sched ($cron)" -ForegroundColor Cyan
        gcloud scheduler jobs create http $sched `
            --location $REGION `
            --project $PROJECT `
            --schedule "$cron" `
            --time-zone "Asia/Singapore" `
            --uri $run_uri `
            --http-method POST `
            --oauth-service-account-email $SCHED_AGENT
        Must "create scheduler job $sched"
    }
    Write-Host "[OK] scheduled $sched"

    # 4e. -Run: execute the job once now (smoke run / first backfill tick).
    if ($Run) {
        Write-Host "[..] Executing $job once" -ForegroundColor Cyan
        gcloud run jobs execute $job --region $REGION --project $PROJECT
        Must "execute job $job"
        Write-Host "[OK] executed $job"
    }
}

Write-Host ""
Write-Host "[OK] ingest jobs deploy complete (tag $SHA)" -ForegroundColor Green
