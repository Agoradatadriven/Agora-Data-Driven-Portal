# Deploy the Agora internal Upwork-demand dashboard (client key: agora).
#
# Contract (CLAUDE.md): image built via Cloud Build (build-only is allowed),
# deploy is MANUAL from this machine, region asia-southeast1, repo `agora`,
# --no-invoker-iam-check (org policy blocks --allow-unauthenticated).
# The service is deliberately unauthenticated in-app so it can be iframed
# anywhere (it only serves aggregated public job-post data).
#
# Steps: ensure bucket -> upload processed data -> ensure web SA + IAM ->
#        build image -> deploy Cloud Run service.
#
# Usage:  .\deploy_dash_agora.ps1            # full pipeline
#         .\deploy_dash_agora.ps1 -DataOnly  # just re-upload data (new export)

param(
    [switch]$DataOnly,
    [switch]$SkipData
)

# NOT "Stop": gcloud writes progress and probe-404s to stderr, which PS 5.1
# turns into terminating NativeCommandErrors when captured. Every step below
# checks $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"

$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$C       = "agora"                                   # client key -> everything derives
$BUCKET  = "$PROJECT-$C-dash"
$SERVICE = "$C-dash"
$SA      = "$C-dash-web@$PROJECT.iam.gserviceaccount.com"
$IMAGE   = "$REGION-docker.pkg.dev/$PROJECT/agora/$SERVICE"
$HERE    = $PSScriptRoot
$DATA    = Join-Path $HERE "data"

# deploys must run as the org account (ian@ lacks actAs on runtime SAs)
$env:CLOUDSDK_CORE_ACCOUNT = "info@agoradatadriven.com"

Write-Host "== agora-dash deploy (project=$PROJECT region=$REGION) ==" -ForegroundColor Cyan

if (-not $SkipData) {
    if (-not (Test-Path (Join-Path $DATA "jobs.sqlite"))) {
        throw "data/jobs.sqlite missing - run processing\process_upwork.py first"
    }
    # 1) bucket (idempotent, private, uniform)
    $null = gcloud storage buckets describe "gs://$BUCKET" --project $PROJECT 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "-- creating bucket gs://$BUCKET"
        gcloud storage buckets create "gs://$BUCKET" --project $PROJECT --location $REGION --uniform-bucket-level-access
        if ($LASTEXITCODE -ne 0) { throw "bucket create failed" }
    }
    # 2) upload processed data
    Write-Host "-- uploading processed data to gs://$BUCKET/upwork/"
    gcloud storage cp (Join-Path $DATA "jobs.sqlite") (Join-Path $DATA "aggregates.json") "gs://$BUCKET/upwork/" --project $PROJECT
    if ($LASTEXITCODE -ne 0) { throw "data upload failed" }
}
if ($DataOnly) {
    Write-Host "-- data uploaded; restart the service to pick it up:"
    Write-Host "   gcloud run services update $SERVICE --region $REGION --project $PROJECT --update-env-vars DATA_STAMP=$(Get-Date -Format yyyyMMddHHmmss)"
    exit 0
}

# 3) web SA (idempotent) + read access on the data bucket
$null = gcloud iam service-accounts describe $SA --project $PROJECT 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "-- creating service account $SA"
    gcloud iam service-accounts create "$C-dash-web" --project $PROJECT --display-name "$C dashboard web"
    if ($LASTEXITCODE -ne 0) { throw "SA create failed" }
}
# a freshly created SA can take ~a minute to propagate to the IAM API — retry
$ok = $false
foreach ($try in 1..6) {
    gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --project $PROJECT `
        --member "serviceAccount:$SA" --role "roles/storage.objectViewer" | Out-Null
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Write-Host "-- IAM binding not ready (attempt $try), waiting 15s..."
    Start-Sleep 15
}
if (-not $ok) { throw "bucket IAM failed" }

# 4) build (Cloud Build builds the image; it never deploys)
Write-Host "-- building $IMAGE"
gcloud builds submit $HERE --tag $IMAGE --project $PROJECT
if ($LASTEXITCODE -ne 0) { throw "build failed" }

# 5) deploy (manual, from this machine)
Write-Host "-- deploying $SERVICE"
gcloud run deploy $SERVICE --image $IMAGE --project $PROJECT --region $REGION `
    --service-account $SA --no-invoker-iam-check `
    --memory 2Gi --cpu 1 --concurrency 40 --timeout 60 `
    --min-instances 0 --max-instances 3 `
    --set-env-vars "DATA_BUCKET=$BUCKET,DATA_PREFIX=upwork"
if ($LASTEXITCODE -ne 0) { throw "deploy failed" }

$url = gcloud run services describe $SERVICE --region $REGION --project $PROJECT --format "value(status.url)"
Write-Host "== LIVE: $url ==" -ForegroundColor Green
