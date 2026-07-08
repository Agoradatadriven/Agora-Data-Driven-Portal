"""TCS Klaviyo email-events loader (DIRECT-API, not Windsor).

Raw target : raw_windsor.tcs_klaviyo_events  (the shared raw layer; project
             agora-data-driven, dataset raw_windsor, location asia-southeast1).
Source     : Klaviyo Events API -- Received / Opened / Clicked Email metrics.
Cadence    : daily scheduled pull (see tools/deploy_ingest_jobs.ps1).

WHY THIS IS A DIRECT-API LOADER (a documented exception to "Windsor is the only ingest
source"): the Business-Quiz diagnostic ("are these quiz leads opening/clicking LESS this
year?") needs PER-RECIPIENT open/click events, which Windsor's Klaviyo connector does not
expose (it serves campaign-level aggregates). This loader ports the "Email Activity" pull
from clients/TCS/archive_code/analytics.py: it produces ONE ROW PER SEND, flagged
is_open / is_click, joined to opens/clicks by Klaviyo's per-send $message id.

Grain: one row per (recipient, message) send -> exactly what client_tcs.stg_email_events
reads. Full-history truncate-and-load each run (idempotent).

Auth:
  * Klaviyo private API key from Secret Manager (secret ``tcs-klaviyo-key``) via ADC.
  * BigQuery via ADC (ingest-runner@ on Cloud Run).
"""

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from dateutil.relativedelta import relativedelta
from google.cloud import bigquery, secretmanager

PROJECT = os.environ.get("GCP_PROJECT", "agora-data-driven")
RAW_DATASET = os.environ.get("RAW_DATASET", "raw_windsor")
LOCATION = "asia-southeast1"
TABLE = "tcs_klaviyo_events"

KLAVIYO_SECRET = "tcs-klaviyo-key"  # Secret Manager id holding the Klaviyo private key.
KLAVIYO_BASE = "https://a.klaviyo.com/api"
KLAVIYO_REVISION = "2024-10-15"
# Backfill start; the full window is re-pulled each run (truncate-load).
START_DATE = os.environ.get("KLAVIYO_START_DATE", "2020-01-01")


def read_secret(secret_id: str) -> str:
    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT}/secrets/{secret_id}/versions/latest"
    return sm.access_secret_version(request={"name": name}).payload.data.decode("utf-8")


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "accept": "application/json",
        "revision": KLAVIYO_REVISION,
    }


def get_metric_map(headers: Dict[str, str]) -> Dict[str, str]:
    """Return {metric_name: metric_id} for this Klaviyo account."""
    resp = requests.get(f"{KLAVIYO_BASE}/metrics", headers=headers, timeout=60)
    resp.raise_for_status()
    return {m["attributes"]["name"]: m["id"] for m in resp.json().get("data", [])}


def fetch_events(headers, metric_id, start, end, fetch_profile=False) -> List[Dict[str, Any]]:
    """Paginate the Events API for one metric within [start, end)."""
    url = f"{KLAVIYO_BASE}/events"
    start_str = start.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%S")
    params = {
        "filter": f'equals(metric_id,"{metric_id}"),'
                  f"greater-than(datetime,{start_str}),less-than(datetime,{end_str})",
        "sort": "-datetime",
        "page[size]": 100,
    }
    if fetch_profile:
        params["include"] = "profile"

    events: List[Dict[str, Any]] = []
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 5)))
            continue
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("data", [])
        if not batch:
            break

        if fetch_profile and "included" in data:
            profile_email = {p["id"]: (p.get("attributes") or {}).get("email")
                             for p in data["included"]}
            for ev in batch:
                pid = (((ev.get("relationships") or {}).get("profile") or {})
                       .get("data") or {}).get("id")
                ev["_email"] = profile_email.get(pid)

        events.extend(batch)
        url = (data.get("links") or {}).get("next")
        params = {}  # subsequent pages carry params in the next link
    return events


def _props(ev: Dict[str, Any]) -> Dict[str, Any]:
    return (ev.get("attributes") or {}).get("event_properties") or {}


def collect_rows(headers: Dict[str, str], metrics: Dict[str, str]) -> List[Dict[str, Any]]:
    """Walk monthly windows, join sends<-opens/clicks by $message, emit per-send rows."""
    received = metrics.get("Received Email")
    opened = metrics.get("Opened Email")
    clicked = metrics.get("Clicked Email")
    if not received:
        raise RuntimeError("Klaviyo metric 'Received Email' not found for this account.")

    start = datetime.fromisoformat(START_DATE).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    rows: List[Dict[str, Any]] = []
    cur = start
    while cur < now:
        nxt = min(cur + relativedelta(months=1), now)
        # Opens/clicks lag sends -- extend their window +7d to catch late interactions.
        lag_end = nxt + timedelta(days=7)

        sends = fetch_events(headers, received, cur, nxt, fetch_profile=True)
        opens = fetch_events(headers, opened, cur, lag_end) if opened else []
        clicks = fetch_events(headers, clicked, cur, lag_end) if clicked else []

        open_at: Dict[str, str] = {}
        for ev in opens:
            mid = _props(ev).get("$message")
            if mid and mid not in open_at:
                open_at[mid] = ev["attributes"]["datetime"]
        click_at: Dict[str, str] = {}
        for ev in clicks:
            mid = _props(ev).get("$message")
            if mid and mid not in click_at:
                click_at[mid] = ev["attributes"]["datetime"]

        for ev in sends:
            p = _props(ev)
            mid = p.get("$message")
            rows.append({
                "message_id": mid,
                "email": (ev.get("_email") or "").lower().strip() or None,
                "subject": p.get("Subject"),
                "campaign": p.get("Campaign Name"),
                "flow": p.get("$flow") or "Campaign",
                "sent_at": ev["attributes"]["datetime"],
                "opened_at": open_at.get(mid),
                "clicked_at": click_at.get(mid),
                "is_open": mid in open_at,
                "is_click": mid in click_at,
            })

        print(f"[tcs_klaviyo] {cur.date()}..{nxt.date()}: "
              f"{len(sends)} sends, {len(opens)} opens, {len(clicks)} clicks "
              f"(running total {len(rows)})")
        cur = nxt

    return rows


def load_rows(bq: bigquery.Client, rows: List[Dict[str, Any]]) -> None:
    table_id = f"{PROJECT}.{RAW_DATASET}.{TABLE}"
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    bq.load_table_from_json(rows, table_id, job_config=job_config).result()
    print(f"[OK] loaded {len(rows)} rows into {table_id}")


def main() -> None:
    headers = _headers(read_secret(KLAVIYO_SECRET))
    bq = bigquery.Client(project=PROJECT, location=LOCATION)
    rows = collect_rows(headers, get_metric_map(headers))
    if not rows:
        print("[tcs_klaviyo] no send events returned; leaving table unchanged.")
        return
    load_rows(bq, rows)


if __name__ == "__main__":
    main()
