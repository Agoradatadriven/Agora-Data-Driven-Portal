"""Create raw_windsor.hubspot with a minimal plausible schema (idempotent).

Run once when wiring the connector. Creating the dataset and table up front lets
hubspot_loader.py use a non-autodetect WRITE_TRUNCATE load against a stable schema.
Re-running is safe: the dataset/table are created only if absent.

The schema below is a minimal, plausible shape for HubSpot CRM/marketing data
(HubSpot is email/CRM rather than pure ad-spend, so its columns differ from the
ad connectors).
# TODO: align these columns to Windsor's real field names/types for HubSpot
#       before the first production pull (and keep hubspot_loader.py's row mapping
#       in lockstep).
"""

import os

from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
LOCATION = "asia-southeast1"
TABLE = "hubspot"

SCHEMA = [
    bigquery.SchemaField("metric_date", "DATE"),
    bigquery.SchemaField("campaign", "STRING"),
    bigquery.SchemaField("emails_sent", "INT64"),
    bigquery.SchemaField("emails_opened", "INT64"),
    bigquery.SchemaField("clicks", "INT64"),
    bigquery.SchemaField("contacts", "INT64"),
    bigquery.SchemaField("deals", "INT64"),
    bigquery.SchemaField("revenue", "FLOAT64"),
]


def main():
    bq = bigquery.Client(project=PROJECT, location=LOCATION)

    dataset_id = f"{PROJECT}.{RAW_DATASET}"
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = LOCATION
    bq.create_dataset(dataset, exists_ok=True)
    print(f"[OK] dataset ready: {dataset_id}")

    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"
    table = bigquery.Table(table_id, schema=SCHEMA)
    bq.create_table(table, exists_ok=True)
    print(f"[OK] table ready: {table_id}")


if __name__ == "__main__":
    main()
