# Turn ON the Atrium Assistant's cross-encoder RERANKING (OPT-IN, idempotent).
#
# The Assistant's retrieval is HYBRID by default (BM25 keyword + text-embedding-005 semantic, fused
# with Reciprocal Rank Fusion) -- that needs no new infra. This script enables the ONE extra piece:
# a cross-encoder rerank pass over the fused candidate pool using the Vertex/Discovery Engine Ranking
# API (model semantic-ranker-fast-004), which sorts "retrieve 50" down to the truly-relevant few
# before they reach the model.
#
# It (a) enables discoveryengine.googleapis.com and (b) grants the portal runtime SA
# (platform-dash-web@) roles/discoveryengine.user so it can call the ranking config. After running
# this once, the NEXT deploy_dash_platform.ps1 detects the API is enabled and flips
# ASSISTANT_RERANK_ENABLED=1 automatically. Reversible: disable the API / remove the role to opt back
# out (the Assistant then degrades to hybrid-without-rerank -- no code change needed).
#
#   .\enable_assistant_reranking.ps1        # then re-run .\deploy_dash_platform.ps1
#
# NOTE ON COST: the Ranking API is billed per query (fixed, not per-token) and is free up to 300 QPM
# during preview -- comfortably inside the Assistant's team-only usage.

$ErrorActionPreference = "Continue"   # gcloud writes progress to stderr; gate on $LASTEXITCODE
$PROJECT = "agora-data-driven"
$WEB_SA = "platform-dash-web@$PROJECT.iam.gserviceaccount.com"

function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

Write-Host "[..] Enabling discoveryengine.googleapis.com (the Ranking API)" -ForegroundColor Cyan
gcloud services enable discoveryengine.googleapis.com --project=$PROJECT
if ($LASTEXITCODE -ne 0) { Die "could not enable discoveryengine.googleapis.com" }
Write-Host "[OK] Ranking API enabled" -ForegroundColor Green

Write-Host "[..] Granting roles/discoveryengine.user to $WEB_SA" -ForegroundColor Cyan
gcloud projects add-iam-policy-binding $PROJECT `
    --member="serviceAccount:$WEB_SA" --role="roles/discoveryengine.user" *> $null
if ($LASTEXITCODE -ne 0) { Die "could not grant roles/discoveryengine.user" }
Write-Host "[OK] $WEB_SA can call the Ranking API" -ForegroundColor Green

Write-Host ""
Write-Host "[OK] Reranking is enabled. Now redeploy so the service picks it up:" -ForegroundColor Green
Write-Host "        .\deploy_dash_platform.ps1" -ForegroundColor Cyan
