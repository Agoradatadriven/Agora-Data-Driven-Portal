"""Create raw_windsor.fields with a minimal plausible schema (idempotent).

Run once when wiring the connector. Creating the dataset and table up front lets
fields_loader.py use a non-autodetect WRITE_TRUNCATE load against a stable schema.
Re-running is safe: the dataset/table are created only if absent.

raw_windsor.fields mirrors Windsor's field/metadata helper -- a catalogue of the
fields (metrics + dimensions) each Windsor connector exposes -- so per-client SQL
views can be validated/documented against the field names Windsor actually offers.
# TODO: align these columns to the Windsor fields helper's real response shape
#       before the first production pull (and keep fields_loader.py's row mapping
#       in lockstep).
"""

import os

from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
LOCATION = "asia-southeast1"
TABLE = "fields"

SCHEMA = [
    bigquery.SchemaField("connector", "STRING"),
    bigquery.SchemaField("field", "STRING"),
    bigquery.SchemaField("label", "STRING"),
    bigquery.SchemaField("field_type", "STRING"),  # metric | dimension
    bigquery.SchemaField("data_type", "STRING"),
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
