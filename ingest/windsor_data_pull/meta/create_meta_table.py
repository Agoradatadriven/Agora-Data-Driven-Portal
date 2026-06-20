"""Create the raw_windsor.meta table (idempotent).

raw_windsor.meta is the Meta (Facebook Ads) connector's slot in the shared raw layer.
The schema below covers what the per-client template views need from Meta (date,
campaign, spend, impressions, clicks). It is created in asia-southeast1 alongside the
rest of the project.

Auth: Application Default Credentials (ADC).
"""

import os

from google.cloud import bigquery

LOCATION = "asia-southeast1"
PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
TABLE = "meta"

# TODO: align these columns to Windsor's ACTUAL Meta field names/types for this account.
# This schema matches the template data contract's needs (spend feeds the blended
# daily_performance roas rollup; impressions/clicks support channel-level reporting).
SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("campaign", "STRING"),
    bigquery.SchemaField("spend", "FLOAT64"),
    bigquery.SchemaField("impressions", "INT64"),
    bigquery.SchemaField("clicks", "INT64"),
]


def main() -> None:
    bq = bigquery.Client(project=PROJECT)
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"

    table = bigquery.Table(table_id, schema=SCHEMA)
    # exists_ok=True makes this idempotent (re-running converges, does not error).
    bq.create_table(table, exists_ok=True)
    print(f"[OK] table ready: {table_id} (location {LOCATION})")


if __name__ == "__main__":
    main()
