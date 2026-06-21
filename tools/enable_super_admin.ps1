# enable_super_admin.ps1 -- grant the portal front-door (platform-dash) god-mode.
#
# What this does: gives the portal web SA a bootstrap "super admin" password plus the IAM it needs to
# act as an operator console -- redeploy/rotate dashboards and rotate per-client passwords -- all from
# the portal UI instead of a laptop. Run once during platform standup; safe to re-run.
#
# Idempotent: re-adding an existing IAM binding is a no-op; an already-present secret is left alone.

param([string]$SuperPw="")

$PROJECT      = "agora-data-driven"
$REGION       = "asia-southeast1"
$PLATFORM     = "platform-dash"
$WEB_SA       = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
$SUPER_SECRET = "platform-super-admin-password"

# --- helpers ---------------------------------------------------------------------------------------
function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) {
    # Call AFTER a native command; fails the script if that command returned non-zero.
    if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" }
}

function Write-SecretFile([string]$path, [string]$value) {
    # Secret Manager stores bytes verbatim. A UTF-8 BOM or a trailing newline becomes part of the
    # secret and silently breaks password/hmac comparisons. Always write secrets through a temp file
    # encoded UTF-8 *without* BOM and *without* a trailing newline, then --data-file= it.
    $enc = New-Object System.Text.UTF8Encoding($false)   # $false = no BOM
    [System.IO.File]::WriteAllText($path, $value, $enc)   # WriteAllText adds NO trailing newline
}

# This script gates on $LASTEXITCODE explicitly and stays on the default Continue: gcloud writes
# ordinary progress to stderr, and under $ErrorActionPreference="Stop" PowerShell would wrap that
# stderr as a terminating NativeCommandError and abort mid-script EVEN ON SUCCESS.

Write-Host "[..] enable_super_admin :: project=$PROJECT region=$REGION platform=$PLATFORM"

# ===================================================================================================
# STEP 1 -- bootstrap super-admin password secret, granted + mounted on the portal.
# ===================================================================================================
gcloud secrets describe $SUPER_SECRET --project=$PROJECT *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[..] creating secret $SUPER_SECRET"

    if ($SuperPw.Trim() -eq "") {
        # Generate a strong random password. NO committed default -- a shipped default would FAIL OPEN
        # as the login fallback (anyone who read the repo would have the god-mode password). We print
        # the generated value ONCE, here, and it is never written to disk except the throwaway temp
        # file below.
        $bytes = New-Object byte[] 24
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        # URL-safe base64 so it pastes cleanly into a browser/login field.
        $SuperPw = [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_').TrimEnd('=')
        Write-Host ""
        Write-Host "    ============================================================" -ForegroundColor Yellow
        Write-Host "    GENERATED SUPER ADMIN PASSWORD (shown ONCE -- save it now):" -ForegroundColor Yellow
        Write-Host "        $SuperPw" -ForegroundColor Yellow
        Write-Host "    ============================================================" -ForegroundColor Yellow
        Write-Host ""
    }

    gcloud secrets create $SUPER_SECRET --project=$PROJECT --replication-policy="automatic" *> $null
    Must "create secret $SUPER_SECRET"

    $tmp = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "$SUPER_SECRET.tmp")
    try {
        Write-SecretFile $tmp $SuperPw
        gcloud secrets versions add $SUPER_SECRET --project=$PROJECT --data-file="$tmp" *> $null
        Must "add first version of $SUPER_SECRET"
    } finally {
        if (Test-Path $tmp) { Remove-Item $tmp -Force }   # never leave secret material on disk
    }
    Write-Host "[OK] secret $SUPER_SECRET created"
} else {
    Write-Host "[OK] secret $SUPER_SECRET already exists (leaving its value untouched)"
}

# Grant the portal web SA read access to the super-admin secret (idempotent).
gcloud secrets add-iam-policy-binding $SUPER_SECRET `
    --project=$PROJECT `
    --member="serviceAccount:$WEB_SA" `
    --role="roles/secretmanager.secretAccessor" *> $null
Must "grant secretAccessor on $SUPER_SECRET to $WEB_SA"
Write-Host "[OK] granted secretAccessor on $SUPER_SECRET to web SA"

# Mount it on the portal and set REGION (the console needs the region to address Cloud Run resources).
gcloud run services update $PLATFORM `
    --project=$PROJECT `
    --region=$REGION `
    --update-secrets "SUPER_ADMIN_PW=$SUPER_SECRET:latest" `
    --update-env-vars "REGION=$REGION" *> $null
Must "mount $SUPER_SECRET on $PLATFORM"
Write-Host "[OK] mounted SUPER_ADMIN_PW on $PLATFORM"

# ===================================================================================================
# STEP 2 -- let the portal console redeploy/rotate dashboards (project-level run.developer).
# ===================================================================================================
gcloud projects add-iam-policy-binding $PROJECT `
    --member="serviceAccount:$WEB_SA" `
    --role="roles/run.developer" *> $null
Must "grant project run.developer to $WEB_SA"
Write-Host "[OK] granted project roles/run.developer to web SA"

# ===================================================================================================
# STEP 3 -- per deployed dashboard: let the portal rotate that client's password and act as the
# dashboard's runtime SA (needed to redeploy it). Client list = the actually-deployed dashboards.
# START with template PLUS status (the status dashboard is operated from the portal too).
# ===================================================================================================
$allServices = (gcloud run services list --project=$PROJECT --region=$REGION --format='value(metadata.name)')
Must "list Cloud Run services"

# Per-client dashboards end in -dash (excluding the portal itself). The status dashboard is
# status-dash and is included here on purpose; platform-dash (the portal) is not a managed target.
$dashServices = @($allServices | Where-Object { $_ -like '*-dash' -and $_ -ne $PLATFORM })

if ($dashServices.Count -eq 0) {
    Write-Host "[OK] no dashboards deployed yet -- skipping per-dashboard grants (start with 'template' + 'status')."
} else {
    foreach ($svc in $dashServices) {
        $c = $svc.Substring(0, $svc.Length - "-dash".Length)
        Write-Host "[..] dashboard '$c' ($svc)"

        # Re-confirm the service is reachable, and read its ACTUAL runtime SA. Names differ per client
        # (and status differs from the <c>-dash-web@ convention), so look it up rather than assuming.
        $sa = (gcloud run services describe $svc --project=$PROJECT --region=$REGION --format='value(spec.template.spec.serviceAccountName)' 2> $null)
        if ($LASTEXITCODE -ne 0) {
            Write-Host "     [WARN] could not describe $svc -- skipping this dashboard" -ForegroundColor Yellow
            continue
        }
        if ([string]::IsNullOrWhiteSpace($sa)) {
            Write-Host "     [WARN] $svc has no runtime SA set -- skipping" -ForegroundColor Yellow
            continue
        }

        # 3a. Let the portal rotate this client's password (add a NEW version; not full admin).
        $pwSecret = "$c-dash-password"
        gcloud secrets describe $pwSecret --project=$PROJECT *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "     [WARN] secret $pwSecret absent -- skipping password-rotation grant" -ForegroundColor Yellow
        } else {
            gcloud secrets add-iam-policy-binding $pwSecret `
                --project=$PROJECT `
                --member="serviceAccount:$WEB_SA" `
                --role="roles/secretmanager.secretVersionAdder" *> $null
            Must "grant secretVersionAdder on $pwSecret to $WEB_SA"
            Write-Host "     [OK] granted secretVersionAdder on $pwSecret"
        }

        # 3b. Let the portal act as the dashboard's runtime SA -- required to redeploy/update it.
        gcloud iam service-accounts add-iam-policy-binding $sa `
            --project=$PROJECT `
            --member="serviceAccount:$WEB_SA" `
            --role="roles/iam.serviceAccountUser" *> $null
        Must "grant serviceAccountUser on $sa to $WEB_SA"
        Write-Host "     [OK] granted serviceAccountUser on $sa"
    }
}

Write-Host "[OK] super admin enabled for $PLATFORM."
