"""Stage 2 of the Agora data contract -- the `template` client EXPORT job.

This Cloud Run job is the middle stage of the three-stage data contract:

    Stage 1  sql/*.sql views (BigQuery)        -- typed/filtered rows from raw_windsor
    Stage 2  job/main.py  (THIS FILE)          -- assemble the `data` dict, upload <c>.json
    Stage 3  dash/dashboard.html               -- read data.* and render

The keys of the assembled `data` dict (and of the rows inside `data["daily"]`) MUST match the
`data.*` keys that dashboard.html reads. Renaming a key here silently breaks the dashboard.

Self-gating freshness:
  The job runs on a `*/10` Cloud Scheduler tick but only rebuilds when the shared `raw_windsor`
  mirror table(s) it reads (GATING_TABLES) advanced past the `_freshness.json` watermark stored in
  this client's OWN bucket. `FORCE_REBUILD=1` bypasses the gate (used for view-only / code / seed
  changes, which do NOT advance the upstream watermark and would otherwise no-op and serve stale
  JSON). The watermark is written ONLY after a successful upload -- see the end of main().
"""

import json
import os
from datetime import datetime, timezone

from google.cloud import bigquery, storage

import freshness

# --- Fixed project constants (use literally; one project, one region, never another) ---
PROJECT = "agora-data-driven"
LOC = "asia-southeast1"  # Singapore. Everything lives here, never another region.

# --- The ONE per-client line -- derive every other name from CLIENT, never re-type ---
CLIENT = "template"
DATASET = f"client_{CLIENT}"
BUCKET = f"agora-data-driven-{CLIENT}-dash"
DATA_OBJECT = f"{CLIENT}.json"

# The BASE Windsor mirror table(s) the Stage 1 views read. We watermark THESE -- NEVER a view.
# A view has no last_modified of its own; freshness must probe the raw_windsor base/mirror tables
# the views select from.
GATING_TABLES = ["raw_windsor.metrics_daily"]
WATERMARK_OBJECT = "_freshness.json"


def _iso_now():
    """Current UTC time as a second-precision ISO-8601 string (build timestamp)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fmt_date(value):
    """Serialize a BigQuery DATE (a datetime.date) as 'YYYY-MM-DD'."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _read_kpis(bq):
    """Stage 1 -> Stage 2: read the single-row kpi_overview view into one kpis dict.

    Keys MUST match dashboard.html's data.kpis.*:
      sessions, users, conversions, spend, revenue, roas, days_covered.
    """
    sql = f"""
        SELECT sessions, users, conversions, spend, revenue, roas, days_covered
        FROM `{PROJECT}.{DATASET}.kpi_overview`
    """
    rows = list(bq.query(sql, location=LOC).result())
    if not rows:
        # No KPI row yet (empty upstream) -- emit a well-formed zeroed dict so the dashboard still
        # renders rather than erroring on missing keys.
        return {
            "sessions": 0,
            "users": 0,
            "conversions": 0,
            "spend": 0.0,
            "revenue": 0.0,
            "roas": 0.0,
            "days_covered": 0,
        }
    r = rows[0]
    return {
        "sessions": int(r["sessions"] or 0),
        "users": int(r["users"] or 0),
        "conversions": int(r["conversions"] or 0),
        "spend": float(r["spend"] or 0.0),
        "revenue": float(r["revenue"] or 0.0),
        "roas": float(r["roas"] or 0.0),
        "days_covered": int(r["days_covered"] or 0),
    }


def _read_daily(bq):
    """Stage 1 -> Stage 2: read daily_performance into a list of row dicts.

    Each row's keys MUST match dashboard.html's data.daily[].*:
      metric_date (YYYY-MM-DD), sessions, users, conversions, spend, revenue, roas.
    """
    sql = f"""
        SELECT metric_date, sessions, users, conversions, spend, revenue, roas
        FROM `{PROJECT}.{DATASET}.daily_performance`
        ORDER BY metric_date
    """
    daily = []
    for r in bq.query(sql, location=LOC).result():
        daily.append({
            "metric_date": _fmt_date(r["metric_date"]),
            "sessions": int(r["sessions"] or 0),
            "users": int(r["users"] or 0),
            "conversions": int(r["conversions"] or 0),
            "spend": float(r["spend"] or 0.0),
            "revenue": float(r["revenue"] or 0.0),
            "roas": float(r["roas"] or 0.0),
        })
    return daily


def _data_through(observed, daily):
    """Pick the 'data_through' timestamp: the newest observed upstream timestamp, falling back to
    the latest daily metric_date when the probe returned nothing usable."""
    stamps = [ts for ts in (observed or {}).values() if ts]
    if stamps:
        return max(stamps)
    # Fallback: the most recent day we actually have data for (daily is ordered ascending).
    if daily:
        return daily[-1]["metric_date"]
    return None


def main():
    bq = bigquery.Client(project=PROJECT)
    gcs = storage.Client(project=PROJECT)
    bucket = gcs.bucket(BUCKET)

    # Probe the BASE raw_windsor mirror table(s) the views read, then compare against the watermark.
    observed = freshness.probe_bq_last_modified(bq, GATING_TABLES, LOC)
    watermark = freshness.read_watermark(bucket, WATERMARK_OBJECT)

    # FORCE_REBUILD=1 bypasses the freshness gate -- used for view-only / code / seed changes that
    # do not advance the upstream watermark and would otherwise no-op and keep serving stale JSON.
    forced = os.environ.get("FORCE_REBUILD") == "1"

    if not forced and not freshness.is_stale(observed, watermark):
        print(f"[{CLIENT}] upstream unchanged since watermark -- fresh, no rebuild.")
        return

    print(f"[{CLIENT}] rebuilding (forced={forced}); reading views from {DATASET}.")
    kpis = _read_kpis(bq)
    daily = _read_daily(bq)

    data = {
        "client": CLIENT,
        "last_updated": _iso_now(),
        "data_through": _data_through(observed, daily),
        "kpis": kpis,
        "daily": daily,
    }

    # Upload the private data JSON. cache_control no-store: the dashboard proxies this per request
    # to authed sessions only, so it must never be cached at any edge.
    blob = bucket.blob(DATA_OBJECT)
    blob.cache_control = "no-store"
    blob.upload_from_string(
        json.dumps(data, separators=(",", ":")),
        content_type="application/json",
    )
    print(f"[{CLIENT}] uploaded gs://{BUCKET}/{DATA_OBJECT} ({len(daily)} daily rows).")

    # Advance the watermark ONLY AFTER the upload succeeded. If the upload had raised, we would NOT
    # reach this line, so a failed build never advances the watermark -- the next tick retries.
    freshness.write_watermark(bucket, WATERMARK_OBJECT, observed)
    print(f"[{CLIENT}] watermark advanced -> {WATERMARK_OBJECT}.")


if __name__ == "__main__":
    main()
