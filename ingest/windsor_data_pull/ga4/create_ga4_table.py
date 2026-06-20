"""Create the raw_windsor.ga4 table (idempotent).

raw_windsor.ga4 is the GA4 connector's slot in the shared raw layer. The schema below
covers what the per-client template views need from GA4 (date, channel, sessions,
users, conversions). It is created in asia-southeast1 alongside the rest of the project.

Auth: Application Default Credentials (ADC).
"""

import os

from google.cloud import bigquery

LOCATION = "asia-southeast1"
PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
TABLE = "ga4"

# TODO: align these columns to Windsor's ACTUAL GA4 field names/types for this account.
# This schema matches the template data contract's needs (the stg_source view reads
# date/channel/sessions/users/conversions from the GA4 mirror).
SCHEMA = [
    bigquery.SchemaField("date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("channel", "STRING"),
    bigquery.SchemaField("sessions", "INT64"),
    bigquery.SchemaField("users", "INT64"),
    bigquery.SchemaField("conversions", "INT64"),
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
