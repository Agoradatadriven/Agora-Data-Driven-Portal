<#
  glm-bypass-mode.ps1  -  launch Claude Code on Z.ai GLM (shared org key)
  -----------------------------------------------------------------------
  Claude Code normally talks to the Anthropic API. This launcher re-points it at
  Z.ai's Anthropic-compatible endpoint and the GLM model family, using ONE org
  key from Secret Manager (glm-api-key) so every dev runs the SAME setup without
  a per-machine key.

  This script is REPO-AGNOSTIC: it does NOT change directory, so it launches
  Claude in whatever folder you are standing in (any of the Agora repos, or the
  agora-devtools repo). That's why an identical copy lives in every repo's
  scripts/ (atrium: tools/) AND in agora-devtools/ -- run whichever is handy.

  It sets these ONLY for the Claude process and restores them afterwards, so the
  token never lingers in your shell after Claude exits:
    ANTHROPIC_BASE_URL                  https://api.z.ai/api/anthropic
    ANTHROPIC_AUTH_TOKEN                (from Secret Manager; never printed)
    ANTHROPIC_DEFAULT_OPUS_MODEL        glm-5.2
    ANTHROPIC_DEFAULT_SONNET_MODEL      glm-5.2
    ANTHROPIC_DEFAULT_HAIKU_MODEL       glm-4.7

  Run:
    .\scripts\glm-bypass-mode.ps1              # launch in THIS terminal (current folder)
    .\scripts\glm-bypass-mode.ps1 -NewWindow   # launch in a fresh window
    .\scripts\glm-bypass-mode.cmd              # double-click = fresh window
    .\scripts\glm-bypass-mode.ps1 --resume <id>  # any extra args pass straight to claude
    .\scripts\glm-bypass-mode.ps1 -Dir ..\website  # launch Claude in another repo

  Prereqs: `claude` on PATH + gcloud logged in with access to the glm-api-key
  secret. Each repo's setup.ps1 installs claude guidance; start_day.ps1 checks
  the secret. If the secret does not exist yet in agora-data-driven, either
  create it there or point this at another project:
    .\scripts\glm-bypass-mode.ps1 -Project bidbrain-analytics
#>

[CmdletBinding()]
param(
    [string]$Project = "agora-data-driven",
    [string]$Secret  = "glm-api-key",
    [string]$Dir      = "",     # optional: launch Claude in this folder instead of the current one
    [switch]$NewWindow,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ClaudeArgs
)

$ErrorActionPreference = "Stop"

# Optional: hop into another folder before launching (e.g. -Dir ..\website).
if ($Dir) {
    if (-not (Test-Path $Dir)) { Write-Host "[X] -Dir '$Dir' does not exist." -ForegroundColor Red; exit 1 }
    Set-Location $Dir
}

# --- 1. claude CLI present? ---------------------------------------------------
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "[X] 'claude' not found on PATH." -ForegroundColor Red
    Write-Host "    Install Claude Code:  npm install -g @anthropic-ai/claude-code" -ForegroundColor Yellow
    Write-Host "    (or the native installer from https://claude.ai/code)" -ForegroundColor Yellow
    exit 1
}

# --- 2. -NewWindow: spawn a fresh terminal that re-runs THIS script inline -----
# Re-fetching the secret inside the child keeps the token out of any command-line
# arg or window title. The child launches WITHOUT -NewWindow, so no recursion, and
# in the SAME working directory so it opens the same repo.
if ($NewWindow) {
    $psExe = (Get-Process -Id $PID).Path      # powershell.exe / pwsh.exe
    $self  = $MyInvocation.MyCommand.Path
    Start-Process -FilePath $psExe `
        -WorkingDirectory (Get-Location).Path `
        -ArgumentList @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $self, "-Project", $Project, "-Secret", $Secret)
    return
}

# --- 3. fetch the key from Secret Manager (never print it) --------------------
Write-Host "[*] Reading $Secret from Secret Manager (project $Project)..." -ForegroundColor Yellow
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Host "[X] gcloud not found on PATH. Run this repo's setup.ps1 first." -ForegroundColor Red
    exit 1
}
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$tok = $null
try {
    $tok = (gcloud secrets versions access latest --secret $Secret --project $Project 2>$null)
} catch {}
$ErrorActionPreference = $prevEAP
if (-not $tok) {
    Write-Host "[X] Could not read '$Secret' from project '$Project'." -ForegroundColor Red
    Write-Host "    Likely causes + fixes:" -ForegroundColor Yellow
    Write-Host "      * Not logged in       ->  gcloud auth login" -ForegroundColor Yellow
    Write-Host "      * Secret not created  ->  create it in $Project, e.g.:" -ForegroundColor Yellow
    Write-Host "          `"<your-z.ai-key>`" | gcloud secrets create $Secret --data-file=- --project $Project" -ForegroundColor Yellow
    Write-Host "      * No IAM access       ->  gcloud secrets add-iam-policy-binding $Secret ``" -ForegroundColor Yellow
    Write-Host "          --member=user:<you> --role=roles/secretmanager.secretAccessor --project $Project" -ForegroundColor Yellow
    Write-Host "      * Use another project ->  .\glm-bypass-mode.ps1 -Project bidbrain-analytics" -ForegroundColor Yellow
    exit 1
}

# --- 4. set env for the Claude process (snapshot prior values to restore) ------
$vars = [ordered]@{
    ANTHROPIC_BASE_URL             = "https://api.z.ai/api/anthropic"
    ANTHROPIC_AUTH_TOKEN           = $tok
    ANTHROPIC_DEFAULT_OPUS_MODEL   = "glm-5.2"
    ANTHROPIC_DEFAULT_SONNET_MODEL = "glm-5.2"
    ANTHROPIC_DEFAULT_HAIKU_MODEL  = "glm-4.7"
}
$prior = @{}
foreach ($k in $vars.Keys) { $prior[$k] = [Environment]::GetEnvironmentVariable($k, "Process") }
foreach ($k in $vars.Keys) { [Environment]::SetEnvironmentVariable($k, $vars[$k], "Process") }

Write-Host "[OK] Launching Claude Code on GLM 5.2 (Z.ai) in $((Get-Location).Path)..." -ForegroundColor Green
Write-Host "     endpoint https://api.z.ai/api/anthropic  |  opus/sonnet=glm-5.2  haiku=glm-4.7" -ForegroundColor DarkGray
try {
    & claude @ClaudeArgs
}
finally {
    # Restore prior env so the token does not outlive this script.
    foreach ($k in $vars.Keys) {
        [Environment]::SetEnvironmentVariable($k, $prior[$k], "Process")
    }
}
