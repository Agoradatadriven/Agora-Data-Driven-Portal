"""Create the raw_windsor.tcs_shopify_orders table (idempotent).

TCS's Shopify orders slot in the shared raw layer. The schema covers exactly what the
client_tcs quiz-conversion views read: order identity, contact/customer email, dates,
money totals, discount codes, and line items. Direct-API source (not Windsor) -- see
tcs_shopify_loader.py for why. Created in asia-southeast1 alongside the rest of the project.

Auth: Application Default Credentials (ADC).
"""

import os

from google.cloud import bigquery

LOCATION = "asia-southeast1"
PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
TABLE = "tcs_shopify_orders"

SCHEMA = [
    bigquery.SchemaField("id", "INT64"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("contact_email", "STRING"),
    bigquery.SchemaField("customer_email", "STRING"),
    bigquery.SchemaField("customer_first_name", "STRING"),
    bigquery.SchemaField("customer_last_name", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("updated_at", "TIMESTAMP"),
    bigquery.SchemaField("currency", "STRING"),
    bigquery.SchemaField("subtotal_price", "NUMERIC"),
    bigquery.SchemaField("total_discounts", "NUMERIC"),
    bigquery.SchemaField("total_price", "NUMERIC"),
    bigquery.SchemaField("primary_discount_code", "STRING"),
    bigquery.SchemaField("discount_codes", "RECORD", mode="REPEATED", fields=[
        bigquery.SchemaField("code", "STRING"),
    ]),
    bigquery.SchemaField("line_items", "RECORD", mode="REPEATED", fields=[
        bigquery.SchemaField("title", "STRING"),
        bigquery.SchemaField("sku", "STRING"),
        bigquery.SchemaField("quantity", "INT64"),
        bigquery.SchemaField("price", "NUMERIC"),
        bigquery.SchemaField("vendor", "STRING"),
    ]),
]


def main() -> None:
    bq = bigquery.Client(project=PROJECT)
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"
    table = bigquery.Table(table_id, schema=SCHEMA)
    bq.create_table(table, exists_ok=True)
    print(f"[OK] table ready: {table_id} (location {LOCATION})")


if __name__ == "__main__":
    main()
