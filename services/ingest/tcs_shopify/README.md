# TCS Shopify orders loader (direct-API)

Pulls TCS's Shopify orders from the **Shopify Admin GraphQL API** once a day and loads
them into the shared raw layer table **`raw_windsor.tcs_shopify_orders`** (project
`agora-data-driven`, location `asia-southeast1`). Scheduled writer of `raw_windsor` --
not self-gating; the `tcs-export` job self-gates downstream.

> **Why direct-API, not Windsor:** TCS's Business-Quiz diagnostic joins order-level
> Shopify data to per-recipient Klaviyo events — a grain Windsor does not serve for this
> account. This is a deliberate, documented exception to "Windsor is the only ingest
> source". The pull is ported from `clients/TCS/archive_code/analytics.py`.

## How to run

```powershell
# 1. Ensure the shared dataset exists (idempotent; once per project).
.\.venv\Scripts\python.exe services\ingest\create_dataset.py

# 2. Ensure the raw_windsor.tcs_shopify_orders table exists (idempotent).
.\.venv\Scripts\python.exe services\ingest\tcs_shopify\create_tcs_shopify_orders_table.py

# 3. Run the loader (truncate-and-load).
.\.venv\Scripts\python.exe services\ingest\tcs_shopify\tcs_shopify_loader.py
```

The loader reads the Shopify Admin token from Secret Manager (secret
`tcs-shopify-token`) via ADC; provision it with
[`services/ingest/tcs_provision_secrets.ps1`](../tcs_provision_secrets.ps1). Store domain
via env `SHOPIFY_STORE_DOMAIN` (default `contractshop.myshopify.com`). In production it
runs as the Cloud Run job `tcs-shopify-ingest`, deployed + scheduled by
[`tools/deploy_ingest_jobs.ps1`](../../../tools/deploy_ingest_jobs.ps1).

**Raw target:** `raw_windsor.tcs_shopify_orders` (`id, name, contact_email,
customer_email, created_at, subtotal_price, total_price, primary_discount_code,
discount_codes[], line_items[]`).
