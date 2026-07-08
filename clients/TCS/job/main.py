"""Stage 2 of the Agora data contract -- the `tcs` client EXPORT job (Business Quiz dashboard).

Three-stage data contract:
    Stage 1  sql/*.sql views (BigQuery)   -- quiz -> conversion -> engagement models from raw_windsor.tcs_*
    Stage 2  job/main.py  (THIS FILE)     -- assemble the `data` dict, upload tcs.json
    Stage 3  dash/dashboard.html          -- read data.* and render the diagnostic

The keys of the assembled `data` dict (and of the rows inside data["monthly"] / ["cohorts"] /
["conversion_trend"] / ["leads"]) MUST match the `data.*` keys dashboard.html reads. Renaming a key
here silently breaks the dashboard.

Self-gating freshness:
  Runs on a `*/10` tick but only rebuilds when the shared raw_windsor.tcs_* mirror tables the views
  read (GATING_TABLES) advanced past the `_freshness.json` watermark in THIS client's bucket.
  FORCE_REBUILD=1 bypasses the gate. The watermark is written ONLY after a successful upload.
"""

import json
import os
from datetime import datetime, timezone

from google.cloud import bigquery, storage

import freshness

PROJECT = "agora-data-driven"
LOC = "asia-southeast1"  # Singapore.

CLIENT = "tcs"
DATASET = f"client_{CLIENT}"
BUCKET = f"agora-data-driven-{CLIENT}-dash"
DATA_OBJECT = f"{CLIENT}.json"

# The BASE raw_windsor mirror tables the Stage 1 views read. We watermark THESE -- never a view.
GATING_TABLES = [
    "raw_windsor.tcs_quiz",
    "raw_windsor.tcs_shopify_orders",
    "raw_windsor.tcs_klaviyo_events",
]
WATERMARK_OBJECT = "_freshness.json"
LEADS_LIMIT = 800


def _iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _i(v):
    return int(v) if v is not None else 0


def _f(v):
    return float(v) if v is not None else None


def _date(v):
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def _read_kpis(bq):
    sql = f"""
        SELECT leads, converted, conversion_rate, revenue, avg_days_to_convert,
               avg_opens, avg_clicks, leads_this_year, converted_this_year,
               open_rate_this_year, open_rate_prior, click_rate_this_year, click_rate_prior
        FROM `{PROJECT}.{DATASET}.kpi_overview`
    """
    rows = list(bq.query(sql, location=LOC).result())
    if not rows:
        return {k: 0 for k in ("leads", "converted", "leads_this_year", "converted_this_year")} | \
               {k: None for k in ("conversion_rate", "revenue", "avg_days_to_convert", "avg_opens",
                                  "avg_clicks", "open_rate_this_year", "open_rate_prior",
                                  "click_rate_this_year", "click_rate_prior")}
    r = rows[0]
    return {
        "leads": _i(r["leads"]),
        "converted": _i(r["converted"]),
        "conversion_rate": _f(r["conversion_rate"]),
        "revenue": _f(r["revenue"]),
        "avg_days_to_convert": _f(r["avg_days_to_convert"]),
        "avg_opens": _f(r["avg_opens"]),
        "avg_clicks": _f(r["avg_clicks"]),
        "leads_this_year": _i(r["leads_this_year"]),
        "converted_this_year": _i(r["converted_this_year"]),
        "open_rate_this_year": _f(r["open_rate_this_year"]),
        "open_rate_prior": _f(r["open_rate_prior"]),
        "click_rate_this_year": _f(r["click_rate_this_year"]),
        "click_rate_prior": _f(r["click_rate_prior"]),
    }


def _read_conversion_trend(bq):
    """conversion_trend -> data.conversion_trend[] (the 'why did conversion drop' headline series)."""
    sql = f"""
        SELECT cohort_month, leads, converted, conversion_rate, converted_90d,
               conversion_rate_90d, open_rate, click_rate, avg_emails_sent, mature
        FROM `{PROJECT}.{DATASET}.conversion_trend`
        ORDER BY cohort_month
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "cohort_month": _date(r["cohort_month"]),
            "leads": _i(r["leads"]),
            "converted": _i(r["converted"]),
            "conversion_rate": _f(r["conversion_rate"]),
            "converted_90d": _i(r["converted_90d"]),
            "conversion_rate_90d": _f(r["conversion_rate_90d"]),
            "open_rate": _f(r["open_rate"]),
            "click_rate": _f(r["click_rate"]),
            "avg_emails_sent": _f(r["avg_emails_sent"]),
            "mature": bool(r["mature"]),
        })
    return out


def _read_monthly(bq):
    sql = f"""
        SELECT month, emails_sent, opens, clicks, open_rate, click_rate, active_leads,
               converted_open_rate, nonconverted_open_rate
        FROM `{PROJECT}.{DATASET}.engagement_monthly`
        ORDER BY month
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "month": _date(r["month"]),
            "emails_sent": _i(r["emails_sent"]),
            "opens": _i(r["opens"]),
            "clicks": _i(r["clicks"]),
            "open_rate": _f(r["open_rate"]),
            "click_rate": _f(r["click_rate"]),
            "active_leads": _i(r["active_leads"]),
            "converted_open_rate": _f(r["converted_open_rate"]),
            "nonconverted_open_rate": _f(r["nonconverted_open_rate"]),
        })
    return out


def _read_cohorts(bq):
    sql = f"""
        SELECT cohort, leads, converted, conversion_rate, avg_opens, avg_clicks,
               open_rate, click_rate, revenue
        FROM `{PROJECT}.{DATASET}.cohort_performance`
        ORDER BY cohort
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "cohort": r["cohort"],
            "leads": _i(r["leads"]),
            "converted": _i(r["converted"]),
            "conversion_rate": _f(r["conversion_rate"]),
            "avg_opens": _f(r["avg_opens"]),
            "avg_clicks": _f(r["avg_clicks"]),
            "open_rate": _f(r["open_rate"]),
            "click_rate": _f(r["click_rate"]),
            "revenue": _f(r["revenue"]),
        })
    return out


def _read_leads(bq):
    """quiz_leads -> data.leads[] with the lead's NAME, most recent first."""
    sql = f"""
        SELECT first_name, email, submitted_at, cohort_year, is_converted, revenue_post_quiz,
               order_count_post_quiz, emails_sent, opens, clicks, open_rate, click_rate,
               days_to_convert, last_open_at
        FROM `{PROJECT}.{DATASET}.quiz_leads`
        ORDER BY submitted_at DESC
        LIMIT {LEADS_LIMIT}
    """
    out = []
    for r in bq.query(sql, location=LOC).result():
        out.append({
            "first_name": r["first_name"],
            "email": r["email"],
            "submitted_at": _date(r["submitted_at"]),
            "cohort_year": _i(r["cohort_year"]),
            "is_converted": bool(r["is_converted"]),
            "revenue_post_quiz": _f(r["revenue_post_quiz"]),
            "order_count_post_quiz": _i(r["order_count_post_quiz"]),
            "emails_sent": _i(r["emails_sent"]),
            "opens": _i(r["opens"]),
            "clicks": _i(r["clicks"]),
            "open_rate": _f(r["open_rate"]),
            "click_rate": _f(r["click_rate"]),
            "days_to_convert": r["days_to_convert"] if r["days_to_convert"] is not None else None,
            "last_open_at": _date(r["last_open_at"]),
        })
    return out


def _data_through(observed, monthly):
    stamps = [ts for ts in (observed or {}).values() if ts]
    if stamps:
        return max(stamps)
    if monthly:
        return monthly[-1]["month"]
    return None


def main():
    bq = bigquery.Client(project=PROJECT)
    gcs = storage.Client(project=PROJECT)
    bucket = gcs.bucket(BUCKET)

    observed = freshness.probe_bq_last_modified(bq, GATING_TABLES, LOC)
    watermark = freshness.read_watermark(bucket, WATERMARK_OBJECT)
    forced = os.environ.get("FORCE_REBUILD") == "1"

    if not forced and not freshness.is_stale(observed, watermark):
        print(f"[{CLIENT}] upstream unchanged since watermark -- fresh, no rebuild.")
        return

    print(f"[{CLIENT}] rebuilding (forced={forced}); reading views from {DATASET}.")
    monthly = _read_monthly(bq)
    data = {
        "client": CLIENT,
        "last_updated": _iso_now(),
        "data_through": _data_through(observed, monthly),
        "kpis": _read_kpis(bq),
        "conversion_trend": _read_conversion_trend(bq),
        "monthly": monthly,
        "cohorts": _read_cohorts(bq),
        "leads": _read_leads(bq),
    }

    blob = bucket.blob(DATA_OBJECT)
    blob.cache_control = "no-store"
    blob.upload_from_string(json.dumps(data, separators=(",", ":")), content_type="application/json")
    print(f"[{CLIENT}] uploaded gs://{BUCKET}/{DATA_OBJECT} "
          f"({len(data['conversion_trend'])} cohort-months, {len(monthly)} months, "
          f"{len(data['leads'])} leads).")

    freshness.write_watermark(bucket, WATERMARK_OBJECT, observed)
    print(f"[{CLIENT}] watermark advanced -> {WATERMARK_OBJECT}.")


if __name__ == "__main__":
    main()
