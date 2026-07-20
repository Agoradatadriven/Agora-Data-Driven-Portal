# =============================================================================
# deploy_dash_platform.ps1 -- REDEPLOY the portal Cloud Run service `platform-dash`
#                             ONLY (build a fresh image, then create-or-update the
#                             service). This is the fast inner-loop redeploy.
#
# Use this for code/template changes to the portal after the one-time standup
# (services\portal\deploy.ps1) has already created the SA, bucket, secrets,
# IAM, and APIs. This script does NOT touch IAM, buckets, or secrets -- it only
# rebuilds the image and rolls the service. The secrets it mounts must already exist
# (they are created by deploy.ps1).
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop. We use `gcloud builds submit
# --tag` ONLY to build the image (no actAs needed for a build), then deploy the Cloud
# Run service from this laptop AS YOU (you do have actAs on the runtime SA). A
# cloudbuild-driven deploy would fail: the Cloud Build SA cannot
# iam.serviceAccounts.actAs the runtime SA.
#
# Idempotent: `gcloud run deploy` is create-or-update.
#
# USAGE
#   .\deploy_dash_platform.ps1            # build, redeploy
#   .\deploy_dash_platform.ps1 -SkipBuild # reuse current image, redeploy only
# =============================================================================

param([switch]$SkipBuild, [switch]$Force)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT  = "agora-data-driven"
$REGION   = "asia-southeast1"   # Singapore. One region, never another.
$REPO     = "agora"             # shared Artifact Registry docker repo
$PLATFORM = "platform-dash"     # the portal Cloud Run service
$WEB_SA   = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
$BUCKET   = "agora-data-driven-platform-dash"  # PRIVATE registry bucket

# Cookie domain the portal mints the SSO cookie on (leading dot -> all subdomains).
$COOKIE_DOMAIN = ".agoradatadriven.com"

# Google Tag Manager container loaded site-wide on every portal HTML page (GA4 is configured INSIDE
# this container in the GTM UI). Set to "" to ship with GTM OFF; the app injects nothing unless this
# is non-empty. Local preview never runs this script, so it stays untracked.
$GTM_CONTAINER_ID = "GTM-KKWX37RG"

# Secrets mounted as env vars (Secret Manager, :latest). Created by deploy.ps1.
$SESSION_SECRET = "platform-dash-session-key"
$SSO_SECRET     = "platform-sso-key"

# The public origin the portal is served on -- used to build the Google OAuth redirect URI.
$PORTAL_BASE_URL = "https://portal.agoradatadriven.com"

# Google Sign-In (OPT-IN). These Secret Manager secrets are mounted ONLY when they exist, so a
# default deploy (before you create them) is unaffected -- the login page simply hides the Google
# button. Create them once (paste the OAuth client from Google Cloud Console -> Credentials):
#   "PASTE_CLIENT_ID"     | gcloud secrets create google-oauth-client-id     --data-file=- --project=agora-data-driven
#   "PASTE_CLIENT_SECRET" | gcloud secrets create google-oauth-client-secret --data-file=- --project=agora-data-driven
# then grant the web SA access to each (roles/secretmanager.secretAccessor) and redeploy.
$OAUTH_ID_SECRET     = "google-oauth-client-id"
$OAUTH_SECRET_SECRET = "google-oauth-client-secret"

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

# Resolve the build dir from THIS script's own location (the Dockerfile/main.py sit next
# to this script) so it works regardless of the caller's current directory.
$DASH_DIR = $PSScriptRoot

# =============================================================================
# Step 0 -- STALE-DEPLOY GUARD. Cloud Run is last-deploy-wins: deploying an out-of-date or
# divergent local tree silently REVERTS production to whatever you have (this bit us repeatedly
# 2026-07-16 -- teammates deploying unpushed local commits kept rolling back live features). So
# refuse to deploy unless HEAD == origin/main. Bypass with -Force ONLY when you are certain your
# tree is the intended one (e.g. a hotfix you have not pushed yet).
# =============================================================================
if (-not $Force) {
    git -C $DASH_DIR fetch origin --quiet 2>$null
    $head = (git -C $DASH_DIR rev-parse HEAD 2>$null)
    $origin = (git -C $DASH_DIR rev-parse origin/main 2>$null)
    if ($head -and $origin -and ($head.Trim() -ne $origin.Trim())) {
        Write-Host "[BLOCKED] Your HEAD is not origin/main:" -ForegroundColor Red
        Write-Host "            HEAD       = $($head.Trim().Substring(0,7))" -ForegroundColor Red
        Write-Host "            origin/main= $($origin.Trim().Substring(0,7))" -ForegroundColor Red
        Write-Host "          Deploying now would REVERT production to your tree. Run 'git pull' (or" -ForegroundColor Red
        Write-Host "          land your branch on main) first. Use -Force only if you KNOW this tree is right." -ForegroundColor Red
        exit 1
    }
    $dirty = (git -C $DASH_DIR status --porcelain 2>$null)
    if ($dirty) {
        Write-Host "[WARN] Working tree has uncommitted changes -- they WILL be built into the image" -ForegroundColor Yellow
        Write-Host "       but are NOT committed. Commit + push after deploying, or another machine's" -ForegroundColor Yellow
        Write-Host "       sync/deploy may clobber them." -ForegroundColor Yellow
    }
    Write-Host "[OK] tree matches origin/main -- safe to deploy" -ForegroundColor Green
}

# =============================================================================
# Step 1 -- Resolve a short git SHA for the image tag.
# =============================================================================
Write-Host "[..] Resolving image tag" -ForegroundColor Cyan
$SHA = (git -C $DASH_DIR rev-parse --short HEAD 2>$null)
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

# =============================================================================
# Step 2 -- Build the image (build ONLY -- no actAs needed; we deploy ourselves).
# =============================================================================
if (-not $SkipBuild) {
    Write-Host "[..] Building image $IMG" -ForegroundColor Cyan
    gcloud builds submit $DASH_DIR --tag $IMG --project=$PROJECT
    Must "build image for $PLATFORM"
    Write-Host "[OK] built $IMG"
} else {
    Write-Host "[..] -SkipBuild: deploying existing image $IMG" -ForegroundColor Yellow
}

# =============================================================================
# Step 3 -- Deploy the portal Cloud Run service AS YOURSELF with the runtime SA.
#
#   Org policy: Domain Restricted Sharing rejects --allow-unauthenticated. Deploy with
#   --no-invoker-iam-check instead; the Flask app does its OWN password/SSO auth in
#   process, and the private registry JSON is only ever read behind the portal login.
# =============================================================================
# Assemble the env-var list; only ship GTM_CONTAINER_ID when it's set (empty -> GTM stays off).
$ENV_VARS = "COOKIE_DOMAIN=$COOKIE_DOMAIN,REGISTRY_BUCKET=$BUCKET,REGISTRY_OBJECT=platform.json,PORTAL_BASE_URL=$PORTAL_BASE_URL"
if (-not [string]::IsNullOrWhiteSpace($GTM_CONTAINER_ID)) { $ENV_VARS += ",GTM_CONTAINER_ID=$GTM_CONTAINER_ID" }

# Assemble the secret list. Google sign-in secrets are appended ONLY if they exist (opt-in), so a
# default deploy still works before they're created and the login page just hides the Google button.
$SECRETS = "SESSION_SECRET=${SESSION_SECRET}:latest,SSO_SECRET=${SSO_SECRET}:latest"
function Test-SecretExists([string]$name) {
    gcloud secrets describe $name --project=$PROJECT 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}
if ((Test-SecretExists $OAUTH_ID_SECRET) -and (Test-SecretExists $OAUTH_SECRET_SECRET)) {
    $SECRETS += ",GOOGLE_OAUTH_CLIENT_ID=${OAUTH_ID_SECRET}:latest,GOOGLE_OAUTH_CLIENT_SECRET=${OAUTH_SECRET_SECRET}:latest"
    Write-Host "[OK] Google sign-in secrets found -- mounting them (the Google button turns ON)" -ForegroundColor Green
} else {
    Write-Host "[..] Google sign-in secrets absent -- deploying WITHOUT them (button stays off)" -ForegroundColor Yellow
}

# Market-Intelligence AI brain. Two providers, both OPTIONAL:
#   * Gemini via VERTEX AI -- GCP-billed (one card, one invoice; NO API key). Enable the API + grant
#     the runtime SA aiplatform.user, then flip VERTEX_GEMINI_ENABLED=1 so the tab offers Gemini.
#   * DeepSeek via its API key secret (mounted if present).
Write-Host "[..] Enabling Vertex AI (GCP-billed Gemini) + granting the runtime SA" -ForegroundColor Cyan
gcloud services enable aiplatform.googleapis.com --project=$PROJECT *> $null
gcloud projects add-iam-policy-binding $PROJECT `
    --member="serviceAccount:$WEB_SA" --role="roles/aiplatform.user" *> $null
$ENV_VARS += ",VERTEX_GEMINI_ENABLED=1,VERTEX_PROJECT=$PROJECT,VERTEX_LOCATION=global"
Write-Host "[OK] Vertex Gemini available (project $PROJECT, location $REGION)" -ForegroundColor Green

# Assistant HYBRID search (semantic embeddings fused with BM25). Reuses the SAME Vertex AI + runtime
# SA just enabled above -- text-embedding-005 lives on aiplatform.googleapis.com with the same
# aiplatform.user role -- so it needs NO new API/IAM and costs ~$0.15/1M tokens (indexed once per data
# change + one tiny embed per question). ON by default; the Assistant falls back to pure BM25 if an
# embedding call ever fails. text-embedding-005 is a REGIONAL model (not served at `global`), so its
# location is pinned to $REGION -- the client's private chunk text stays in-region (unlike the Gemini
# brain, which only ever sends public news + keywords to `global`).
$ENV_VARS += ",ASSISTANT_EMBED_ENABLED=1,VERTEX_EMBED_LOCATION=$REGION"
Write-Host "[OK] Assistant hybrid search ON (text-embedding-005 in $REGION)" -ForegroundColor Green

# Assistant cross-encoder RERANKING (Vertex/Discovery Engine Ranking API). This is the ONE Assistant
# piece needing a new API + IAM, so it is OPT-IN: run enable_assistant_reranking.ps1 once (it enables
# discoveryengine.googleapis.com + grants the web SA roles/discoveryengine.user). This deploy then
# DETECTS the API is enabled and turns reranking on; absent, retrieval is hybrid (BM25+vector+RRF)
# without the rerank pass.
function Test-ApiEnabled([string]$api) {
    $v = (gcloud services list --enabled --project=$PROJECT --filter="config.name:$api" `
          --format="value(config.name)" 2>$null)
    return (-not [string]::IsNullOrWhiteSpace($v))
}
if (Test-ApiEnabled "discoveryengine.googleapis.com") {
    $ENV_VARS += ",ASSISTANT_RERANK_ENABLED=1"
    Write-Host "[OK] Ranking API enabled -- Assistant reranking ON (semantic-ranker-fast-004)" -ForegroundColor Green
} else {
    Write-Host "[..] Ranking API absent -- reranking OFF (run enable_assistant_reranking.ps1 to turn it on)" -ForegroundColor Yellow
}

if (Test-SecretExists "DEEPSEEK_API_KEY") {
    gcloud secrets add-iam-policy-binding "DEEPSEEK_API_KEY" --project=$PROJECT `
        --member="serviceAccount:$WEB_SA" --role="roles/secretmanager.secretAccessor" *> $null
    $SECRETS += ",DEEPSEEK_API_KEY=DEEPSEEK_API_KEY:latest"
    Write-Host "[OK] DEEPSEEK_API_KEY found -- mounting it (DeepSeek models available)" -ForegroundColor Green
} else {
    Write-Host "[..] DEEPSEEK_API_KEY absent -- DeepSeek models stay unavailable (Gemini still works)" -ForegroundColor Yellow
}

# Atrium Mail (OPT-IN, mailroom.py). App-password (imap) mailboxes need nothing from the deploy;
# the Workspace-delegation (dwd) connector needs the one-time enable_atrium_mail.ps1, after which
# the mail-sync SA exists and we ship its address so the app can mint delegated Gmail tokens.
gcloud iam service-accounts describe "mail-sync@$PROJECT.iam.gserviceaccount.com" --project=$PROJECT 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    $ENV_VARS += ",MAIL_DWD_SA=mail-sync@$PROJECT.iam.gserviceaccount.com"
    Write-Host "[OK] mail-sync SA found -- Workspace (dwd) mailboxes enabled on the portal" -ForegroundColor Green
} else {
    Write-Host "[..] mail-sync SA absent -- Mail tab runs imap mailboxes only (run enable_atrium_mail.ps1 for dwd)" -ForegroundColor Yellow
}

# Watcher egress proxy (OPT-IN). YouTube blocks datacenter IPs, so transcript fetching from Cloud
# Run needs a residential proxy. Create the secret once with the full proxy URL, e.g. Webshare
# rotating residential  http://USER-rotate:PASS@p.webshare.io:80 :
#   "PASTE_PROXY_URL" | gcloud secrets create watcher-proxy-url --data-file=- --project=agora-data-driven
# then redeploy. Absent secret -> the Watcher tab still works but YouTube may rate-limit fetches.
if (Test-SecretExists "watcher-proxy-url") {
    gcloud secrets add-iam-policy-binding "watcher-proxy-url" --project=$PROJECT `
        --member="serviceAccount:$WEB_SA" --role="roles/secretmanager.secretAccessor" *> $null
    $SECRETS += ",WATCHER_PROXY_URL=watcher-proxy-url:latest"
    Write-Host "[OK] watcher-proxy-url found -- mounting it (Watcher fetches go through the proxy)" -ForegroundColor Green
} else {
    Write-Host "[..] watcher-proxy-url absent -- Watcher fetches directly (YouTube may rate-limit)" -ForegroundColor Yellow
}

Write-Host "[..] Deploying Cloud Run service $PLATFORM" -ForegroundColor Cyan
gcloud run deploy $PLATFORM `
    --image $IMG `
    --region $REGION `
    --project $PROJECT `
    --service-account $WEB_SA `
    --no-invoker-iam-check `
    --update-env-vars $ENV_VARS `
    --update-secrets $SECRETS
Must "deploy Cloud Run service $PLATFORM"
Write-Host "[OK] deployed $PLATFORM (tag $SHA)" -ForegroundColor Green
