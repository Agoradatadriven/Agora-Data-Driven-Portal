"""Create the raw_windsor.tcs_quiz table (idempotent).

TCS's Business-Quiz submissions slot in the shared raw layer: one row per quiz lead
(email + submitted_at + answers + setup flags). This is the funnel entry point the
client_tcs quiz-conversion / engagement views hang off. Direct-API source (Google Sheet),
not Windsor -- see tcs_quiz_loader.py.

Auth: Application Default Credentials (ADC).
"""

import os

from google.cloud import bigquery

LOCATION = "asia-southeast1"
PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
TABLE = "tcs_quiz"

SCHEMA = [
    bigquery.SchemaField("email", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("submitted_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("first_name", "STRING"),
    bigquery.SchemaField("business_age", "STRING"),
    bigquery.SchemaField("services", "STRING"),
    bigquery.SchemaField("description", "STRING"),
    bigquery.SchemaField("current", "STRING"),
    bigquery.SchemaField("website", "STRING"),
    bigquery.SchemaField("pain_points", "STRING"),
    bigquery.SchemaField("ein", "INT64"),
    bigquery.SchemaField("llc", "INT64"),
    bigquery.SchemaField("bank_account", "INT64"),
    bigquery.SchemaField("operating_agreement", "INT64"),
    bigquery.SchemaField("trademark", "INT64"),
    bigquery.SchemaField("refund_policy", "INT64"),
    bigquery.SchemaField("terms", "INT64"),
]


def main() -> None:
    bq = bigquery.Client(project=PROJECT)
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"
    table = bigquery.Table(table_id, schema=SCHEMA)
    bq.create_table(table, exists_ok=True)
    print(f"[OK] table ready: {table_id} (location {LOCATION})")


if __name__ == "__main__":
    main()
