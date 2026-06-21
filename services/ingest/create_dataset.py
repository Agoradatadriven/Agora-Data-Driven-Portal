"""Create the shared ``raw_windsor`` BigQuery dataset (idempotent).

``raw_windsor`` is the ONE shared raw layer for the whole monorepo: every Windsor.ai
connector loader (ga4, google_ads, meta, ...) lands its source rows into a
``raw_windsor.<connector>`` table here, and every per-client SQL view reads
*downstream* from these mirror tables. Windsor is the only ingest source; there is no
other raw layer.

This script only creates the *dataset* (the namespace), not the per-connector tables;
each connector owns its own ``create_<x>_table.py``. Run this once before (or it is a
harmless no-op alongside) any connector loader.

The dataset lives in ``asia-southeast1`` -- the single region for everything in this
project. BigQuery dataset location is immutable after creation, so it is pinned here.

Environment:
  GCP_PROJECT   -- the GCP project id (defaults to ``agora-data-driven``).
  RAW_DATASET   -- the shared raw dataset id (defaults to ``raw_windsor``).

Auth: Application Default Credentials (ADC). On Cloud Run the ingest-runner@ service
account provides these automatically; locally run ``gcloud auth application-default
login`` first.
"""

import os

from google.cloud import bigquery

# Single region for everything in this project (Singapore). Dataset location is
# immutable once set, so this constant is load-bearing -- do not change it later.
LOCATION = "asia-southeast1"

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")


def main() -> None:
    bq = bigquery.Client(project=PROJECT)
    dataset_id = f"{PROJECT}.{RAW_DATASET}"

    dataset = bigquery.Dataset(dataset_id)
    dataset.location = LOCATION
    dataset.description = (
        "Shared raw layer for all Windsor.ai connectors. Connector loaders write "
        "raw_windsor.<connector> tables; per-client SQL views read from here."
    )

    # exists_ok=True makes this idempotent: re-running converges to the desired
    # state instead of erroring if the dataset already exists.
    bq.create_dataset(dataset, exists_ok=True)
    print(f"[OK] dataset ready: {dataset_id} (location {LOCATION})")


if __name__ == "__main__":
    main()
