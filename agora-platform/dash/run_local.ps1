# Run the portal LOCALLY for click-through testing -- no GCP, no ADC, no deploy.
#
# It stands up an isolated venv (.venv-portal) with just Flask + requests, points the data layer at
# a throwaway local folder (.local_portal_data), seeds a demo portal you can log into, and serves it
# at http://localhost:8080. The production .venv is deliberately left untouched (it excludes the web
# deps on purpose -- see agora-platform/dash/requirements.txt).
#
#   From anywhere:  agora-platform\dash\run_local.ps1
#
# Stop with Ctrl+C. Delete .local_portal_data to reset the demo. This NEVER touches the real bucket.

$ErrorActionPreference = "Stop"

$dash = $PSScriptRoot
$repo = (Resolve-Path (Join-Path $dash "..\..")).Path

# 1. An isolated local-run venv (bootstrapped from the repo venv we know exists).
$venv = Join-Path $repo ".venv-portal"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[run_local] creating local-run venv at .venv-portal ..."
    $bootstrap = Join-Path $repo ".venv\Scripts\python.exe"
    if (-not (Test-Path $bootstrap)) { $bootstrap = "python" }
    & $bootstrap -m venv $venv
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet "Flask==3.0.3" "requests==2.34.2"
}

# 2. Point the data layer at a throwaway local folder (both registry and workspaces).
$data = Join-Path $repo ".local_portal_data"
New-Item -ItemType Directory -Force -Path $data | Out-Null
$env:REGISTRY_LOCAL_DIR = $data
$env:WORKSPACE_LOCAL_DIR = $data

# 3. Local-dev app config: a dummy session secret, and relaxed cookies so login works over http.
$env:SESSION_SECRET = "local-dev-secret-not-for-production"
$env:PORTAL_SECURE_COOKIES = "0"
$env:PORT = "8080"

# 4. Seed the demo clients (idempotent) and print the logins.
& $py (Join-Path $dash "seed_local.py")

Write-Host ""
Write-Host "[run_local] starting portal at http://localhost:8080  (Ctrl+C to stop)" -ForegroundColor Green
Write-Host ""

# 5. Serve.
& $py (Join-Path $dash "main.py")
