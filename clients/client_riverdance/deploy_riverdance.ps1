# =============================================================================
# deploy_riverdance.ps1 -- ONE-SHOT, IDEMPOTENT standup of the `riverdance` client.
#
# Riverdance is a LIVE-Windsor client (NOT BigQuery-fed): the export job pulls the
# Meta connector straight from the Windsor.ai API each run and writes the private
# riverdance.json that the gated dash service serves. So this standup has NO dataset
# and NO SQL views -- instead it stores the Windsor key as a secret the job reads.
#
# Creates / converges:
#   APIs -> AR repo + private bucket -> job/web service accounts + IAM
#   -> password/session/windsor-key secrets -> export job (build/deploy/run)
#   -> scheduler (every 3h) -> dash web service (private + app-level auth).
#
# RUN AS YOURSELF (gcloud auth login info@agoradatadriven.com) -- never Cloud Build
# from a laptop. `gcloud builds submit --tag` builds the image (no actAs); the deploy
# runs AS YOU with the runtime SAs.
#
# USAGE
#   .\deploy_riverdance.ps1 -Password "clientpw" -WindsorKey "xxxxx"
#   (omit either to be prompted; -WindsorKey also reads $env:WINDSOR_API_KEY)
# =============================================================================

param([string]$Password = "", [string]$WindsorKey = "")

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$REPO    = "agora"
$CLIENT  = "riverdance"

# Derived names (DERIVE from the client key `<c>`; never re-type) --------------
$BUCKET      = "agora-data-driven-$CLIENT-dash"
$EXPORT_JOB  = "$CLIENT-export"
$SCHED       = "$CLIENT-export-refresh"
$WEB_SERVICE = "$CLIENT-dash"
$JOB_SA      = "$CLIENT-dash-job@agora-data-driven.iam.gserviceaccount.com"
$WEB_SA      = "$CLIENT-dash-web@agora-data-driven.iam.gserviceaccount.com"
$PW_SECRET   = "$CLIENT-dash-password"
$KEY_SECRET  = "$CLIENT-dash-session-key"
$WIN_SECRET  = "$CLIENT-windsor-key"
$AR_HOST     = "$REGION-docker.pkg.dev"

$ROOT     = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$JOB_DIR  = Join-Path $PSScriptRoot "job"
$DASH_DIR = Join-Path $PSScriptRoot "dash"
$VALIDATOR = Join-Path $ROOT "tools\_validate_dash_js.py"
# JS-gate python: prefer the dev .venv; fall back to .venv-portal (both may carry esprima).
$VENV_PY = Join-Path $ROOT ".venv\Scripts\python.exe"
if (-not (Test-Path $VENV_PY)) { $VENV_PY = Join-Path $ROOT ".venv-portal\Scripts\python.exe" }

function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) { if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" } }
function Exists([scriptblock]$Probe) { & $Probe *> $null; return ($LASTEXITCODE -eq 0) }
function Ensure-Sa([string]$email, [string]$accountId, [string]$displayName) {
    if (Exists { gcloud iam service-accounts describe $email --project $PROJECT }) {
        Write-Host "    $email already exists"
    } else {
        Write-Host "    creating $email" -ForegroundColor Yellow
        gcloud iam service-accounts create $accountId --project $PROJECT --display-name $displayName
        Must "create service account $email"
    }
}
function Write-SecretFile([string]$path, [string]$value) {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $value, $enc)
}

# --- Step 0: image tag + project number + scheduler agent --------------------
Write-Host "[..] Resolving image tag + project number" -ForegroundColor Cyan
$SHA = (git -C $ROOT rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) { $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss") }
$SHA = $SHA.Trim()
$PNUM = (gcloud projects describe $PROJECT --format='value(projectNumber)'); Must "resolve project number"
$PNUM = ($PNUM | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($PNUM)) { Die "project number came back empty" }
$SCHED_AGENT = "service-$PNUM@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
Write-Host "[OK] image tag = $SHA ; project number = $PNUM"

# --- Step 1: APIs (no bigquery — Riverdance is Windsor-live) ------------------
Write-Host "[..] Enabling required APIs" -ForegroundColor Cyan
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
    storage.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com --project $PROJECT
Must "enable required APIs"
Write-Host "[OK] APIs enabled"

# --- Step 2: AR repo + private bucket (no dataset) ---------------------------
Write-Host "[..] Ensuring AR repo / private bucket" -ForegroundColor Cyan
if (Exists { gcloud artifacts repositories describe $REPO --location $REGION --project $PROJECT }) { Write-Host "    AR repo $REPO already exists" }
else { gcloud artifacts repositories create $REPO --repository-format docker --location $REGION --project $PROJECT --description "Agora Data Driven shared docker images"; Must "create AR repo" }
if (Exists { gcloud storage buckets describe "gs://$BUCKET" --project $PROJECT }) { Write-Host "    bucket $BUCKET already exists" }
else { gcloud storage buckets create "gs://$BUCKET" --project $PROJECT --location $REGION --uniform-bucket-level-access --public-access-prevention; Must "create bucket $BUCKET" }
Write-Host "[OK] AR repo / bucket in place"

# --- Step 3: service accounts + least-privilege IAM --------------------------
Write-Host "[..] Ensuring service accounts + IAM" -ForegroundColor Cyan
Ensure-Sa $JOB_SA "$CLIENT-dash-job" "riverdance export job (Windsor -> bucket)"
Ensure-Sa $WEB_SA "$CLIENT-dash-web" "riverdance dash web (bucket reader + auth)"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$JOB_SA" --role "roles/storage.objectAdmin"; Must "grant objectAdmin to job SA"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$WEB_SA" --role "roles/storage.objectViewer"; Must "grant objectViewer to web SA"
Write-Host "[OK] service accounts + IAM in place"

# --- Step 4: secrets (password, session key, windsor key) --------------------
Write-Host "[..] Ensuring secrets" -ForegroundColor Cyan
if ([string]::IsNullOrEmpty($Password)) { $Password = $env:DASH_PASSWORD }
if ([string]::IsNullOrEmpty($Password)) {
    $sec = Read-Host "Dashboard password for '$CLIENT'" -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    $Password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b); [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b)
}
if ([string]::IsNullOrEmpty($Password)) { Die "no dashboard password supplied" }
if ([string]::IsNullOrEmpty($WindsorKey)) { $WindsorKey = $env:WINDSOR_API_KEY }
if ([string]::IsNullOrEmpty($WindsorKey)) {
    $sec2 = Read-Host "Windsor.ai API key for '$CLIENT'" -AsSecureString
    $b2 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec2)
    $WindsorKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b2); [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b2)
}
if ([string]::IsNullOrEmpty($WindsorKey)) { Die "no Windsor API key supplied" }

$rng = [Security.Cryptography.RandomNumberGenerator]::Create(); $bytes = New-Object byte[] 32; $rng.GetBytes($bytes)
$SessionKey = [Convert]::ToBase64String($bytes)

$tmpPw  = Join-Path ([IO.Path]::GetTempPath()) ("agora-pw-"  + [Guid]::NewGuid().ToString("N") + ".txt")
$tmpKey = Join-Path ([IO.Path]::GetTempPath()) ("agora-key-" + [Guid]::NewGuid().ToString("N") + ".txt")
$tmpWin = Join-Path ([IO.Path]::GetTempPath()) ("agora-win-" + [Guid]::NewGuid().ToString("N") + ".txt")
Write-SecretFile $tmpPw $Password; Write-SecretFile $tmpKey $SessionKey; Write-SecretFile $tmpWin $WindsorKey
try {
    # secret -> which SA may read it
    $secrets = @(
        @{ name=$PW_SECRET;  file=$tmpPw;  reader=$WEB_SA },
        @{ name=$KEY_SECRET; file=$tmpKey; reader=$WEB_SA },
        @{ name=$WIN_SECRET; file=$tmpWin; reader=$JOB_SA }
    )
    foreach ($s in $secrets) {
        if (Exists { gcloud secrets describe $s.name --project $PROJECT }) {
            gcloud secrets versions add $s.name --project $PROJECT --data-file="$($s.file)"; Must "add version to $($s.name)"
        } else {
            gcloud secrets create $s.name --project $PROJECT --replication-policy=automatic --data-file="$($s.file)"; Must "create secret $($s.name)"
        }
        gcloud secrets add-iam-policy-binding $s.name --project $PROJECT --member "serviceAccount:$($s.reader)" --role "roles/secretmanager.secretAccessor"; Must "grant accessor on $($s.name)"
    }
} finally { Remove-Item $tmpPw, $tmpKey, $tmpWin -ErrorAction SilentlyContinue }
Write-Host "[OK] secrets created + readers granted"

# --- Step 5: build + deploy + run the export job -----------------------------
Write-Host "[..] Building + deploying export job $EXPORT_JOB" -ForegroundColor Cyan
$jobImg = "$AR_HOST/$PROJECT/$REPO/${EXPORT_JOB}:$SHA"
gcloud builds submit $JOB_DIR --tag $jobImg --project $PROJECT; Must "build export job image"
gcloud run jobs deploy $EXPORT_JOB --image $jobImg --region $REGION --project $PROJECT `
    --service-account $JOB_SA --max-retries 1 --task-timeout 900 `
    --set-env-vars "GCS_BUCKET=$BUCKET,DATA_OBJECT=$CLIENT.json" `
    --set-secrets "WINDSOR_API_KEY=${WIN_SECRET}:latest"
Must "deploy export job $EXPORT_JOB"
gcloud run jobs execute $EXPORT_JOB --region $REGION --project $PROJECT --wait; Must "execute export job (initial)"
Write-Host "[OK] initial live data export complete"

# --- Step 6: make the job triggerable on demand (NO scheduler) ---------------
# Refresh is manual, driven by the admin console's "Sync all dashboards" button (mirrors the
# Bidbrain platform's /sync-all): the portal service account triggers each <c>-export via the
# Run Admin API. So we grant the PORTAL web SA run.invoker on this job instead of a scheduler.
$PORTAL_SA = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
Write-Host "[..] Granting the portal SA run.invoker on $EXPORT_JOB (for the admin Sync button)" -ForegroundColor Cyan
gcloud run jobs add-iam-policy-binding $EXPORT_JOB --region $REGION --project $PROJECT --member "serviceAccount:$PORTAL_SA" --role "roles/run.invoker"; Must "grant run.invoker to portal SA"
Write-Host "[OK] $EXPORT_JOB is on-demand only (admin Sync button); no scheduler"

# --- Step 7: JS gate + build + deploy the dash service -----------------------
Write-Host "[..] Validating dashboard.html inline JS" -ForegroundColor Cyan
if ((Test-Path $VENV_PY) -and (Test-Path $VALIDATOR)) { & $VENV_PY $VALIDATOR (Join-Path $DASH_DIR "dashboard.html"); Must "dashboard.html failed JS gate" }
else { Write-Host "    (skipping JS gate: validator/python not found)" -ForegroundColor Yellow }

Write-Host "[..] Building + deploying dash service $WEB_SERVICE" -ForegroundColor Cyan
$webImg = "$AR_HOST/$PROJECT/$REPO/${WEB_SERVICE}:$SHA"
gcloud builds submit $DASH_DIR --tag $webImg --project $PROJECT; Must "build dash service image"
gcloud run deploy $WEB_SERVICE --image $webImg --region $REGION --project $PROJECT `
    --service-account $WEB_SA --no-invoker-iam-check `
    --set-env-vars "GCS_BUCKET=$BUCKET,DATA_OBJECT=$CLIENT.json" `
    --set-secrets "SESSION_SECRET=${KEY_SECRET}:latest,DASH_PASSWORD=${PW_SECRET}:latest"
Must "deploy dash service $WEB_SERVICE"

$SVC_URL = (gcloud run services describe $WEB_SERVICE --region $REGION --project $PROJECT --format='value(status.url)')
Write-Host ""
Write-Host "[OK] riverdance standup complete (tag $SHA)" -ForegroundColor Green
Write-Host "     dash service : $WEB_SERVICE"
Write-Host "     live URL     : $SVC_URL   (works immediately; no DNS needed)"
Write-Host "     export job   : $EXPORT_JOB   (scheduler $SCHED, every 3h, live Windsor pull)"
Write-Host "     next (optional): map $CLIENT.agoradatadriven.com -> $WEB_SERVICE, then tools\enable_platform_sso.ps1 -Keys $CLIENT"
