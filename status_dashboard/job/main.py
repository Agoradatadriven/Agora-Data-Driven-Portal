"""Status dashboard EXPORT job -- the agency-wide freshness MONITOR.

Unlike a per-client export job, this job has NO BigQuery dataset and NO SQL views of its own. It is
a META dashboard: it watches every client's exported data JSON and reports how fresh each one is.

What it produces:
  A single private JSON object `status.json` in the status bucket
  (`agora-data-driven-status-dash`), shaped:

    {
      "generated_at": "<ISO build time, UTC second precision>",
      "clients": [
        {
          "client":           "<client key derived from the bucket name>",
          "last_updated":      "<data.last_updated from that client's <key>.json>",
          "data_through":      "<data.data_through from that client's <key>.json>",
          "last_json_update":  "<GCS updated time of the <key>.json blob>",
          "lag_minutes":       <int minutes between last_json_update and now>,
          "stale":             <bool: lag exceeds STALE_AFTER_MINUTES>
        },
        ...
      ]
    }

Self-gating freshness (same contract as the client export jobs):
  This job runs on a `*/15` Cloud Scheduler tick but only rebuilds when the shared raw_windsor
  mirror table(s) it gates on (GATING_TABLES) advanced past the `_freshness.json` watermark stored
  in the STATUS bucket. We gate on `raw_windsor.metrics_daily` -- the same base Windsor mirror that
  drives the client exports -- so the status page refreshes on roughly the same cadence the client
  data does, rather than rebuilding blindly every tick. `FORCE_REBUILD=1` bypasses the gate (used
  for view-only / code / seed changes, which do NOT advance the upstream watermark and would
  otherwise no-op and serve stale status JSON). The watermark is written ONLY after a successful
  upload -- see the end of main().

How it enumerates clients (no registry, no database):
  It LISTS GCS buckets matching `agora-data-driven-*-dash` and EXCLUDES the platform bucket
  (`agora-data-driven-platform-dash`) and its OWN status bucket (`agora-data-driven-status-dash`),
  deriving each client key from the bucket name. For each client bucket it reads that bucket's
  `_freshness.json` (the client's own watermark) and the `<key>.json` data blob.

IAM note:
  The status job service account (status-dash-job@) needs roles/storage.objectViewer on EVERY
  client bucket so it can read each client's `<key>.json` + `_freshness.json`. deploy_status.ps1
  grants that by iterating the existing `agora-data-driven-*-dash` client buckets. When a NEW client
  bucket is created later, re-run deploy_status.ps1 (or grant objectViewer to status-dash-job@ on
  the new bucket) so the monitor can see it.
"""

import json
import os
from datetime import datetime, timezone

from google.cloud import bigquery, storage

import freshness

# --- Fixed project constants (use literally; one project, one region, never another) ---
PROJECT = "agora-data-driven"
LOC = "asia-southeast1"  # Singapore. Everything lives here, never another region.

# --- This job's OWN bucket + objects (the status page is private, like every dashboard) ---
BUCKET = "agora-data-driven-status-dash"
DATA_OBJECT = "status.json"
WATERMARK_OBJECT = "_freshness.json"

# The BASE Windsor mirror table(s) we gate on. We watermark THIS -- NEVER a view. A view has no
# last_modified of its own; freshness must probe the raw_windsor base/mirror tables. We gate on the
# same base mirror the client exports read so the status page advances on the same upstream cadence.
GATING_TABLES = ["raw_windsor.metrics_daily"]

# Bucket-naming convention: client data buckets are `agora-data-driven-<c>-dash`. We enumerate
# clients by listing those, then EXCLUDE the platform + status buckets (they are not clients).
BUCKET_PREFIX = "agora-data-driven-"
BUCKET_SUFFIX = "-dash"
EXCLUDED_BUCKETS = {
    "agora-data-driven-platform-dash",  # the portal/CRM registry bucket -- not a client
    "agora-data-driven-status-dash",    # this monitor's OWN bucket -- not a client
}

# A client is "stale" if its data JSON has not been refreshed within this many minutes. The client
# export tick is */10 and self-gates, so a healthy client refreshes well inside this window; beyond
# it, something upstream (Windsor pull) or the export job is likely wedged.
STALE_AFTER_MINUTES = 120


def _iso_now():
    """Current UTC time as a second-precision ISO-8601 string (build timestamp)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _client_key_from_bucket(bucket_name):
    """Derive a client key from a data-bucket name (`agora-data-driven-<c>-dash` -> `<c>`).

    Returns None if the name does not fit the convention, so non-matching buckets are skipped.
    """
    if not bucket_name.startswith(BUCKET_PREFIX) or not bucket_name.endswith(BUCKET_SUFFIX):
        return None
    middle = bucket_name[len(BUCKET_PREFIX):-len(BUCKET_SUFFIX)]
    return middle or None


def _read_json_blob(bucket, object_name):
    """Read a JSON object from GCS, or {} if it is absent / unparseable.

    A missing or corrupt blob must never crash the monitor -- a client that has not exported yet, or
    whose JSON is mid-write, should simply show up with blank fields rather than failing the whole
    status build.
    """
    blob = bucket.blob(object_name)
    if not blob.exists():
        return {}
    try:
        return json.loads(blob.download_as_text())
    except (ValueError, UnicodeDecodeError):
        return {}


def _blob_updated_iso(bucket, object_name):
    """Return the GCS `updated` time of an object as a second-precision UTC ISO string, or None.

    We must reload the blob to populate its metadata (bucket.blob() returns a bare reference). The
    `updated` time is when the export job last wrote `<key>.json`, which is our truest "last refresh"
    signal -- independent of whatever the client put inside `last_updated`.
    """
    blob = bucket.blob(object_name)
    if not blob.exists():
        return None
    blob.reload()
    return freshness._to_utc_seconds(blob.updated)


def _lag_minutes(last_json_update_iso, now_dt):
    """Whole minutes between a client's last JSON write and `now`, or None if unknown."""
    if not last_json_update_iso:
        return None
    then = datetime.fromisoformat(last_json_update_iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    delta = now_dt - then
    return int(delta.total_seconds() // 60)


def _collect_clients(gcs, now_dt):
    """List client data buckets and assemble one status row per client.

    For each `agora-data-driven-<c>-dash` bucket (excluding platform + status), read the `<key>.json`
    data blob (for last_updated / data_through), its GCS `updated` time (last_json_update), and
    derive lag + a stale flag. The client's own `_freshness.json` watermark is read too -- its
    presence confirms the export job has run at least once against that bucket.
    """
    rows = []
    for b in gcs.list_buckets(project=PROJECT, prefix=BUCKET_PREFIX):
        bucket_name = b.name
        if bucket_name in EXCLUDED_BUCKETS:
            continue
        client = _client_key_from_bucket(bucket_name)
        if not client:
            continue

        bucket = gcs.bucket(bucket_name)
        data_object = "%s.json" % client

        data = _read_json_blob(bucket, data_object)
        # The client's own watermark sidecar; read for completeness / to confirm the export ran.
        _watermark = freshness.read_watermark(bucket, WATERMARK_OBJECT)

        last_json_update = _blob_updated_iso(bucket, data_object)
        lag = _lag_minutes(last_json_update, now_dt)
        stale = (lag is None) or (lag > STALE_AFTER_MINUTES)

        rows.append({
            "client": client,
            "last_updated": data.get("last_updated"),
            "data_through": data.get("data_through"),
            "last_json_update": last_json_update,
            "lag_minutes": lag,
            "stale": stale,
        })

    # Stable, predictable ordering for the dashboard table.
    rows.sort(key=lambda r: r["client"])
    return rows


def main():
    bq = bigquery.Client(project=PROJECT)
    gcs = storage.Client(project=PROJECT)
    status_bucket = gcs.bucket(BUCKET)

    # Probe the BASE raw_windsor mirror table(s) we gate on, then compare against the watermark
    # stored in the STATUS bucket.
    observed = freshness.probe_bq_last_modified(bq, GATING_TABLES, LOC)
    watermark = freshness.read_watermark(status_bucket, WATERMARK_OBJECT)

    # FORCE_REBUILD=1 bypasses the freshness gate -- used for view-only / code / seed changes that
    # do not advance the upstream watermark and would otherwise no-op and keep serving stale JSON.
    forced = os.environ.get("FORCE_REBUILD") == "1"

    if not forced and not freshness.is_stale(observed, watermark):
        print("[status] upstream unchanged since watermark -- fresh, no rebuild.")
        return

    print("[status] rebuilding (forced=%s); scanning client buckets." % forced)
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    clients = _collect_clients(gcs, now_dt)

    data = {
        "generated_at": _iso_now(),
        "clients": clients,
    }

    # Upload the private status JSON. cache_control no-store: the status dash proxies this per
    # request to authed sessions only, so it must never be cached at any edge.
    blob = status_bucket.blob(DATA_OBJECT)
    blob.cache_control = "no-store"
    blob.upload_from_string(
        json.dumps(data, separators=(",", ":")),
        content_type="application/json",
    )
    print("[status] uploaded gs://%s/%s (%d clients)." % (BUCKET, DATA_OBJECT, len(clients)))

    # Advance the watermark ONLY AFTER the upload succeeded. If the upload had raised, we would NOT
    # reach this line, so a failed build never advances the watermark -- the next tick retries.
    freshness.write_watermark(status_bucket, WATERMARK_OBJECT, observed)
    print("[status] watermark advanced -> %s." % WATERMARK_OBJECT)


if __name__ == "__main__":
    main()
