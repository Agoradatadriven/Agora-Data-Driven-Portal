# Give the Atrium Assistant read access to every client's dashboard data export (OPT-IN, idempotent).
#
# The Assistant (assistant_ai.read_client_dash_data) tries to read `<c>.json` from the client's
# dash bucket `agora-data-driven-<c>-dash`; by default only that client's own SAs can read it, so
# the source is silently skipped. Running this once grants the PORTAL runtime SA
# (platform-dash-web@) roles/storage.objectViewer on each existing client dash bucket -- read-only,
# reversible (remove the binding to opt back out). New clients need a re-run (or add the grant to
# the client standup).
#
#   .\enable_assistant_dash_data.ps1

$ErrorActionPreference = "Stop"
$PROJECT = "agora-data-driven"
$WEB_SA = "platform-dash-web@$PROJECT.iam.gserviceaccount.com"

Write-Host "[..] Finding client dash buckets in $PROJECT" -ForegroundColor Cyan
$buckets = gcloud storage buckets list --project=$PROJECT --format="value(name)" |
    Where-Object { $_ -match "^$PROJECT-.+-dash$" -and $_ -ne "$PROJECT-platform-dash" }

if (-not $buckets) {
    Write-Host "[..] No client dash buckets found -- nothing to grant." -ForegroundColor Yellow
    exit 0
}

foreach ($b in $buckets) {
    Write-Host "[..] Granting objectViewer on gs://$b to $WEB_SA" -ForegroundColor Cyan
    gcloud storage buckets add-iam-policy-binding "gs://$b" `
        --member="serviceAccount:$WEB_SA" --role="roles/storage.objectViewer" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "[ERROR] grant failed for $b" -ForegroundColor Red; exit 1 }
    Write-Host "[OK] $b" -ForegroundColor Green
}
Write-Host "[OK] The Assistant can now read every client's dashboard data export." -ForegroundColor Green
