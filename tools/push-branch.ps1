# =============================================================================
# push-branch.ps1 -- commit ALL your local work and push it to THIS machine's
#                    own branch, so it can be merged via a PR.
#
# Each machine gets its own branch so two developers never push to the same one.
# The branch name defaults to this machine's name; override it with your own name
# once (it sticks) so the branch reads like "alex/work" instead of "DESKTOP-AB12/work".
#
#   First time, set your name (writes a gitignored tools/.devname):
#     .\tools\push-branch.ps1 -Dev alex
#   After that, just:
#     .\tools\push-branch.ps1                       # -> alex/work
#     .\tools\push-branch.ps1 -Desc checkout-fix    # -> alex/checkout-fix
#     .\tools\push-branch.ps1 -Message "WIP nav"    # custom commit message
#
# Then open a Pull Request to main on GitHub. CI runs the gates; only green merges.
# To integrate everyone's branches, see tools/merge-branches.ps1 + docs/dev-workflow.md.
# =============================================================================

param(
    [string]$Dev = "",       # your name (slugified). Saved to tools/.devname on first use.
    [string]$Desc = "",      # short description -> the part after the slash (default "work")
    [string]$Message = ""    # commit message (default "WIP from <name>")
)

# Stay on Continue: git writes ordinary progress to stderr, which "Stop" would treat
# as a terminating error even on success. We gate on $LASTEXITCODE via Must.
$ErrorActionPreference = "Continue"
function Die([string]$m) { Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }
function Must([string]$w) { if ($LASTEXITCODE -ne 0) { Die "$w (exit $LASTEXITCODE)" } }

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path   # tools/ -> repo root
Set-Location $repo

# 1. Resolve the owner name: -Dev (and remember it) > tools/.devname > this machine's name.
$devFile = Join-Path $PSScriptRoot ".devname"
if (-not [string]::IsNullOrWhiteSpace($Dev)) {
    Set-Content -Path $devFile -Value $Dev.Trim() -Encoding ascii   # remember for next time
} elseif (Test-Path $devFile) {
    $Dev = (Get-Content $devFile -Raw).Trim()
}
if ([string]::IsNullOrWhiteSpace($Dev)) { $Dev = $env:COMPUTERNAME }

function Slug([string]$s) { return (($s.ToLower() -replace '[^a-z0-9]+', '-').Trim('-')) }
$name = Slug $Dev
if ([string]::IsNullOrWhiteSpace($name)) { Die "could not derive a branch name from '$Dev'" }
$slug = Slug $Desc
if ([string]::IsNullOrWhiteSpace($slug)) { $slug = "work" }
$branch = "$name/$slug"

Write-Host "[push-branch] target branch: $branch" -ForegroundColor Cyan

# 2. Snapshot the current working state onto the branch (create-or-reset to HEAD).
git switch -C $branch
Must "create/switch to $branch"

# 3. Stage everything (new + modified + deleted), including untracked files.
git add -A
Must "git add -A"

# 4. Secret guard (defense in depth -- these are gitignored, but never push them anyway).
$staged = (git diff --cached --name-only) -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$danger = $staged | Where-Object { $_ -match '(\.env$)|(\.p8$)|(\.pem$)|(\.key$)|(credentials.*\.json$)' }
if ($danger) {
    git restore --staged $danger 2>$null
    Die "refusing to commit secret-looking files: $($danger -join ', '). They have been unstaged -- gitignore them."
}

# 5. Commit (skip cleanly if there is nothing to commit; the branch still gets pushed).
if (-not [string]::IsNullOrWhiteSpace((git status --porcelain))) {
    if ([string]::IsNullOrWhiteSpace($Message)) { $Message = "WIP from $name" }
    git commit -m $Message
    Must "commit"
} else {
    Write-Host "[push-branch] nothing new to commit -- pushing the branch as-is." -ForegroundColor Yellow
}

# 6. Prune stale remote-tracking refs first. After a branch is merged-and-deleted on
#    the server, our local origin/<branch> ref lingers; --force-with-lease then leases
#    against that ghost and the push is rejected with "stale info". Pruning clears it so
#    the next push cleanly re-creates the branch. (Non-fatal: offline still pushes below.)
git fetch --prune origin 2>$null

# 7. Push with upstream. --force-with-lease so re-running updates YOUR branch safely
#    (it only overwrites if the remote is where we last saw it -- never clobbers someone else).
git push -u origin $branch --force-with-lease
Must "push $branch"

Write-Host ""
Write-Host "[OK] pushed $branch" -ForegroundColor Green
Write-Host "     Open a Pull Request to main on GitHub -- CI will run the gates before it can merge."
