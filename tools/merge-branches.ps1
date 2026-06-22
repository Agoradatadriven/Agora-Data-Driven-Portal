# =============================================================================
# merge-branches.ps1 -- SAFELY integrate the per-machine dev branches into main.
#
# This is the HYBRID merge tool (see docs/dev-workflow.md). It automates the safe,
# mechanical path and HANDS OFF to a human (you + Claude) the moment judgment is
# needed. Deliberately NOT a "merge everything and delete" button -- a real conflict
# (two devs editing the same screen) needs a person, and a blind script would ship a
# broken merge or delete unmerged work. This session is the cautionary tale.
#
# What it does:
#   0. if your working tree has local changes, commit + push them to THIS machine's
#      own dev branch first (delegates to push-branch.ps1) -- otherwise the dirty tree
#      blocks the integration checkout AND your work would be left out of the merge
#   1. fetch + discover every per-machine branch on origin (everything except main)
#   2. create a throwaway integration branch off origin/main
#   3. merge each branch in turn -- on the FIRST conflict it aborts that merge and
#      STOPS, telling you to resolve it (ask Claude -- it handles the semantic ones)
#   4. run the CI tests locally against the integrated result; STOP if anything is red
#   5. it does NOT push to main and does NOT delete anything. It prints the exact
#      commands to land it after you've eyeballed the diff.
#
# Branch cleanup is a SEPARATE, GATED step: -DeleteMerged deletes ONLY the remote
# branches whose commits are already contained in origin/main (so it can never drop
# unmerged work). Run it after you've landed the integration branch.
#
# USAGE
#   .\tools\merge-branches.ps1                     # integrate + test, then stop for review
#   .\tools\merge-branches.ps1 -Exclude alex/wip   # skip specific branches (comma-sep)
#   .\tools\merge-branches.ps1 -DeleteMerged       # prune remote branches already in main
# =============================================================================

param(
    [string]$Exclude = "",
    [switch]$DeleteMerged
)

$ErrorActionPreference = "Continue"
function Die([string]$m) { Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }
function Must([string]$w) { if ($LASTEXITCODE -ne 0) { Die "$w (exit $LASTEXITCODE)" } }

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path   # tools/ -> repo root
Set-Location $repo

# 0. Capture any local working changes BEFORE we touch branches. A dirty tree both
#    blocks the integration checkout below AND means this machine has unpushed work --
#    so commit + push it to this machine's own dev branch first (delegates to
#    push-branch.ps1: add -A -> secret guard -> commit -> push). It then gets discovered
#    and integrated in this same run. Skipped for the prune-only -DeleteMerged path.
if (-not $DeleteMerged -and -not [string]::IsNullOrWhiteSpace((git status --porcelain))) {
    Write-Host "[..] Local changes detected -- committing + pushing them to your branch first" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "push-branch.ps1")
    Must "push-branch (commit + push local changes)"
    Write-Host "[OK] local work pushed -- it will be integrated below" -ForegroundColor Green
}

# 1. Fresh view of remotes, then discover the dev branches (origin/* minus main/HEAD).
Write-Host "[..] Fetching origin" -ForegroundColor Cyan
git fetch origin --prune
Must "git fetch"

$skip = @("main", "HEAD") + (($Exclude -split ',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$branches = git branch -r --format='%(refname:short)' |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -like 'origin/*' } |
    ForEach-Object { $_ -replace '^origin/', '' } |
    Where-Object { $_ -and ($skip -notcontains $_) }

if (-not $branches) { Write-Host "[OK] no dev branches to merge -- main is already current." -ForegroundColor Green; exit 0 }
Write-Host "[OK] branches to integrate: $($branches -join ', ')"

# -DeleteMerged is a standalone, GATED cleanup -- it never runs the merge.
if ($DeleteMerged) {
    Write-Host "[..] Deleting remote branches already contained in origin/main" -ForegroundColor Cyan
    $alreadyMerged = git branch -r --merged origin/main --format='%(refname:short)' |
        ForEach-Object { ($_ -replace '^origin/', '').Trim() } |
        Where-Object { $_ -and ($skip -notcontains $_) }
    if (-not $alreadyMerged) { Write-Host "    (none are fully merged into main yet -- nothing to delete)" -ForegroundColor Yellow; exit 0 }
    foreach ($b in $alreadyMerged) {
        Write-Host "    deleting origin/$b (its commits are in main)" -ForegroundColor Yellow
        git push origin --delete $b; Must "delete origin/$b"
    }
    Write-Host "[OK] pruned: $($alreadyMerged -join ', ')" -ForegroundColor Green
    exit 0
}

# 2. Throwaway integration branch off the CURRENT origin/main.
$intg = "integration/merge"
Write-Host "[..] Creating $intg off origin/main" -ForegroundColor Cyan
git switch -C $intg origin/main
Must "create $intg"

# 3. Merge each branch; STOP on the first conflict (hand off to a human).
$merged = @()
foreach ($b in $branches) {
    Write-Host "[..] Merging $b" -ForegroundColor Cyan
    git merge --no-ff -m "Merge $b into $intg" "origin/$b"
    if ($LASTEXITCODE -ne 0) {
        git merge --abort
        Write-Host ""
        Write-Host "[CONFLICT] $b does not merge cleanly -- aborted that merge." -ForegroundColor Red
        Write-Host "  Already integrated cleanly: $($merged -join ', ')" -ForegroundColor Yellow
        Write-Host "  Resolve the conflicting branch with Claude (it handles the semantic ones), then re-run."
        Write-Host "  The $intg branch holds the clean merges so far."
        exit 1
    }
    $merged += $b
}
Write-Host "[OK] all branches merged cleanly: $($merged -join ', ')" -ForegroundColor Green

# 4. Run the CI tests locally before trusting the integrated result.
Write-Host "[..] Running the off-cloud CI tests against the integrated tree" -ForegroundColor Cyan
$py = Join-Path $repo ".venv-portal\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = Join-Path $repo ".venv\Scripts\python.exe" }
if (-not (Test-Path $py)) { Die "no python venv found (.venv-portal or .venv). Run tools\setup.ps1 / preview once." }

Push-Location (Join-Path $repo "services\portal\dash")
& $py _workspace_localtest.py | Out-Null; $t1 = $LASTEXITCODE
& $py _atrium_smoketest.py    | Out-Null; $t2 = $LASTEXITCODE
Pop-Location
if ($t1 -ne 0 -or $t2 -ne 0) {
    Die "integration tests FAILED (workspace=$t1 smoke=$t2) -- do NOT land this. The $intg branch holds the result; inspect it / ask Claude."
}
Write-Host "[OK] integration tests green" -ForegroundColor Green

# 5. Stop here -- never auto-push main. Print the exact land + cleanup steps.
Write-Host ""
Write-Host "[OK] $intg is clean + green. Review, then land it:" -ForegroundColor Green
Write-Host "     git log --oneline origin/main..$intg          # what's about to land"
Write-Host "     git switch main; git merge --ff-only $intg; git push origin main"
Write-Host "     .\tools\merge-branches.ps1 -DeleteMerged       # prune the branches now in main"
