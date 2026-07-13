"""Shared self-gating freshness helper (vendored identically into every export job).

The contract (see the root CLAUDE.md "Freshness contract"):
  * Every export job runs on a Cloud Scheduler tick but only rebuilds when the upstream tables it
    reads (the shared `raw_windsor` mirror tables) have advanced past a stored watermark.
  * The watermark is a `_freshness.json` sidecar in the client's OWN GCS bucket.
  * Probe the BASE / MIRROR tables the views read -- NEVER watermark a VIEW (a view has no
    last-modified time of its own; watermark the raw_* tables the views select from).
  * Write the watermark only AFTER a successful data upload.

Why the consumers self-gate (and not the loaders):
  The ingest jobs are scheduled API pulls -- they are the WRITERS of `raw_windsor`, so there
  is nothing upstream of them to gate against. The self-gating lives in the CONSUMERS: each client
  export job (and the status dashboard) runs every few minutes and rebuilds only when the ingest
  pull advanced the `raw_windsor` mirror tables it depends on.

Design notes:
  * No heavy imports at module top level. `google-cloud-storage` is imported lazily inside the
    watermark helpers so a job that only probes BigQuery never pays for it. The BigQuery client is
    PASSED IN by the caller (this module never constructs it).
  * All timestamps are normalized to second-precision UTC ISO-8601 strings so that watermark
    comparisons are exact and JSON-stable across runs.
"""

from datetime import datetime, timezone

BQ_LOCATION = "asia-southeast1"  # Singapore. Everything lives in one region, never another.


def _to_utc_seconds(value):
    """Normalize a datetime / epoch-seconds / ISO string to a second-precision UTC ISO string.

    Returns None for falsy input so callers can skip absent probes cleanly.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    else:
        # Assume ISO-8601 string; tolerate a trailing 'Z'.
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat()


def probe_bq_last_modified(bq, tables, location=BQ_LOCATION):
    """Return {"dataset.table": last_modified_iso} for the given BigQuery base/mirror tables.

    Reads __TABLES__.last_modified_time (epoch millis), grouped per dataset so we issue one query
    per dataset rather than one per table. `tables` is an iterable of "dataset.table" strings (for
    Agora these are the shared `raw_windsor.*` mirror tables). `bq` is a
    google.cloud.bigquery.Client supplied by the caller.

    NEVER pass a view here -- __TABLES__ has no meaningful last_modified for views; watermark the
    base/mirror tables the views read.
    """
    by_dataset = {}
    for fq in tables or []:
        if "." not in fq:
            continue
        dataset, table = fq.split(".", 1)
        by_dataset.setdefault(dataset, set()).add(table)

    observed = {}
    for dataset, wanted in by_dataset.items():
        sql = (
            "SELECT table_id, last_modified_time "
            "FROM `%s.__TABLES__`" % dataset
        )
        for row in bq.query(sql, location=location).result():
            if row["table_id"] in wanted:
                key = "%s.%s" % (dataset, row["table_id"])
                # last_modified_time is epoch milliseconds.
                observed[key] = _to_utc_seconds(float(row["last_modified_time"]) / 1000.0)
    return observed


def read_watermark(bucket, object_name):
    """Read the JSON watermark sidecar from GCS. Returns {} if it does not exist yet.

    `bucket` is a google.cloud.storage.Bucket. storage is imported lazily so a job that never
    touches GCS does not pay the import cost.
    """
    import json  # local import keeps the module top clean

    blob = bucket.blob(object_name)
    if not blob.exists():
        return {}
    try:
        return json.loads(blob.download_as_text())
    except (ValueError, UnicodeDecodeError):
        # A corrupt sidecar should not crash the job -- treat it as "no watermark".
        return {}


def write_watermark(bucket, object_name, observed):
    """Write the observed-timestamps dict as the JSON watermark sidecar in GCS.

    Call this ONLY after a successful data upload, so a failed build never advances the watermark.
    """
    import json

    blob = bucket.blob(object_name)
    blob.cache_control = "no-store"
    blob.upload_from_string(
        json.dumps(observed, sort_keys=True, indent=2),
        content_type="application/json",
    )


def is_stale(observed, watermark):
    """True if a rebuild is warranted.

    Stale iff ANY observed key is newer than the stored watermark, OR an observed key is absent
    from the watermark (a newly-tracked upstream table). An EMPTY `observed` returns False so a
    broken/empty probe never burns a rebuild -- we would rather serve slightly stale data than
    rebuild blindly on a probe failure.
    """
    if not observed:
        return False
    for key, ts in observed.items():
        if ts is None:
            continue
        previous = (watermark or {}).get(key)
        if previous is None:
            return True
        if _to_utc_seconds(ts) > _to_utc_seconds(previous):
            return True
    return False
