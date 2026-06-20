"""Windsor.ai Meta connector loader.

Raw target : raw_windsor.meta  (the shared raw layer; project agora-data-driven,
             dataset raw_windsor, location asia-southeast1).
Source     : Windsor.ai -- meta connector (Meta / Facebook Ads).
Cadence    : daily scheduled pull (see scripts/deploy_ingest_jobs.ps1, cron 20 1 * * *).

This is a scheduled API pull -- a plain WRITER of the shared raw_windsor dataset. It is
NOT self-gating: the freshness watermark logic lives downstream in the client export
jobs and the status dashboard, which probe whether raw_windsor advanced before
rebuilding. This loader just keeps raw_windsor.meta fresh once a day.

Auth:
  * The shared Windsor API key is read from Secret Manager (secret ``windsor-api-key``)
    via Application Default Credentials -- there is NO machine-specific key path.
  * BigQuery access is also via ADC (the ingest-runner@ service account on Cloud Run).

This file is a SKELETON. Every Windsor-API-specific request and field-mapping decision
is marked ``# TODO:`` for the operator to align to the agency's real Windsor account.
"""

import os

import requests
from google.cloud import bigquery
from google.cloud import secretmanager

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
WINDSOR_SECRET = "windsor-api-key"  # Secret Manager secret id (shared ingest key).
TABLE = "meta"
LOCATION = "asia-southeast1"

# Windsor REST base. The connector path / fields are account-specific -- see TODOs.
WINDSOR_API_BASE = "https://connectors.windsor.ai/all"


def read_windsor_api_key() -> str:
    """Fetch the shared Windsor API key from Secret Manager via ADC.

    The project number is never hardcoded; the secret resource path uses the project
    *id* (agora-data-driven), which is stable and resolved from env.
    """
    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT}/secrets/{WINDSOR_SECRET}/versions/latest"
    resp = sm.access_secret_version(request={"name": name})
    return resp.payload.data.decode("utf-8")


def fetch_rows(api_key: str) -> list:
    """Pull Meta Ads metrics from the Windsor REST API and return rows for BigQuery.

    Returns a list of dicts whose keys match the raw_windsor.meta schema
    (date, campaign, spend, impressions, clicks).
    """
    # TODO: Build the real Windsor request for the Meta connector. Windsor's "all"
    # endpoint takes an api_key plus a connector/datasource id, a field list, and a
    # date range, e.g.:
    #     params = {
    #         "api_key": api_key,
    #         "date_preset": "last_30d",
    #         "fields": "date,campaign,spend,impressions,clicks",
    #         "connector": "facebook",   # confirm the real connector id
    #     }
    #     resp = requests.get(WINDSOR_API_BASE, params=params, timeout=120)
    #     resp.raise_for_status()
    #     payload = resp.json()["data"]
    # TODO: Map Windsor's returned field names to the raw_windsor.meta columns. The
    # exact Windsor field labels depend on the account's connector config.
    raise NotImplementedError(
        "TODO: implement the Windsor Meta request + field mapping for this account"
    )


def load_rows(bq: bigquery.Client, rows: list) -> None:
    """Truncate-and-load ``rows`` into raw_windsor.meta (idempotent full refresh)."""
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"

    # Truncate-and-load: each daily run replaces the table contents, so a re-run is
    # idempotent and never double-counts.
    # TODO: switch to an incremental MERGE keyed on (date, campaign) if/when the
    # window grows beyond what a full daily refresh can comfortably reload.
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )

    load_job = bq.load_table_from_json(rows, table_id, job_config=job_config)
    load_job.result()  # wait for completion; raises on failure.
    print(f"[OK] loaded {len(rows)} rows into {table_id}")


def main() -> None:
    api_key = read_windsor_api_key()
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    rows = fetch_rows(api_key)
    load_rows(bq, rows)


if __name__ == "__main__":
    main()
