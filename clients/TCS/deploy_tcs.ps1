# =============================================================================
# deploy_tcs.ps1 -- ONE-SHOT, IDEMPOTENT standup of the `tcs` client.
#
# Stands up the full per-client stack from nothing (or converges an existing one):
#   APIs -> AR repo + private bucket + dataset -> job/web service accounts + IAM
#   -> password/session secrets -> SQL views -> export job (build/deploy/run)
#   -> */10 scheduler -> dash web service (build/deploy, private + app-level auth).
#
# Re-running is safe: every step is create-or-update / add-if-missing. To stand up
# a NEW client, copy this directory, set $CLIENT, and re-run -- all names DERIVE
# from the client key.
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop. We use `gcloud builds
# submit --tag` ONLY to build images (no actAs needed), then deploy the Cloud Run
# job/service from this laptop AS YOU (you do have actAs on the runtime SAs). A
# cloudbuild-driven deploy fails on iam.serviceAccounts.actAs because the Cloud
# Build SA is org-blocked from acting as our runtime SAs.
#
# USAGE
#   .\deploy_tcs.ps1                       # prompt for the dashboard password
#   .\deploy_tcs.ps1 -Password "s3cret"    # pass it inline (or set $env:DASH_PASSWORD)
# =============================================================================

param([string]$Password = "")

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$REPO    = "agora"
$CLIENT  = "tcs"

# Derived names (DERIVE from the client key `<c>`; never re-type) --------------
$DATASET     = "client_$CLIENT"                                            # BigQuery dataset
$BUCKET      = "agora-data-driven-$CLIENT-dash"                            # PRIVATE data bucket
$EXPORT_JOB  = "$CLIENT-export"                                            # Cloud Run job
$SCHED       = "$CLIENT-export-daily"                                      # Cloud Scheduler trigger
$WEB_SERVICE = "$CLIENT-dash"                                             # Cloud Run service
$JOB_SA      = "$CLIENT-dash-job@agora-data-driven.iam.gserviceaccount.com"
$WEB_SA      = "$CLIENT-dash-web@agora-data-driven.iam.gserviceaccount.com"
$PW_SECRET   = "$CLIENT-dash-password"
$KEY_SECRET  = "$CLIENT-dash-session-key"
$AR_HOST     = "$REGION-docker.pkg.dev"

# Paths relative to THIS script (so the standup works regardless of CWD).
$ROOT         = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path      # repo root
$VENV_PY      = Join-Path $ROOT ".venv\Scripts\python.exe"
$CREATE_VIEWS = Join-Path $PSScriptRoot "create_views.py"                  # sits next to this script
$JOB_DIR      = Join-Path $PSScriptRoot "job"
$DASH_DIR     = Join-Path $PSScriptRoot "dash"

# NOTE: This script stays on the default $ErrorActionPreference = "Continue".
# gcloud writes ordinary progress to stderr; under "Stop" PowerShell wraps that
# stderr as a terminating NativeCommandError and aborts mid-script EVEN ON
# SUCCESS. We therefore gate on $LASTEXITCODE explicitly via Must instead.

# --- Helpers -----------------------------------------------------------------
function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) {
    # Call AFTER a native command; fails the script if that command returned non-zero.
    if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" }
}
function Exists([scriptblock]$Probe) {
    # Returns $true iff the probe command exits 0. Used as the create-vs-skip probe
    # for create-if-absent resources. We swallow the probe's stderr (a "not found"
    # is the expected, non-fatal case) and report purely from the exit code.
    & $Probe *> $null
    return ($LASTEXITCODE -eq 0)
}
function Ensure-Sa([string]$email, [string]$accountId, [string]$displayName) {
    # Create a service account only if absent (describe is the idempotency probe).
    if (Exists { gcloud iam service-accounts describe $email --project $PROJECT }) {
        Write-Host "    $email already exists"
    } else {
        Write-Host "    creating $email" -ForegroundColor Yellow
        gcloud iam service-accounts create $accountId --project $PROJECT --display-name $displayName
        Must "create service account $email"
    }
}
function Write-SecretFile([string]$path, [string]$value) {
    # Secret Manager stores bytes verbatim. A UTF-8 BOM or a trailing newline would
    # become part of the secret and silently break password / HMAC comparisons.
    # Always write secret material through a temp file encoded UTF-8 WITHOUT a BOM
    # and WITHOUT a trailing newline, then --data-file= it.
    $enc = New-Object System.Text.UTF8Encoding($false)   # $false = no BOM
    [System.IO.File]::WriteAllText($path, $value, $enc)   # WriteAllText adds NO trailing newline
}

# =============================================================================
# Step 0 -- Resolve an image tag + the project number (NEVER hardcode the number).
# =============================================================================
Write-Host "[..] Resolving image tag + project number" -ForegroundColor Cyan
$SHA = (git -C $ROOT rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) {
    # Not a git repo (or no commits yet): fall back to a timestamped manual tag so
    # the image is still uniquely identifiable.
    $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss")
    Write-Host "    not a git repo; using fallback tag $SHA" -ForegroundColor Yellow
}
$SHA = $SHA.Trim()
$PNUM = (gcloud projects describe $PROJECT --format='value(projectNumber)'); Must "resolve project number"
$PNUM = ($PNUM | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($PNUM)) { Die "project number came back empty" }
# Scheduler service agent: the identity Cloud Scheduler uses to mint an OAuth token
# and invoke the export job. Resolved from the project number, never hardcoded.
$SCHED_AGENT = "service-$PNUM@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
Write-Host "[OK] image tag = $SHA ; project number = $PNUM"

# =============================================================================
# Step 1 -- Enable the required APIs (idempotent; enabling an enabled API is a no-op).
# =============================================================================
Write-Host "[..] Enabling required APIs" -ForegroundColor Cyan
gcloud services enable `
    run.googleapis.com `
    cloudbuild.googleapis.com `
    artifactregistry.googleapis.com `
    bigquery.googleapis.com `
    storage.googleapis.com `
    secretmanager.googleapis.com `
    cloudscheduler.googleapis.com `
    --project $PROJECT
Must "enable required APIs"
Write-Host "[OK] APIs enabled"

# =============================================================================
# Step 2 -- Ensure the shared AR repo, the PRIVATE data bucket, and the dataset
#           (all create-if-absent).
# =============================================================================
Write-Host "[..] Ensuring AR repo / private bucket / dataset" -ForegroundColor Cyan

# 2a. Shared Artifact Registry docker repo `agora` (shared across all clients).
if (Exists { gcloud artifacts repositories describe $REPO --location $REGION --project $PROJECT }) {
    Write-Host "    AR repo $REPO already exists"
} else {
    Write-Host "    creating AR repo $REPO" -ForegroundColor Yellow
    gcloud artifacts repositories create $REPO `
        --repository-format docker `
        --location $REGION `
        --project $PROJECT `
        --description "Agora Data Driven shared docker images"
    Must "create AR repo $REPO"
}

# 2b. The PRIVATE per-client data bucket. The data JSON lives here and is NEVER
#     public -- the dash web service proxies it only to authenticated sessions.
#     Uniform bucket-level access + public-access-prevention keep it locked down.
if (Exists { gcloud storage buckets describe "gs://$BUCKET" --project $PROJECT }) {
    Write-Host "    bucket $BUCKET already exists"
} else {
    Write-Host "    creating private bucket $BUCKET" -ForegroundColor Yellow
    gcloud storage buckets create "gs://$BUCKET" `
        --project $PROJECT `
        --location $REGION `
        --uniform-bucket-level-access `
        --public-access-prevention
    Must "create bucket $BUCKET"
}

# 2c. The per-client BigQuery dataset client_tcs (location MUST be the region;
#     bq mk is a no-op if it already exists, so tolerate that exit code).
if (Exists { bq --project_id=$PROJECT show --dataset "${PROJECT}:${DATASET}" }) {
    Write-Host "    dataset $DATASET already exists"
} else {
    Write-Host "    creating dataset $DATASET" -ForegroundColor Yellow
    bq --project_id=$PROJECT --location=$REGION mk --dataset "${PROJECT}:${DATASET}"
    Must "create dataset $DATASET"
}
Write-Host "[OK] AR repo / bucket / dataset in place"

# =============================================================================
# Step 3 -- Create the job + web service accounts with LEAST-PRIVILEGE IAM.
#           job SA: read BigQuery + run query jobs, read/write the data bucket.
#           web SA: read the data bucket ONLY (it proxies <c>.json; never writes).
#           Every binding is add-if-missing (idempotent).
# =============================================================================
Write-Host "[..] Ensuring service accounts + least-privilege IAM" -ForegroundColor Cyan

Ensure-Sa $JOB_SA "$CLIENT-dash-job" "tcs export job (BigQuery -> bucket)"
Ensure-Sa $WEB_SA "$CLIENT-dash-web" "tcs dash web (bucket reader + auth)"

# 3a. Job SA -- project roles: jobUser (run query jobs) + dataViewer (read views).
gcloud projects add-iam-policy-binding $PROJECT `
    --member "serviceAccount:$JOB_SA" `
    --role "roles/bigquery.jobUser" `
    --condition=None
Must "grant bigquery.jobUser to job SA"

gcloud projects add-iam-policy-binding $PROJECT `
    --member "serviceAccount:$JOB_SA" `
    --role "roles/bigquery.dataViewer" `
    --condition=None
Must "grant bigquery.dataViewer to job SA"

# 3b. Job SA -- bucket role: objectAdmin (writes <c>.json and the _freshness.json
#     watermark sidecar into its OWN bucket).
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" `
    --member "serviceAccount:$JOB_SA" `
    --role "roles/storage.objectAdmin"
Must "grant storage.objectAdmin to job SA on $BUCKET"

# 3c. Web SA -- bucket role: objectViewer ONLY. The web service proxies the private
#     <c>.json to authed sessions; it never writes, so read-only is sufficient.
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" `
    --member "serviceAccount:$WEB_SA" `
    --role "roles/storage.objectViewer"
Must "grant storage.objectViewer to web SA on $BUCKET"
Write-Host "[OK] service accounts + IAM in place"

# =============================================================================
# Step 4 -- Create the password + session-key secrets, and grant the web SA
#           secretAccessor on both. Secrets are written through UTF-8-no-BOM temp
#           files (see Write-SecretFile) so no stray byte corrupts them.
# =============================================================================
Write-Host "[..] Ensuring dashboard secrets" -ForegroundColor Cyan

# 4a. Resolve the dashboard password: -Password param > $env:DASH_PASSWORD > prompt.
if ([string]::IsNullOrEmpty($Password)) { $Password = $env:DASH_PASSWORD }
if ([string]::IsNullOrEmpty($Password)) {
    $sec = Read-Host "Enter the dashboard password for '$CLIENT'" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    $Password = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
if ([string]::IsNullOrEmpty($Password)) { Die "no dashboard password supplied" }

# 4b. Generate a cryptographically strong session key (32 random bytes -> base64)
#     for signing the Flask session cookie.
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32
$rng.GetBytes($bytes)
$SessionKey = [System.Convert]::ToBase64String($bytes)

# 4c. Stage both secrets to UTF-8-no-BOM temp files, create-or-add-version, then
#     scrub the temp files. add-version is idempotent: first run creates, re-runs
#     add a fresh version (Cloud Run pins :latest, so the newest wins).
$tmpPw  = Join-Path ([System.IO.Path]::GetTempPath()) ("agora-pw-"  + [System.Guid]::NewGuid().ToString("N") + ".txt")
$tmpKey = Join-Path ([System.IO.Path]::GetTempPath()) ("agora-key-" + [System.Guid]::NewGuid().ToString("N") + ".txt")
Write-SecretFile $tmpPw  $Password
Write-SecretFile $tmpKey $SessionKey
try {
    foreach ($pair in @(@{ name=$PW_SECRET; file=$tmpPw }, @{ name=$KEY_SECRET; file=$tmpKey })) {
        $name = $pair.name
        $file = $pair.file
        if (Exists { gcloud secrets describe $name --project $PROJECT }) {
            Write-Host "    adding new version to $name" -ForegroundColor Yellow
            gcloud secrets versions add $name --project $PROJECT --data-file="$file"
            Must "add version to secret $name"
        } else {
            Write-Host "    creating secret $name" -ForegroundColor Yellow
            gcloud secrets create $name --project $PROJECT --replication-policy=automatic --data-file="$file"
            Must "create secret $name"
        }
        # Grant the web SA read access on this secret (idempotent add-if-missing).
        gcloud secrets add-iam-policy-binding $name `
            --project $PROJECT `
            --member "serviceAccount:$WEB_SA" `
            --role "roles/secretmanager.secretAccessor"
        Must "grant secretAccessor on $name to web SA"
    }
} finally {
    # Always scrub the plaintext temp files, even if a gcloud step failed.
    Remove-Item $tmpPw, $tmpKey -ErrorAction SilentlyContinue
}
Write-Host "[OK] secrets created + web SA granted access"

# =============================================================================
# Step 5 -- Apply the SQL views via the repo .venv python (create_views.py reads
#           sql/*.sql in order and CREATE OR REPLACE VIEWs them into the dataset).
#           This is the create_views.py invocation referenced by README.md.
# =============================================================================
Write-Host "[..] Applying SQL views with create_views.py" -ForegroundColor Cyan
if (-not (Test-Path $VENV_PY))      { Die "repo .venv python not found at $VENV_PY (run tools\setup.ps1 first)" }
if (-not (Test-Path $CREATE_VIEWS)) { Die "create_views.py not found at $CREATE_VIEWS" }
# create_views.py takes no arguments: it resolves its sql/ directory relative to
# its own location and applies every *.sql in filename (NN_) order against the
# fixed project/location. Run it through the repo .venv python (the dev venv has
# the google-cloud-bigquery the script imports).
& $VENV_PY $CREATE_VIEWS
Must "apply SQL views for $CLIENT"
Write-Host "[OK] views applied to dataset $DATASET"

# =============================================================================
# Step 6 -- Build + deploy + run the export job. The FIRST run passes
#           FORCE_REBUILD=1 so it builds the data JSON immediately rather than
#           no-op'ing because the watermark has not yet been written.
# =============================================================================
Write-Host "[..] Building + deploying export job $EXPORT_JOB" -ForegroundColor Cyan
$jobImg = "$AR_HOST/$PROJECT/$REPO/${EXPORT_JOB}:$SHA"

# 6a. Build the image (build ONLY -- no actAs needed; we deploy ourselves below).
gcloud builds submit $JOB_DIR --tag $jobImg --project $PROJECT
Must "build export job image"

# 6b. Deploy the Cloud Run job AS YOURSELF with the job SA. deploy is
#     create-or-update (idempotent). The job resolves project/dataset/bucket from
#     its OWN constants (job/main.py), so it needs NO env for normal runs.
gcloud run jobs deploy $EXPORT_JOB `
    --image $jobImg `
    --region $REGION `
    --project $PROJECT `
    --service-account $JOB_SA `
    --max-retries 1 `
    --task-timeout 900
Must "deploy export job $EXPORT_JOB"
Write-Host "[OK] deployed $EXPORT_JOB"

# 6c. First run with FORCE_REBUILD=1 -- the watermark does not exist yet, so we
#     bypass the freshness gate to produce the initial <c>.json. Subsequent
#     scheduled ticks self-gate normally.
Write-Host "[..] Running $EXPORT_JOB once with FORCE_REBUILD=1" -ForegroundColor Cyan
gcloud run jobs execute $EXPORT_JOB `
    --region $REGION `
    --project $PROJECT `
    --update-env-vars "FORCE_REBUILD=1" `
    --wait
Must "execute export job $EXPORT_JOB"
Write-Host "[OK] initial data export complete"

# =============================================================================
# Step 7 -- Grant the scheduler agent run.invoker on the job, then create the
#           */10 Cloud Scheduler trigger tcs-export-daily (project number
#           resolved at runtime in Step 0).
# =============================================================================
Write-Host "[..] Creating the */10 scheduler $SCHED" -ForegroundColor Cyan

# 7a. The scheduler agent must be able to invoke the job's :run endpoint.
gcloud run jobs add-iam-policy-binding $EXPORT_JOB `
    --region $REGION `
    --project $PROJECT `
    --member "serviceAccount:$SCHED_AGENT" `
    --role "roles/run.invoker"
Must "grant run.invoker to scheduler agent on $EXPORT_JOB"

# 7b. Create-or-update the */10 HTTP scheduler that POSTs the Run :run URI as the
#     scheduler agent SA (OAuth; the Run jobs:run endpoint is a Google API).
$RUN_URI = "https://$REGION-run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/${EXPORT_JOB}:run"
if (Exists { gcloud scheduler jobs describe $SCHED --location $REGION --project $PROJECT }) {
    Write-Host "    updating scheduler $SCHED" -ForegroundColor Yellow
    gcloud scheduler jobs update http $SCHED `
        --location $REGION `
        --project $PROJECT `
        --schedule "*/10 * * * *" `
        --time-zone "Asia/Singapore" `
        --uri $RUN_URI `
        --http-method POST `
        --oauth-service-account-email $SCHED_AGENT
    Must "update scheduler $SCHED"
} else {
    Write-Host "    creating scheduler $SCHED" -ForegroundColor Yellow
    gcloud scheduler jobs create http $SCHED `
        --location $REGION `
        --project $PROJECT `
        --schedule "*/10 * * * *" `
        --time-zone "Asia/Singapore" `
        --uri $RUN_URI `
        --http-method POST `
        --oauth-service-account-email $SCHED_AGENT
    Must "create scheduler $SCHED"
}
Write-Host "[OK] scheduled $SCHED (every 10 min; job self-gates on _freshness.json)"

# =============================================================================
# Step 8 -- Build + deploy the dash web service with its env + secrets, then make
#           it reachable WITHOUT public IAM invoke.
# =============================================================================
Write-Host "[..] Building + deploying dash service $WEB_SERVICE" -ForegroundColor Cyan
$webImg = "$AR_HOST/$PROJECT/$REPO/${WEB_SERVICE}:$SHA"

# 8a. Build the dash image (build ONLY -- deploy ourselves below).
gcloud builds submit $DASH_DIR --tag $webImg --project $PROJECT
Must "build dash service image"

# 8b. Deploy the Cloud Run service AS YOURSELF with the web SA. It mounts the
#     password + session-key secrets and learns its bucket/object from env. The
#     web SA can read the secrets (Step 4) and read the bucket (Step 3). The env
#     and secret NAMES must match what dash/main.py reads: GCS_BUCKET, DATA_OBJECT,
#     SESSION_SECRET (signs the session cookie), DASH_PASSWORD.
#
#     Org policy: Domain Restricted Sharing REJECTS --allow-unauthenticated, so we
#     deploy with --no-invoker-iam-check instead -- Cloud Run skips the IAM invoker
#     check and the Flask app does its OWN password / SSO auth. NEVER use
#     --allow-unauthenticated here.
gcloud run deploy $WEB_SERVICE `
    --image $webImg `
    --region $REGION `
    --project $PROJECT `
    --service-account $WEB_SA `
    --no-invoker-iam-check `
    --set-env-vars "GCS_BUCKET=$BUCKET,DATA_OBJECT=$CLIENT.json" `
    --set-secrets "SESSION_SECRET=${KEY_SECRET}:latest,DASH_PASSWORD=${PW_SECRET}:latest"
Must "deploy dash service $WEB_SERVICE"
Write-Host "[OK] deployed $WEB_SERVICE (private invoke; app-level auth)"

Write-Host ""
Write-Host "[OK] tcs standup complete (tag $SHA)" -ForegroundColor Green
Write-Host "     dash service : $WEB_SERVICE  (map $CLIENT.agoradatadriven.com to it)"
Write-Host "     export job   : $EXPORT_JOB   (scheduler $SCHED, */10)"
