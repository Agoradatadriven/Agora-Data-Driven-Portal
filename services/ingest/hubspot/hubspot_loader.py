"""Windsor.ai -> raw_windsor.hubspot ingest loader.

Raw target : raw_windsor.hubspot (the shared Windsor mirror dataset)
Source     : Windsor.ai "HubSpot" connector (CRM / marketing email + deals)
Cadence    : daily (scheduled Cloud Run job; staggered just before the client
             export window -- see tools/deploy_ingest_jobs.ps1 $JOBS)

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

TABLE = "hubspot"  # raw_windsor.hubspot
WINDSOR_SECRET = "windsor-api-key"

# Windsor.ai REST endpoint. The connector slug identifies HubSpot.
WINDSOR_BASE_URL = "https://connectors.windsor.ai/all"
# TODO: confirm the exact Windsor connector slug for HubSpot against the
#       Windsor.ai connector catalogue.
WINDSOR_CONNECTOR = "hubspot"


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
    """Pull HubSpot rows from the Windsor.ai REST API.

    Returns a list of dicts, one per source row, already mapped onto the
    raw_windsor.hubspot schema (see create_hubspot_table.py).
    """
    # TODO: build the real Windsor request for HubSpot. Windsor's /all endpoint
    #       takes the api_key, a connector selector and a comma-separated `fields`
    #       list, and (typically) a date range. HubSpot is CRM/email rather than
    #       pure ad-spend, so the field set differs from the ad connectors. Page
    #       through results if the connector returns more than one page.
    params = {
        "api_key": api_key,
        "connector": WINDSOR_CONNECTOR,
        # TODO: enumerate the real Windsor field names for HubSpot and map them to
        #       our column names below.
        "fields": "date,campaign,emails_sent,emails_opened,clicks,contacts,deals,revenue",
        "date_preset": "yesterday",
    }
    resp = requests.get(WINDSOR_BASE_URL, params=params, timeout=120)
    resp.raise_for_status()
    payload = resp.json()
    raw_rows = payload.get("data", []) if isinstance(payload, dict) else payload

    rows = []
    for r in raw_rows:
        # TODO: align these keys to Windsor's actual field names for HubSpot.
        rows.append(
            {
                "metric_date": r.get("date"),
                "campaign": r.get("campaign"),
                "emails_sent": r.get("emails_sent"),
                "emails_opened": r.get("emails_opened"),
                "clicks": r.get("clicks"),
                "contacts": r.get("contacts"),
                "deals": r.get("deals"),
                "revenue": r.get("revenue"),
            }
        )
    return rows


def load_rows(rows):
    """Replace raw_windsor.hubspot with the freshly pulled rows.

    Staging NDJSON to a temp file and using a WRITE_TRUNCATE load keeps the daily
    pull idempotent: each run lands a clean snapshot of the source window.
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
    print(f"[..] Windsor ingest: {WINDSOR_CONNECTOR} -> {RAW_DATASET}.{TABLE}")
    api_key = read_windsor_api_key()
    rows = fetch_windsor_rows(api_key)
    load_rows(rows)
    print("[OK] hubspot ingest complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # surface a non-zero exit so the Cloud Run job fails loudly
        print(f"[ERROR] hubspot ingest failed: {exc}", file=sys.stderr)
        sys.exit(1)
