# tcs_provision_secrets.ps1 -- provision the direct-API secrets the TCS ingest loaders read.
#
# The TCS loaders (tcs_shopify / tcs_klaviyo) read their OWN key from Secret Manager BY ID via ADC
# (mirroring services/ingest/ga4/ga4_loader.py, which reads windsor-api-key). This script creates
# (or adds a new version to) those secrets and grants the shared ingest runtime SA read access.
# The quiz loader needs NO secret -- it reads the Google Sheet via ADC (share the sheet with the
# ingest SA instead; see the note at the end).
#
# Secret material is written through a UTF-8 (no BOM, no trailing newline) temp file, per the repo
# rule: a BOM or trailing newline would become part of the secret and silently break auth.
#
# Usage (values are prompted if omitted so they never land in shell history):
#   .\services\ingest\tcs_provision_secrets.ps1
#   .\services\ingest\tcs_provision_secrets.ps1 -ShopifyToken "shpat_..." -KlaviyoKey "pk_..."

[CmdletBinding()]
param(
  [string]$ShopifyToken,
  [string]$KlaviyoKey
)

$ErrorActionPreference = "Continue"

$PROJECT   = "agora-data-driven"
$INGEST_SA = "ingest-runner@agora-data-driven.iam.gserviceaccount.com"

function Must($label) {
  if ($LASTEXITCODE -ne 0) { Write-Host "[ERR] $label (exit $LASTEXITCODE)" -ForegroundColor Red; exit 1 }
}

# Write secret material as UTF-8 with NO BOM and NO trailing newline, to a GUID temp file.
function Write-SecretFile([string]$value) {
  $path = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString() + ".txt")
  $enc = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($path, $value, $enc)
  return $path
}

function Set-Secret([string]$name, [string]$value) {
  if ([string]::IsNullOrWhiteSpace($value)) {
    Write-Host "[--] $name skipped (no value provided)." -ForegroundColor Yellow
    return
  }
  $file = Write-SecretFile $value
  try {
    gcloud secrets describe $name --project $PROJECT *> $null
    if ($LASTEXITCODE -eq 0) {
      Write-Host "[..] $name exists -> adding new version" -ForegroundColor Cyan
      gcloud secrets versions add $name --project $PROJECT --data-file="$file" | Out-Null
      Must "add version $name"
    } else {
      Write-Host "[..] creating secret $name" -ForegroundColor Cyan
      gcloud secrets create $name --project $PROJECT --replication-policy=automatic --data-file="$file" | Out-Null
      Must "create secret $name"
    }
    # Grant the ingest runtime SA read access (idempotent).
    gcloud secrets add-iam-policy-binding $name --project $PROJECT `
      --member "serviceAccount:$INGEST_SA" `
      --role "roles/secretmanager.secretAccessor" | Out-Null
    Must "grant secretAccessor on $name"
    Write-Host "[OK] $name provisioned + granted to $INGEST_SA" -ForegroundColor Green
  }
  finally {
    Remove-Item $file -Force -ErrorAction SilentlyContinue
  }
}

if (-not $ShopifyToken) { $ShopifyToken = Read-Host "Shopify Admin API token (tcs-shopify-token), blank to skip" }
if (-not $KlaviyoKey)   { $KlaviyoKey   = Read-Host "Klaviyo private API key (tcs-klaviyo-key), blank to skip" }

Set-Secret "tcs-shopify-token" $ShopifyToken
Set-Secret "tcs-klaviyo-key"   $KlaviyoKey

Write-Host ""
Write-Host "Quiz loader uses ADC Google Sheets access -- no secret needed. Instead:" -ForegroundColor Yellow
Write-Host "  1) Enable the Sheets + Drive APIs on $PROJECT." -ForegroundColor Yellow
Write-Host "  2) Share the Business-Quiz sheet (Viewer) with: $INGEST_SA" -ForegroundColor Yellow
Write-Host "[done] TCS ingest secrets provisioned." -ForegroundColor Green
