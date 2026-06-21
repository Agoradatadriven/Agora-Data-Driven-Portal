# =============================================================================
# deploy.ps1 -- ONE-SHOT, IDEMPOTENT standup of the portal/CRM front-door
#                        (the Cloud Run service `platform-dash` at
#                        portal.agoradatadriven.com).
#
# What this stands up, end to end (re-running converges to the same state):
#   1. enable the GCP APIs the portal needs
#   2. ensure the shared Artifact Registry repo `agora` + the PRIVATE registry bucket
#      `agora-data-driven-platform-dash` (holds the ONE registry JSON; no database)
#   3. create the web service account platform-dash-web@ + least-privilege IAM
#      (objectAdmin on its own bucket, secretAccessor)
#   4. create two secrets -- platform-dash-session-key (Flask session signer) AND the
#      SHARED platform-sso-key (the HMAC key dashboards will additively trust) -- via
#      UTF-8-no-BOM temp files, and grant the web SA secretAccessor on both
#   5. build + deploy platform-dash with COOKIE_DOMAIN + the two secrets, deployed
#      with --no-invoker-iam-check (org forbids public Cloud Run)
#   6. seed the registry JSON by running the repo .venv python on dash/seed_registry.py
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop. We use `gcloud builds submit
# --tag` ONLY to build the image (no actAs needed for a build), then deploy the Cloud
# Run service from this laptop AS YOU (you do have actAs on the runtime SA). A
# cloudbuild-driven deploy would fail: the Cloud Build SA cannot
# iam.serviceAccounts.actAs the runtime SA.
#
# NOTE on SSO + super-admin: this standup CREATES the shared platform-sso-key here (it
# is the portal that MINTS the SSO cookie). Wiring the deployed <c>-dash dashboards to
# additively TRUST that cookie is a separate, additive step --
# tools\enable_platform_sso.ps1. Granting the portal god-mode (super-admin console)
# is likewise separate -- tools\enable_super_admin.ps1. Keep those out of this
# standup so the front-door can come up before any dashboards exist.
#
# USAGE
#   .\services\portal\deploy.ps1            # full standup / converge
#   .\services\portal\deploy.ps1 -SkipBuild # reuse current image, redeploy only
#   .\services\portal\deploy.ps1 -SkipSeed  # skip the registry seed step
# =============================================================================

param(
    [switch]$SkipBuild,
    [switch]$SkipSeed
)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT  = "agora-data-driven"
$REGION   = "asia-southeast1"   # Singapore. One region, never another.
$REPO     = "agora"             # shared Artifact Registry docker repo
$PLATFORM = "platform-dash"     # the portal Cloud Run service
$WEB_SA   = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
$BUCKET   = "agora-data-driven-platform-dash"  # PRIVATE; holds the ONE registry JSON

# Cookie domain the portal mints the SSO cookie on. Leading dot -> shared across every
# <c>.agoradatadriven.com dashboard.
$COOKIE_DOMAIN = ".agoradatadriven.com"

# Secrets the portal owns:
#   - platform-dash-session-key : signs the portal's own Flask session cookie.
#   - platform-sso-key          : the SHARED HMAC signing key the portal signs the
#                                 cross-subdomain SSO cookie with, and that each
#                                 dashboard verifies against (vendored platform_sso.py).
$SESSION_SECRET = "platform-dash-session-key"
$SSO_SECRET     = "platform-sso-key"

# NOTE: This script stays on the default $ErrorActionPreference = "Continue".
# gcloud writes ordinary progress to stderr; under "Stop" PowerShell wraps that
# stderr as a terminating NativeCommandError and aborts mid-script EVEN ON
# SUCCESS. We instead gate on $LASTEXITCODE explicitly via Must.

# --- Helpers (Die / Must / Exists / Write-SecretFile) ------------------------
function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) {
    # Call AFTER a native command; fails the script if that command returned non-zero.
    if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" }
}
function Exists([scriptblock]$Probe) {
    # Idempotency probe: returns $true iff the describe command exits 0, WITHOUT letting
    # its stderr (the "not found" message) abort the script. We are on Continue here, so
    # we only need to swallow the output and read $LASTEXITCODE.
    & $Probe *> $null
    return ($LASTEXITCODE -eq 0)
}
function Write-SecretFile([string]$path, [string]$value) {
    # Secret Manager stores bytes verbatim. A UTF-8 BOM or a trailing newline becomes part
    # of the secret and silently breaks password/hmac comparisons. Always write secrets
    # through a temp file encoded UTF-8 *without* BOM and *without* a trailing newline,
    # then --data-file= it.
    $enc = New-Object System.Text.UTF8Encoding($false)   # $false = no BOM
    [System.IO.File]::WriteAllText($path, $value, $enc)   # WriteAllText adds NO trailing newline
}

function New-RandomSecret {
    # 24 random bytes -> URL-safe base64 (no padding) so it pastes cleanly anywhere and
    # contains no bytes that would confuse an env/cookie/header.
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_').TrimEnd('=')
}

function Ensure-RandomSecret([string]$name) {
    # Create the secret + a first random version only if it does not already exist. An
    # existing secret is LEFT UNTOUCHED -- never rotate a live signing key on a re-run, or
    # every portal session / SSO cookie minted so far would be invalidated.
    if (Exists { gcloud secrets describe $name --project=$PROJECT }) {
        Write-Host "    [OK] secret $name already exists (value untouched)"
        return
    }
    Write-Host "    creating secret $name" -ForegroundColor Yellow
    gcloud secrets create $name --project=$PROJECT --replication-policy="automatic" *> $null
    Must "create secret $name"

    $tmp = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "$name.tmp")
    try {
        Write-SecretFile $tmp (New-RandomSecret)
        gcloud secrets versions add $name --project=$PROJECT --data-file="$tmp" *> $null
        Must "add first version of $name"
    } finally {
        if (Test-Path $tmp) { Remove-Item $tmp -Force }   # never leave secret material on disk
    }
    Write-Host "    [OK] secret $name created"
}

# Paths resolved from THIS script's own location so it runs from any working directory.
$HERE      = $PSScriptRoot
$REPO_ROOT = (Resolve-Path (Join-Path $HERE "..\..")).Path
$DASH_DIR  = Join-Path $HERE "dash"
$VENV_PY   = Join-Path $REPO_ROOT ".venv\Scripts\python.exe"
$SEED_PY   = Join-Path $DASH_DIR "seed_registry.py"

Write-Host "[..] deploy_platform :: project=$PROJECT region=$REGION service=$PLATFORM"

# =============================================================================
# Step 1 -- Enable the APIs the portal needs. Idempotent (already-enabled is a no-op).
# =============================================================================
Write-Host "[..] Enabling required APIs" -ForegroundColor Cyan
gcloud services enable `
    run.googleapis.com `
    artifactregistry.googleapis.com `
    cloudbuild.googleapis.com `
    storage.googleapis.com `
    secretmanager.googleapis.com `
    iam.googleapis.com `
    --project=$PROJECT
Must "enable required APIs"
Write-Host "[OK] APIs enabled"

# =============================================================================
# Step 2 -- Ensure the shared Artifact Registry repo + the PRIVATE registry bucket.
#           The registry is ONE private JSON object in this bucket -- there is no
#           database. The bucket is created with uniform bucket-level access and NO
#           public grant; it is never made public.
# =============================================================================
Write-Host "[..] Ensuring Artifact Registry repo '$REPO'" -ForegroundColor Cyan
if (Exists { gcloud artifacts repositories describe $REPO --location=$REGION --project=$PROJECT }) {
    Write-Host "    [OK] AR repo '$REPO' already exists"
} else {
    Write-Host "    creating AR repo '$REPO'" -ForegroundColor Yellow
    gcloud artifacts repositories create $REPO `
        --repository-format=docker `
        --location=$REGION `
        --project=$PROJECT `
        --description="Agora Data Driven shared docker images"
    Must "create Artifact Registry repo $REPO"
    Write-Host "    [OK] AR repo '$REPO' created"
}

Write-Host "[..] Ensuring PRIVATE registry bucket 'gs://$BUCKET'" -ForegroundColor Cyan
if (Exists { gcloud storage buckets describe "gs://$BUCKET" --project=$PROJECT }) {
    Write-Host "    [OK] bucket 'gs://$BUCKET' already exists"
} else {
    Write-Host "    creating bucket 'gs://$BUCKET'" -ForegroundColor Yellow
    # uniform-bucket-level-access keeps ACLs off; access is governed purely by IAM. No
    # public member is ever added -- the registry JSON is private and read only by the
    # portal's runtime SA.
    gcloud storage buckets create "gs://$BUCKET" `
        --project=$PROJECT `
        --location=$REGION `
        --uniform-bucket-level-access
    Must "create bucket $BUCKET"
    Write-Host "    [OK] bucket 'gs://$BUCKET' created"
}

# =============================================================================
# Step 3 -- Ensure the portal web service account + least-privilege IAM.
#           Every binding here is idempotent (create/grant is add-if-missing).
# =============================================================================
Write-Host "[..] Ensuring web service account + IAM" -ForegroundColor Cyan

# 3a. The runtime SA itself (create only if absent; describe is the idempotency probe).
if (Exists { gcloud iam service-accounts describe $WEB_SA --project=$PROJECT }) {
    Write-Host "    [OK] $WEB_SA already exists"
} else {
    Write-Host "    creating $WEB_SA" -ForegroundColor Yellow
    gcloud iam service-accounts create "platform-dash-web" `
        --project=$PROJECT `
        --display-name="Agora portal (platform-dash) web runtime"
    Must "create platform-dash-web service account"
    Write-Host "    [OK] $WEB_SA created"
}

# 3b. objectAdmin on its OWN registry bucket -- the portal reads AND writes the registry
#     JSON (add/edit client records, notes, tasks from the CRM/admin console).
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" `
    --member="serviceAccount:$WEB_SA" `
    --role="roles/storage.objectAdmin"
Must "grant storage.objectAdmin on $BUCKET to $WEB_SA"
Write-Host "    [OK] granted storage.objectAdmin on $BUCKET"

# 3c. Project-level secretAccessor so the portal can read the secrets it mounts. (The
#     per-secret grants in Step 4 are the tight version; this project-level grant also
#     covers secrets the super-admin console reads later, e.g. the super-admin password.)
gcloud projects add-iam-policy-binding $PROJECT `
    --member="serviceAccount:$WEB_SA" `
    --role="roles/secretmanager.secretAccessor" `
    --condition=None
Must "grant project secretmanager.secretAccessor to $WEB_SA"
Write-Host "    [OK] granted secretmanager.secretAccessor"

Write-Host "[OK] web SA + IAM in place"

# =============================================================================
# Step 4 -- Ensure the two portal secrets (random, created-once) + per-secret grants.
#           platform-sso-key is the SHARED key dashboards will trust additively, so it
#           is created HERE during the portal standup; enable_platform_sso.ps1 only
#           grants+mounts it onto each dashboard later.
# =============================================================================
Write-Host "[..] Ensuring portal secrets" -ForegroundColor Cyan
Ensure-RandomSecret $SESSION_SECRET
Ensure-RandomSecret $SSO_SECRET

foreach ($s in @($SESSION_SECRET, $SSO_SECRET)) {
    gcloud secrets add-iam-policy-binding $s `
        --project=$PROJECT `
        --member="serviceAccount:$WEB_SA" `
        --role="roles/secretmanager.secretAccessor" *> $null
    Must "grant secretAccessor on $s to $WEB_SA"
    Write-Host "    [OK] granted secretAccessor on $s"
}
Write-Host "[OK] portal secrets ready"

# =============================================================================
# Step 5 -- Build the image, then deploy the portal Cloud Run service AS YOURSELF.
#
#   Org policy: Domain Restricted Sharing rejects --allow-unauthenticated. Deploy with
#   --no-invoker-iam-check instead; the Flask app does its OWN password/SSO auth in
#   process. The registry JSON is private and reached only through the portal's
#   authenticated session.
# =============================================================================
Write-Host "[..] Resolving image tag" -ForegroundColor Cyan
$SHA = (git -C $HERE rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) {
    # Not a git repo (or no commits yet): fall back to a timestamped manual tag so the
    # image is still uniquely identifiable.
    $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss")
    Write-Host "    not a git repo; using fallback tag $SHA" -ForegroundColor Yellow
}
$SHA = $SHA.Trim()
$AR_HOST = "$REGION-docker.pkg.dev"
$IMG = "$AR_HOST/$PROJECT/$REPO/${PLATFORM}:$SHA"
Write-Host "[OK] image = $IMG"

if (-not $SkipBuild) {
    if (-not (Test-Path $DASH_DIR)) { Die "portal dash build dir not found: $DASH_DIR" }
    Write-Host "[..] Building image $IMG" -ForegroundColor Cyan
    gcloud builds submit $DASH_DIR --tag $IMG --project=$PROJECT
    Must "build image for $PLATFORM"
    Write-Host "[OK] built $IMG"
} else {
    Write-Host "[..] -SkipBuild: deploying existing image $IMG" -ForegroundColor Yellow
}

Write-Host "[..] Deploying Cloud Run service $PLATFORM" -ForegroundColor Cyan
gcloud run deploy $PLATFORM `
    --image $IMG `
    --region $REGION `
    --project $PROJECT `
    --service-account $WEB_SA `
    --no-invoker-iam-check `
    --update-env-vars "COOKIE_DOMAIN=$COOKIE_DOMAIN,REGISTRY_BUCKET=$BUCKET,REGISTRY_OBJECT=platform.json" `
    --update-secrets "SESSION_SECRET=${SESSION_SECRET}:latest,SSO_SECRET=${SSO_SECRET}:latest"
Must "deploy Cloud Run service $PLATFORM"
Write-Host "[OK] deployed $PLATFORM (tag $SHA)"

# =============================================================================
# Step 6 -- Seed the registry JSON (the ONE private object the portal reads). This is a
#           code/seed change, not upstream data, so it is its OWN step (the portal has
#           no freshness gate). seed_registry.py uploads an initial platform.json to the
#           registry bucket via ADC -- run it with the repo .venv python.
# =============================================================================
if (-not $SkipSeed) {
    Write-Host "[..] Seeding registry JSON via $SEED_PY" -ForegroundColor Cyan
    if (-not (Test-Path $VENV_PY)) { Die "repo .venv python not found at $VENV_PY (run tools\setup.ps1 first)" }
    if (-not (Test-Path $SEED_PY)) { Die "seed_registry.py not found at $SEED_PY" }
    & $VENV_PY $SEED_PY
    Must "seed registry JSON"
    Write-Host "[OK] registry JSON seeded"
} else {
    Write-Host "[..] -SkipSeed: leaving the registry JSON as-is" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[OK] platform standup complete (tag $SHA)" -ForegroundColor Green
Write-Host "     Next steps:"
Write-Host "       - map portal.agoradatadriven.com onto the $PLATFORM service"
Write-Host "       - .\tools\enable_super_admin.ps1                      # super-admin console"
Write-Host "       - .\tools\enable_platform_sso.ps1 -Keys ""template""  # trust portal SSO on dashboards"
