"""Admin "Sync all dashboards" — trigger every <c>-export Cloud Run job on demand (no scheduler).

Mirrors the Bidbrain platform's /sync-all: the portal's OWN service account lists the client export
jobs and POSTs each `:run` via the Cloud Run Admin API, then records the sync time. New clients are
picked up automatically because jobs are DISCOVERED (any job whose name ends in `-export`), never
hardcoded — so onboarding a client needs no change here.

The last-sync stamp is one small private JSON (`sync_state.json`) in the registry bucket, mirroring
store.py (GCS by default; local filesystem when REGISTRY_LOCAL_DIR is set) so it works off-cloud too.
Everything degrades gracefully — no credentials / no jobs / a failed trigger never raises into the
console (the button just reports what it could and couldn't do).
"""
import datetime
import json
import os

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "agora-data-driven")
REGION = os.environ.get("REGION", "asia-southeast1")
REGISTRY_BUCKET = os.environ.get("REGISTRY_BUCKET", "agora-data-driven-platform-dash")
SYNC_OBJECT = os.environ.get("SYNC_OBJECT", "sync_state.json")

_storage_client = None


def _local_dir():
    return os.environ.get("REGISTRY_LOCAL_DIR", "")


def _blob():
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage  # lazy: only the GCS backend needs the package
        _storage_client = storage.Client()
    return _storage_client.bucket(REGISTRY_BUCKET).blob(SYNC_OBJECT)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_state():
    """The last recorded sync ({} if never synced / unreadable — never raises)."""
    try:
        local = _local_dir()
        if local:
            path = os.path.join(local, SYNC_OBJECT)
            if not os.path.isfile(path):
                return {}
            with open(path, "r", encoding="utf-8") as fh:
                return json.loads(fh.read())
        blob = _blob()
        if not blob.exists():
            return {}
        return json.loads(blob.download_as_bytes().decode("utf-8"))
    except Exception:
        return {}


def write_state(state):
    body = json.dumps(state, indent=2).encode("utf-8")
    try:
        local = _local_dir()
        if local:
            path = os.path.join(local, SYNC_OBJECT)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(body)
            return
        _blob().upload_from_string(body, content_type="application/json")
    except Exception:
        pass  # best-effort; a persistence miss must not fail the sync


def _session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def list_export_jobs(sess):
    """Every Cloud Run job in this project/region whose name ends in `-export`."""
    base = "https://run.googleapis.com/v2/projects/%s/locations/%s/jobs" % (PROJECT, REGION)
    names, page = [], ""
    while True:
        url = base + (("?pageToken=" + page) if page else "")
        r = sess.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        for j in data.get("jobs", []):
            short = (j.get("name") or "").split("/")[-1]
            if short.endswith("-export"):
                names.append(short)
        page = data.get("nextPageToken") or ""
        if not page:
            break
    return sorted(names)


def trigger_all():
    """Discover + trigger every <c>-export job. Returns (triggered[], failed[], iso_ts).

    `failed` entries are {"job", "error"}. Records the stamp regardless so "last synced" advances
    to when the operator pressed the button (the dashboards then rebuild over the next minute or two).
    """
    ts = _now()
    triggered, failed = [], []
    try:
        sess = _session()
        jobs = list_export_jobs(sess)
    except Exception as e:  # noqa: BLE001 — no creds / API off: report, don't crash the console
        raw = str(e)
        low = raw.lower()
        # Turn the raw auth/library exception into something an operator can act on. The most common
        # cause off-cloud (and the local-preview case) is simply "no Google credentials".
        if "default credentials" in low or "could not automatically determine" in low \
                or "google.auth" in low or "adc" in low:
            friendly = ("Couldn't reach Google Cloud (no credentials on this environment). "
                        "This is expected in local preview; on the live deploy it means the service "
                        "account can't list the export jobs.")
        elif "permission" in low or "403" in low or "forbidden" in low:
            friendly = ("Google Cloud denied access (permission) — the service account is missing "
                        "run.jobs.list / run.jobs.run on the export jobs.")
        else:
            friendly = "Couldn't start the sync: " + raw[:180]
        return [], [{"job": "(job discovery)", "error": friendly}], ts

    for job in jobs:
        url = "https://run.googleapis.com/v2/projects/%s/locations/%s/jobs/%s:run" % (PROJECT, REGION, job)
        # FORCE_REBUILD is a no-op for the always-fresh Windsor jobs, but keeps parity with the
        # BigQuery-gated jobs (which need it to bypass their freshness watermark).
        body = {"overrides": {"containerOverrides": [{"env": [{"name": "FORCE_REBUILD", "value": "1"}]}]}}
        try:
            r = sess.post(url, json=body, timeout=30)
            r.raise_for_status()
            triggered.append(job)
        except Exception as e:  # noqa: BLE001
            failed.append({"job": job, "error": str(e)[:200]})

    write_state({"last_sync": ts, "triggered": triggered,
                 "failed": [f["job"] for f in failed], "job_count": len(jobs)})
    return triggered, failed, ts
