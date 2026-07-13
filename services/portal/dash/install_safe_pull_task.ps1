# Registers the "Agora Watcher Safe Pull" scheduled task on THIS machine: every 5 minutes it
# runs safe_pull_agent.vbs (hidden), which serves the Watcher tab's Safe-pull queue with the
# slow home-IP scraper (safe_scrape_local.py --queue). Idempotent: re-running replaces the task.
#
# Requirements on the machine: gcloud authed with bucket access, and the repo's atrium
# .venv-portal (youtube-transcript-api installed). No admin rights needed (per-user task).
#
# Remove with: Unregister-ScheduledTask -TaskName "Agora Watcher Safe Pull" -Confirm:$false

$vbs = Join-Path $PSScriptRoot "safe_pull_agent.vbs"
if (-not (Test-Path $vbs)) { throw "safe_pull_agent.vbs not found next to this script." }

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B //Nologo `"$vbs`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
# No overlap (queue scrapes can run for hours) and no time limit; catch up after sleep/reboot.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable

Register-ScheduledTask -TaskName "Agora Watcher Safe Pull" -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null

Write-Host "[OK] 'Agora Watcher Safe Pull' registered: every 5 minutes, hidden, single-instance."
Write-Host "     Queue log: $env:TEMP\watcher_safe_scrape\agent.log"
