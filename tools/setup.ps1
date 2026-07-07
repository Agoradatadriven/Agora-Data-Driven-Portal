<#
.SYNOPSIS
    One-time machine setup for the Agora Data Driven monorepo. Idempotent: safe to
    re-run. Installs Python 3.12 and the Google Cloud SDK (via winget) if missing,
    builds the dev .venv, logs in to BOTH gcloud credential systems, and verifies
    that both systems actually work against live resources.
.DESCRIPTION
    Run this once per developer machine. After it succeeds, use start_day.ps1 as a
    ~30s per-session preflight.
#>

$ErrorActionPreference = "Stop"
$PROJECT = "agora-data-driven"

# --- Optional probe targets (parameterized) ---------------------------------
# After verifying BOTH credential systems with resource-agnostic checks, setup does a
# SOFT, informational probe for these two resources. On a fresh/blank GCP project they
# do not exist yet -- that is EXPECTED, so their absence is reported as a note, never a
# failure. They are created later by the ingest standup (the shared secret + raw dataset).
# Adjust these to whatever your FIRST ingest unit actually needs.
$PROBE_SECRET  = "windsor-api-key"   # shared ingest API key (Secret Manager)
$PROBE_DATASET = "raw_windsor"       # shared raw layer (BigQuery)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

function Must([string]$what) {
    # Call AFTER a native command; fails the script if that command returned non-zero.
    if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" }
}

function Update-SessionPath {
    # After a winget install the new exe is on the machine/user PATH in the registry,
    # but THIS already-running shell still has the old PATH. Refresh it in-session by
    # re-reading both the machine and user PATH from the environment so the new tool
    # becomes callable without opening a brand-new terminal.
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Test-Probe([scriptblock]$Probe) {
    # With $ErrorActionPreference = "Stop", redirecting a native command's stderr
    # (2>$null) turns its error output into a terminating NativeCommandError, which
    # would abort the whole script. Test-Probe drops to "Continue" for the probe and
    # reports success purely from the exit code, so an "expected to fail" check (e.g.
    # not-logged-in) falls through to the login step instead of killing the script.
    # Returns $true iff the probe command exits 0, WITHOUT letting its stderr abort the script.
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Probe *> $null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
    finally { $ErrorActionPreference = $old }
}

function Set-GitHubAuth {
    # Configure GitHub auth so `git push` NEVER opens a browser again. Prefer a pasted
    # Personal Access Token (durable, browser-free); fall back to the browser device flow.
    # No-op if already signed in. Manages its own $ErrorActionPreference (this script runs
    # under 'Stop', which would otherwise abort on gh's stderr progress).
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { Write-Host "[!] gh not on PATH yet -- skipping GitHub auth (reopen terminal + re-run)." -ForegroundColor Yellow; return }
        gh auth status *> $null
        if ($LASTEXITCODE -eq 0) { Write-Host "[OK] Already signed in to GitHub." -ForegroundColor Green; gh auth setup-git 2>$null | Out-Null; return }
        Write-Host "GitHub sign-in (so git push never opens a browser). Do ONE of:" -ForegroundColor Cyan
        Write-Host "  - copy your PAT to the clipboard, then press ENTER here to use it, or" -ForegroundColor Gray
        Write-Host "  - paste the PAT on the line below and press Enter (it will be visible), or" -ForegroundColor Gray
        Write-Host "  - type 'b' then Enter to sign in via browser instead." -ForegroundColor Gray
        Write-Host "Create one: https://github.com/settings/tokens  (classic; scopes: repo, workflow, read:org)" -ForegroundColor DarkGray
        $tok = Read-Host "PAT (or ENTER=use clipboard, b=browser)"
        if ($tok -match '^(b|browser)$') { gh auth login --web --git-protocol https }
        else {
            if ([string]::IsNullOrWhiteSpace($tok)) {
                try { $tok = (Get-Clipboard -Raw 2>$null | Out-String) } catch { $tok = "" }
                if (-not [string]::IsNullOrWhiteSpace($tok)) { Write-Host "[OK] Using the token from your clipboard." -ForegroundColor Green }
            }
            $tok = ($tok -replace '\s', '')
            if ([string]::IsNullOrWhiteSpace($tok)) { Write-Host "[!] No token found (clipboard empty?). Falling back to browser." -ForegroundColor Yellow; gh auth login --web --git-protocol https }
            else {
                $tok | gh auth login --with-token
                if ($LASTEXITCODE -ne 0) { Write-Host "[!] Token rejected (needs scopes: repo, workflow, read:org). Falling back to browser." -ForegroundColor Yellow; gh auth login --web --git-protocol https }
            }
        }
        $tok = $null
        gh auth setup-git 2>$null | Out-Null
        gh auth status *> $null
        if ($LASTEXITCODE -eq 0) { Write-Host "[OK] GitHub configured -- git push will not prompt." -ForegroundColor Green } else { Write-Host "[!] GitHub auth not confirmed -- pushes may still prompt." -ForegroundColor Yellow }
    } finally { $ErrorActionPreference = $old }
}

# ---------------------------------------------------------------------------
# (a) Locate + sanity-check the repo root
# ---------------------------------------------------------------------------
Write-Host "[..] Locating repo root" -ForegroundColor Cyan
$REPO = Split-Path -Parent $PSScriptRoot   # tools/ -> repo root
if (-not $REPO) { Die "Could not resolve repo root from PSScriptRoot ($PSScriptRoot)" }

$rootReq      = Join-Path $REPO "requirements.txt"
$clientsDir   = Join-Path $REPO "clients"
if (-not (Test-Path $rootReq))    { Die "Not the Agora Data Driven repo: missing requirements.txt at $rootReq" }
if (-not (Test-Path $clientsDir)) { Die "Not the Agora Data Driven repo: missing clients/ folder at $clientsDir" }
Write-Host "[OK] Repo root: $REPO" -ForegroundColor Green

# ---------------------------------------------------------------------------
# (b) Python 3.12
# ---------------------------------------------------------------------------
Write-Host "[..] Checking for Python" -ForegroundColor Cyan
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[..] Python not found -- installing Python 3.12 via winget" -ForegroundColor Cyan
    winget install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements
    Must "winget install Python.Python.3.12"
    Update-SessionPath
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Write-Host "[..] Python was installed but is not visible in THIS terminal." -ForegroundColor Yellow
        Write-Host "     Please open a NEW terminal and re-run setup." -ForegroundColor Yellow
        exit 0
    }
}
Write-Host "[OK] Python: $((python --version) 2>&1)" -ForegroundColor Green

# ---------------------------------------------------------------------------
# (c) Google Cloud SDK
# ---------------------------------------------------------------------------
Write-Host "[..] Checking for gcloud" -ForegroundColor Cyan
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Host "[..] gcloud not found -- installing Google Cloud SDK via winget" -ForegroundColor Cyan
    winget install --id Google.CloudSDK --exact --silent --accept-package-agreements --accept-source-agreements
    Must "winget install Google.CloudSDK"
    Update-SessionPath
    if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
        Write-Host "[..] Google Cloud SDK was installed but is not visible in THIS terminal." -ForegroundColor Yellow
        Write-Host "     Please open a NEW terminal and re-run setup." -ForegroundColor Yellow
        exit 0
    }
}
Write-Host "[OK] gcloud is on PATH" -ForegroundColor Green

# ---------------------------------------------------------------------------
# (c2) Git -- needed to clone/branch/push (and by tools/push-branch.ps1 etc.)
# ---------------------------------------------------------------------------
Write-Host "[..] Checking for git" -ForegroundColor Cyan
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "[..] git not found -- installing Git via winget" -ForegroundColor Cyan
    winget install --id Git.Git --exact --silent --accept-package-agreements --accept-source-agreements
    Must "winget install Git.Git"
    Update-SessionPath
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "[..] git installed but not visible in THIS terminal. Open a NEW terminal and re-run setup." -ForegroundColor Yellow
        exit 0
    }
}
Write-Host "[OK] git: $((git --version) 2>&1)" -ForegroundColor Green

# ---------------------------------------------------------------------------
# (c3) GitHub CLI -- so `git push` over HTTPS uses a token (no browser prompts)
# ---------------------------------------------------------------------------
Write-Host "[..] Checking for gh (GitHub CLI)" -ForegroundColor Cyan
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "[..] gh not found -- installing GitHub CLI via winget" -ForegroundColor Cyan
    winget install --id GitHub.cli --exact --silent --accept-package-agreements --accept-source-agreements
    Must "winget install GitHub.cli"
    Update-SessionPath
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Host "[..] gh installed but not visible in THIS terminal. Open a NEW terminal and re-run setup." -ForegroundColor Yellow
        exit 0
    }
}
Write-Host "[OK] gh is on PATH" -ForegroundColor Green

# ---------------------------------------------------------------------------
# (d) Verify the committed requirements files EXIST before using them
# ---------------------------------------------------------------------------
Write-Host "[..] Verifying version-controlled requirements files" -ForegroundColor Cyan
$jobReq = Join-Path $REPO "clients/client_template/job/requirements.txt"
if (-not (Test-Path $rootReq)) { Die "Missing version-controlled file: $rootReq" }
if (-not (Test-Path $jobReq))  { Die "Missing version-controlled file: $jobReq" }
Write-Host "[OK] Found root requirements.txt and clients/client_template/job/requirements.txt" -ForegroundColor Green

# ---------------------------------------------------------------------------
# (e) Resolve the target venv, then pip install BOTH requirements files
# ---------------------------------------------------------------------------
# All Agora Python repos share ONE venv at <workspace-root>\.venv (created by bootstrap.ps1
# / agora-start-day.ps1). Prefer it when present; fall back to a repo-local .venv only when
# this repo was cloned STANDALONE (no sibling agora-devtools, so no shared venv). Either way
# the dev venv is a SUPERSET -- it installs the root requirements.txt (loaders + setup
# scripts) AND the template client's job/requirements.txt (export job) because they pin
# compatible google-cloud-* versions; the dash web app is deliberately EXCLUDED because it
# can pin a conflicting google-cloud-storage; each Cloud Run unit still builds its own
# container, so this local venv never affects image builds.
$sharedPy = Join-Path (Split-Path $REPO -Parent) ".venv/Scripts/python.exe"
if (Test-Path $sharedPy) {
    $venvPy = $sharedPy
    Write-Host "[OK] Using shared workspace venv: $venvPy" -ForegroundColor Green
} else {
    $venvDir = Join-Path $REPO ".venv"
    $venvPy  = Join-Path $venvDir "Scripts/python.exe"
    if (-not (Test-Path $venvPy)) {
        Write-Host "[..] Creating repo-local .venv (no shared venv found)" -ForegroundColor Cyan
        python -m venv $venvDir
        Must "python -m venv .venv"
    } else {
        Write-Host "[OK] Repo-local .venv already exists" -ForegroundColor Green
    }
}
# A pre-existing venv can be missing pip entirely (e.g. created with --without-pip,
# or with a base Python whose ensurepip was unavailable at creation time). `python.exe`
# exists, so the Test-Path check above skips recreation -- but `-m pip` then dies with
# "No module named pip". Bootstrap pip via ensurepip first if it is absent, so the venv
# self-heals instead of failing setup.
if (-not (Test-Probe { & $venvPy -m pip --version })) {
    Write-Host "[..] pip missing in venv -- bootstrapping via ensurepip" -ForegroundColor Yellow
    & $venvPy -m ensurepip --upgrade
    Must "ensurepip bootstrap"
}
Write-Host "[..] Upgrading pip" -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip
Must "pip upgrade"
Write-Host "[..] pip install -r requirements.txt (root)" -ForegroundColor Cyan
& $venvPy -m pip install -r $rootReq
Must "pip install root requirements.txt"
Write-Host "[..] pip install -r clients/client_template/job/requirements.txt" -ForegroundColor Cyan
& $venvPy -m pip install -r $jobReq
Must "pip install job requirements.txt"
Write-Host "[OK] venv ready (root + template job requirements)" -ForegroundColor Green

# ---------------------------------------------------------------------------
# (f) Log in to gcloud TWICE -- CLI creds AND Application Default Credentials
# ---------------------------------------------------------------------------
# gcloud keeps two independent logins and the org enforces periodic reauth on each, so
# either can expire without the other:
#   - CLI creds (used by `gcloud secrets ...`, refreshed via `gcloud auth login`)
#   - Application Default Credentials / ADC (used by the Python client libraries --
#     google-cloud-bigquery / -storage / -secret-manager -- refreshed via
#     `gcloud auth application-default login`).
# Test-Probe first so we only trigger a browser login when the existing creds are
# actually expired/absent.
Write-Host "[..] Checking gcloud CLI credentials" -ForegroundColor Cyan
if (Test-Probe { gcloud auth print-access-token }) {
    Write-Host "[OK] CLI credentials already valid" -ForegroundColor Green
} else {
    Write-Host "[..] CLI login required -- launching browser" -ForegroundColor Cyan
    gcloud auth login
    Must "gcloud auth login"
}

Write-Host "[..] Checking Application Default Credentials (ADC)" -ForegroundColor Cyan
if (Test-Probe { gcloud auth application-default print-access-token }) {
    Write-Host "[OK] ADC already valid" -ForegroundColor Green
} else {
    Write-Host "[..] ADC login required -- launching browser" -ForegroundColor Cyan
    gcloud auth application-default login
    Must "gcloud auth application-default login"
}

Write-Host "[..] Pinning project and ADC quota project to $PROJECT" -ForegroundColor Cyan
gcloud config set project $PROJECT
Must "gcloud config set project"
gcloud auth application-default set-quota-project $PROJECT
Must "gcloud auth application-default set-quota-project"
Write-Host "[OK] Project pinned to $PROJECT" -ForegroundColor Green

# ---------------------------------------------------------------------------
# (f2) GitHub sign-in (PAT preferred -> browser-free pushes)
# ---------------------------------------------------------------------------
Set-GitHubAuth

# ---------------------------------------------------------------------------
# (g) Final verification -- prove BOTH credential systems work.
# ---------------------------------------------------------------------------
# This runs as the FIRST thing on a new machine, possibly against a BLANK GCP project,
# so it must NOT require any resource to pre-exist. We verify the two credential systems
# with RESOURCE-AGNOSTIC checks, then SOFT-probe the configured secret/dataset (absent ==
# fine on a fresh project). A `NOT_FOUND` only proves auth works and the resource is not
# created yet -- it is never a setup failure.

# CLI creds: describing the project needs valid CLI creds + project access and works on
# an empty project.
Write-Host "[..] Verifying CLI credentials (describe project $PROJECT)" -ForegroundColor Cyan
$null = gcloud projects describe $PROJECT --format='value(projectId)'
Must "describe project $PROJECT (CLI credentials)"
Write-Host "[OK] CLI credentials work" -ForegroundColor Green

# ADC: mint a token via the Python client libraries' credential path. This is
# API-agnostic, so it works before any API is enabled or any dataset/secret exists.
Write-Host "[..] Verifying Application Default Credentials (Python libraries)" -ForegroundColor Cyan
& $venvPy -c @"
import google.auth
from google.auth.transport.requests import Request
creds, _ = google.auth.default()
creds.refresh(Request())
assert creds.valid
print('ok')
"@
Must "mint an ADC token (ADC / Python client libraries)"
Write-Host "[OK] ADC works (the Python client libraries can authenticate)" -ForegroundColor Green

# Soft, informational probe: do the configured ingest secret + raw dataset exist YET?
# On a fresh project they will not -- that is expected; they are created later by the
# ingest standup. Test-Probe swallows the stderr/exit so a NOT_FOUND never aborts setup.
Write-Host "[..] Checking optional probe targets (informational only)" -ForegroundColor Cyan
if (Test-Probe { gcloud secrets describe $PROBE_SECRET --project=$PROJECT }) {
    Write-Host "[OK] secret '$PROBE_SECRET' exists" -ForegroundColor Green
} else {
    Write-Host "[..] secret '$PROBE_SECRET' not created yet -- expected on a fresh project (create it when you wire up Windsor ingest)." -ForegroundColor Yellow
}
if (Test-Probe { & $venvPy -c "from google.cloud import bigquery; bigquery.Client(project='$PROJECT').get_dataset('$PROJECT.$PROBE_DATASET')" }) {
    Write-Host "[OK] dataset '$PROBE_DATASET' exists" -ForegroundColor Green
} else {
    Write-Host "[..] dataset '$PROBE_DATASET' not created yet -- expected on a fresh project (created by the Windsor ingest standup)." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[OK] Setup complete. Both credential systems verified." -ForegroundColor Green
Write-Host "     (Any '[..] not created yet' notes above are normal on a blank project.)" -ForegroundColor Green
Write-Host "     Next: run start_day.ps1 at the start of each work session." -ForegroundColor Green
