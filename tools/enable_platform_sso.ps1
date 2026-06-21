# enable_platform_sso.ps1 -- wire deployed dashboards to TRUST the portal SSO cookie.
#
# What this does: the portal (platform-dash) issues a signed cookie scoped to
# .agoradatadriven.com on login. This script mounts the shared platform-sso-key secret onto each
# <c>-dash service and tells it which CLIENT_KEY it is, so the dashboard's extended authed() will
# additively trust that portal cookie. The dashboard's OWN password ALWAYS still works -- SSO is
# purely additive, never a replacement.
#
# PREREQUISITES (in order):
#   (1) The portal standup already created the platform-sso-key secret (the HMAC signing key that
#       platform-dash signs the SSO cookie with and that each dashboard verifies it against). If it
#       does not exist yet, run the portal standup first -- this script only GRANTS and MOUNTS it.
#   (2) Each dashboard was rebuilt+redeployed with the image that contains platform_sso.py AND the
#       extended authed() that calls into it. A dashboard on an older image will ignore the mounted
#       secret entirely (the cookie check code is not in it).
#   (3) Dashboards are served on their <c>.agoradatadriven.com custom domains so the
#       .agoradatadriven.com cookie actually REACHES them. On a raw *.run.app host the browser never
#       sends the .agoradatadriven.com cookie, so SSO stays inert there -- but that is harmless: the
#       dashboard's own password login always still works regardless.
#
# Idempotent: safe to re-run. -Keys "a,b" limits to specific clients; default = all deployed <c>-dash.
# START with just `template`.

param([string]$Keys="")

$PROJECT    = "agora-data-driven"
$REGION     = "asia-southeast1"
$SSO_SECRET = "platform-sso-key"

# --- helpers ---------------------------------------------------------------------------------------
function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) {
    # Call AFTER a native command; fails the script if that command returned non-zero.
    if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" }
}

# This script gates on $LASTEXITCODE explicitly and stays on the default Continue: gcloud writes
# ordinary progress to stderr, and under $ErrorActionPreference="Stop" PowerShell would wrap that
# stderr as a terminating NativeCommandError and abort mid-script EVEN ON SUCCESS.

Write-Host "[..] enable_platform_sso :: project=$PROJECT region=$REGION secret=$SSO_SECRET"

# Confirm the shared SSO signing key exists before we try to grant/mount it (prereq 1).
gcloud secrets describe $SSO_SECRET --project=$PROJECT *> $null
if ($LASTEXITCODE -ne 0) {
    Die "secret '$SSO_SECRET' not found -- run the portal standup first (it creates the SSO signing key)"
}

# --- discover the deployed dashboards --------------------------------------------------------------
# Drive the client list from the deployed <c>-dash Cloud Run SERVICES (not a hardcoded list), so this
# tracks reality. We list service names, keep the ones ending in -dash, and derive <c> by stripping
# the suffix. platform-dash (the portal) and status-dash (the meta dashboard) are NOT per-client
# dashboards, so they are excluded.
$allServices = (gcloud run services list --project=$PROJECT --region=$REGION --format='value(metadata.name)')
Must "list Cloud Run services"

$dashServices = @($allServices | Where-Object { $_ -like '*-dash' -and $_ -ne 'platform-dash' -and $_ -ne 'status-dash' })

# Optional filter: -Keys "a,b" restricts to those client keys (matched as <key>-dash).
if ($Keys.Trim() -ne "") {
    $wanted = @($Keys.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
    $dashServices = @($dashServices | Where-Object {
        $c = $_.Substring(0, $_.Length - "-dash".Length)
        $wanted -contains $c
    })
    if ($dashServices.Count -eq 0) { Die "no deployed <c>-dash services matched -Keys '$Keys'" }
}

if ($dashServices.Count -eq 0) {
    Write-Host "[OK] no per-client <c>-dash services deployed yet -- nothing to wire (start with 'template')."
    exit 0
}

Write-Host ("[..] dashboards to wire: " + ($dashServices -join ", "))

# --- wire each dashboard ---------------------------------------------------------------------------
foreach ($svc in $dashServices) {
    $c = $svc.Substring(0, $svc.Length - "-dash".Length)
    Write-Host "[..] $svc (client '$c')"

    # Look up the service's ACTUAL runtime service account -- names differ per client and the SA may
    # be the per-client <c>-dash-web@ or something the operator set, so never assume; read it back.
    $sa = (gcloud run services describe $svc --project=$PROJECT --region=$REGION --format='value(spec.template.spec.serviceAccountName)')
    Must "describe runtime SA for $svc"
    if ([string]::IsNullOrWhiteSpace($sa)) { Die "$svc has no runtime service account set" }
    Write-Host "     runtime SA: $sa"

    # Grant that SA read access to the SSO signing key (idempotent: re-adding an existing binding is a no-op).
    gcloud secrets add-iam-policy-binding $SSO_SECRET `
        --project=$PROJECT `
        --member="serviceAccount:$sa" `
        --role="roles/secretmanager.secretAccessor" *> $null
    Must "grant secretAccessor on $SSO_SECRET to $sa"
    Write-Host "     [OK] granted secretAccessor on $SSO_SECRET"

    # Mount the secret as env var SSO_SECRET and tell the dashboard its own CLIENT_KEY. --update-* is
    # additive/idempotent: it leaves the dashboard's existing password secret and other env untouched.
    gcloud run services update $svc `
        --project=$PROJECT `
        --region=$REGION `
        --update-secrets "SSO_SECRET=$SSO_SECRET:latest" `
        --update-env-vars "CLIENT_KEY=$c" *> $null
    Must "update $svc with SSO secret + CLIENT_KEY"
    Write-Host "     [OK] mounted SSO_SECRET + set CLIENT_KEY=$c"
}

Write-Host "[OK] platform SSO wired for: $($dashServices -join ', ')"
Write-Host "     Reminder: SSO only fires on the <c>.agoradatadriven.com domain; each dashboard's own password always still works."
