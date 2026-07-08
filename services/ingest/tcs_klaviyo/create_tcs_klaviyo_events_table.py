"""Create the raw_windsor.tcs_klaviyo_events table (idempotent).

TCS's per-recipient email-events slot in the shared raw layer: ONE ROW PER SEND, flagged
is_open / is_click. This is the grain client_tcs.stg_email_events reads to answer the
Business-Quiz diagnostic (are quiz leads opening/clicking less over time?). Direct-API
source (Klaviyo Events API), not Windsor -- see tcs_klaviyo_loader.py.

Auth: Application Default Credentials (ADC).
"""

import os

from google.cloud import bigquery

LOCATION = "asia-southeast1"
PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
TABLE = "tcs_klaviyo_events"

SCHEMA = [
    bigquery.SchemaField("message_id", "STRING"),
    bigquery.SchemaField("email", "STRING"),
    bigquery.SchemaField("subject", "STRING"),
    bigquery.SchemaField("campaign", "STRING"),
    bigquery.SchemaField("flow", "STRING"),
    bigquery.SchemaField("sent_at", "TIMESTAMP"),
    bigquery.SchemaField("opened_at", "TIMESTAMP"),
    bigquery.SchemaField("clicked_at", "TIMESTAMP"),
    bigquery.SchemaField("is_open", "BOOL"),
    bigquery.SchemaField("is_click", "BOOL"),
]


def main() -> None:
    bq = bigquery.Client(project=PROJECT)
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"
    table = bigquery.Table(table_id, schema=SCHEMA)
    bq.create_table(table, exists_ok=True)
    print(f"[OK] table ready: {table_id} (location {LOCATION})")


if __name__ == "__main__":
    main()
