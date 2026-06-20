"""Windsor.ai -> raw_windsor.fields ingest loader.

Raw target : raw_windsor.fields (the shared Windsor mirror dataset)
Source     : Windsor.ai field/metadata helper connector. This is NOT a marketing
             data source -- it is Windsor's own helper that enumerates the fields
             (metrics + dimensions) each connector exposes. We mirror that catalogue
             into raw_windsor.fields so the per-client SQL views can be validated /
             documented against the field names Windsor actually offers.
Cadence    : daily (scheduled Cloud Run job; staggered just before the client
             export window -- see scripts/deploy_ingest_jobs.ps1 $JOBS)

This is a plain scheduled WRITER of raw_windsor. It is NOT self-gating: now that
the only source is Windsor (a scheduled REST API), the ingest jobs just pull and
land data daily. The self-gating freshness logic lives DOWNSTREAM in the consumers
(the client export jobs and the status dashboard), which probe whether raw_windsor
advanced past their _freshness.json watermark before rebuilding. That is why
freshness.py is NOT vendored here.

Auth model:
  * Windsor REST API key  -> read from Secret Manager secret `windsor-api-key`
                             (mounted as WINDSOR_API_KEY by the deploy script).
  * Google client libs    -> Application Default Credentials (the ingest-runner@
                             service account the Cloud Run job runs as).
"""

import json
import os
import sys
import tempfile

import requests
from google.cloud import bigquery
from google.cloud import secret_manager

# --- Runtime config (resolved from env; never hardcode project number) --------
PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
STAGING_BUCKET = os.environ.get("STAGING_BUCKET", "agora-data-driven-staging")
LOCATION = "asia-southeast1"

TABLE = "fields"  # raw_windsor.fields
WINDSOR_SECRET = "windsor-api-key"

# Windsor.ai fields/metadata helper endpoint.
# TODO: confirm the exact Windsor "fields" helper URL/shape against the Windsor.ai
#       API docs (the helper that lists available metrics/dimensions per connector).
WINDSOR_FIELDS_URL = "https://connectors.windsor.ai/fields"


def read_windsor_api_key():
    """Read the shared Windsor API key.

    Prefer the secret mounted by the deploy script as the WINDSOR_API_KEY env var;
    fall back to reading the Secret Manager secret directly via ADC so the loader
    also works when run outside Cloud Run (e.g. a manual backfill).
    """
    key = os.environ.get("WINDSOR_API_KEY")
    if key:
        return key.strip()
    client = secret_manager.SecretManagerServiceClient()
    name = f"projects/{PROJECT}/secrets/{WINDSOR_SECRET}/versions/latest"
    resp = client.access_secret_version(request={"name": name})
    return resp.payload.data.decode("utf-8").strip()


def fetch_windsor_rows(api_key):
    """Pull the Windsor field catalogue from the fields/metadata helper.

    Returns a list of dicts, one per (connector, field), already mapped onto the
    raw_windsor.fields schema (see create_fields_table.py).
    """
    # TODO: build the real Windsor request for the fields helper. The helper
    #       typically takes the api_key (and optionally a connector selector) and
    #       returns the available fields, their type (metric/dimension) and a
    #       human label. Page through results if the helper returns more than one
    #       page.
    params = {
        "api_key": api_key,
    }
    resp = requests.get(WINDSOR_FIELDS_URL, params=params, timeout=120)
    resp.raise_for_status()
    payload = resp.json()
    raw_rows = payload.get("data", []) if isinstance(payload, dict) else payload

    rows = []
    for r in raw_rows:
        # TODO: align these keys to the fields helper's actual response shape.
        rows.append(
            {
                "connector": r.get("connector"),
                "field": r.get("field"),
                "label": r.get("label"),
                "field_type": r.get("type"),
                "data_type": r.get("data_type"),
            }
        )
    return rows


def load_rows(rows):
    """Replace raw_windsor.fields with the freshly pulled catalogue.

    Staging NDJSON to a temp file and using a WRITE_TRUNCATE load keeps the daily
    pull idempotent: each run lands a clean snapshot of the current field catalogue.
    """
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"

    if not rows:
        print(f"[..] no rows returned from Windsor for {TABLE}; nothing to load")
        return

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ndjson", delete=False, encoding="utf-8"
    ) as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
        ndjson_path = fh.name

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        autodetect=False,
    )
    with open(ndjson_path, "rb") as src:
        load_job = bq.load_table_from_file(src, table_id, job_config=job_config)
    load_job.result()
    os.remove(ndjson_path)

    table = bq.get_table(table_id)
    print(f"[OK] loaded {table.num_rows} rows into {table_id}")


def main():
    print(f"[..] Windsor ingest: fields helper -> {RAW_DATASET}.{TABLE}")
    api_key = read_windsor_api_key()
    rows = fetch_windsor_rows(api_key)
    load_rows(rows)
    print("[OK] fields ingest complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # surface a non-zero exit so the Cloud Run job fails loudly
        print(f"[ERROR] fields ingest failed: {exc}", file=sys.stderr)
        sys.exit(1)
