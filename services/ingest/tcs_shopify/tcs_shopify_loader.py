"""TCS Shopify orders loader (DIRECT-API, not Windsor).

Raw target : raw_windsor.tcs_shopify_orders  (the shared raw layer; project
             agora-data-driven, dataset raw_windsor, location asia-southeast1).
Source     : Shopify Admin GraphQL API (the TCS / Contract Shop store).
Cadence    : daily scheduled pull (see tools/deploy_ingest_jobs.ps1).

WHY THIS IS A DIRECT-API LOADER (a documented exception to "Windsor is the only
ingest source"): TCS's Business-Quiz diagnostic needs order-level Shopify data joined
to per-recipient Klaviyo events, a grain Windsor does not serve for this account. This
loader ports the proven pull from clients/TCS/archive_code/analytics.py. It is a plain
WRITER of raw_windsor -- NOT self-gating; the TCS export job self-gates downstream.

Auth:
  * Shopify Admin API token read from Secret Manager (secret ``tcs-shopify-token``) via
    Application Default Credentials -- never a committed key.
  * BigQuery access is also via ADC (the ingest-runner@ service account on Cloud Run).
"""

import os
import time
from typing import Any, Dict, List, Optional

import requests
from google.cloud import bigquery, secretmanager

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
LOCATION = "asia-southeast1"
TABLE = "tcs_shopify_orders"

SHOPIFY_SECRET = "tcs-shopify-token"  # Secret Manager id holding the Admin API token.
SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "contractshop.myshopify.com")
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-01")
API_SLEEP_SEC = 0.1


def read_secret(secret_id: str) -> str:
    """Fetch a secret payload from Secret Manager via ADC (project id, never number)."""
    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT}/secrets/{secret_id}/versions/latest"
    return sm.access_secret_version(request={"name": name}).payload.data.decode("utf-8")


def _num(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_gid(gid: Optional[str]) -> Optional[int]:
    """gid://shopify/Order/12345 -> 12345."""
    if not gid:
        return None
    try:
        return int(str(gid).split("/")[-1])
    except (TypeError, ValueError):
        return None


# GraphQL: one page of orders with the fields the TCS quiz model reads downstream.
QUERY = """
query($cursor: String) {
  orders(first: 50, after: $cursor, query: "status:any", sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id name createdAt updatedAt currencyCode email
        customer { id email firstName lastName }
        totalPriceSet { shopMoney { amount } }
        subtotalPriceSet { shopMoney { amount } }
        totalDiscountsSet { shopMoney { amount } }
        discountCodes
        lineItems(first: 25) {
          edges { node { title sku quantity vendor originalUnitPriceSet { shopMoney { amount } } } }
        }
      }
    }
  }
}
"""


def transform(node: Dict[str, Any]) -> Dict[str, Any]:
    """Map a GraphQL order node -> a raw_windsor.tcs_shopify_orders row dict."""
    cust = node.get("customer") or {}
    total = node.get("totalPriceSet") or {}
    subtotal = node.get("subtotalPriceSet") or {}
    discounts_total = node.get("totalDiscountsSet") or {}

    items: List[Dict[str, Any]] = []
    for edge in ((node.get("lineItems") or {}).get("edges") or []):
        i = edge.get("node") or {}
        price_set = i.get("originalUnitPriceSet") or {}
        items.append({
            "title": i.get("title"),
            "sku": i.get("sku"),
            "quantity": i.get("quantity"),
            "price": _num((price_set.get("shopMoney") or {}).get("amount")),
            "vendor": i.get("vendor"),
        })

    raw_codes = node.get("discountCodes") or []
    discount_codes = [{"code": c} for c in raw_codes]
    primary_discount_code = raw_codes[0] if raw_codes else None

    return {
        "id": _parse_gid(node.get("id")),
        "name": node.get("name"),
        "contact_email": node.get("email"),
        "customer_email": cust.get("email"),
        "customer_first_name": cust.get("firstName"),
        "customer_last_name": cust.get("lastName"),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "currency": node.get("currencyCode"),
        "subtotal_price": _num((subtotal.get("shopMoney") or {}).get("amount")),
        "total_discounts": _num((discounts_total.get("shopMoney") or {}).get("amount")),
        "total_price": _num((total.get("shopMoney") or {}).get("amount")),
        "primary_discount_code": primary_discount_code,
        "discount_codes": discount_codes,
        "line_items": items,
    }


def fetch_orders(token: str) -> List[Dict[str, Any]]:
    """Paginate the Shopify Admin GraphQL API and return transformed rows."""
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    rows: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        resp = requests.post(url, headers=headers,
                             json={"query": QUERY, "variables": {"cursor": cursor}}, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(f"Shopify GraphQL error: {payload['errors']}")

        orders = (payload.get("data") or {}).get("orders") or {}
        for edge in orders.get("edges", []):
            rows.append(transform(edge["node"]))

        page = orders.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        time.sleep(API_SLEEP_SEC)

    print(f"[tcs_shopify] fetched {len(rows)} orders from Shopify.")
    return rows


def load_rows(bq: bigquery.Client, rows: List[Dict[str, Any]]) -> None:
    """Truncate-and-load rows into raw_windsor.tcs_shopify_orders (idempotent refresh)."""
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    bq.load_table_from_json(rows, table_id, job_config=job_config).result()
    print(f"[OK] loaded {len(rows)} rows into {table_id}")


def main() -> None:
    token = read_secret(SHOPIFY_SECRET)
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    rows = fetch_orders(token)
    if not rows:
        print("[tcs_shopify] no orders returned; leaving table unchanged.")
        return
    load_rows(bq, rows)


if __name__ == "__main__":
    main()
