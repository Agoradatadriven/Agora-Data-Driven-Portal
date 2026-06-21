"""Feedback storage -- persist text and voice feedback in the portal's OWN private bucket.

Feedback lands in the same private bucket as the registry (agora-data-driven-platform-dash) under a
`feedback/` prefix, so it inherits the bucket's private ACL and there is no extra resource to stand
up. Each item is timestamped + uuid-suffixed so concurrent submissions never collide.

  feedback/text/<UTC-timestamp>-<uuid>.json   -- a small JSON record (message, subject, meta)
  feedback/voice/<UTC-timestamp>-<uuid>.webm  -- the raw uploaded audio bytes
  feedback/voice/<UTC-timestamp>-<uuid>.json  -- the sidecar record for that audio

Optional AI enrichment (transcription / summarisation) is handled separately in feedback_ai.py and
is a no-op unless explicitly configured; this module never hard-depends on it.
"""

import datetime
import json
import os
import uuid

# Same private bucket as the registry (deploy_dash_platform.ps1 sets REGISTRY_BUCKET); feedback
# lands under the feedback/ prefix so it inherits the bucket's private ACL.
FEEDBACK_BUCKET = os.environ.get("REGISTRY_BUCKET", "agora-data-driven-platform-dash")
FEEDBACK_PREFIX = "feedback"

# google-cloud-storage is imported LAZILY and the client built on first use, so importing this
# module (and the whole app) never needs the package or ADC -- the portal can boot locally with no
# GCP access. Feedback submission is the only path that actually touches GCS.
_storage_client = None


def _client():
    """Lazily construct and cache the GCS client (so module import never needs ADC)."""
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage  # lazy: only feedback submission needs the package
        _storage_client = storage.Client()
    return _storage_client


def _now_iso():
    """UTC, second precision, ISO 8601 with a trailing Z -- matches the rest of the contract."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stamp():
    """A filesystem-safe sortable stamp for object names: YYYYMMDDTHHMMSSZ."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _bucket():
    return _client().bucket(FEEDBACK_BUCKET)


def save_text_feedback(message, subject="", extra=None):
    """Store a text feedback record as JSON and return its object name.

    `subject` is the logged-in portal user (opaque id / email). `extra` is an optional dict of any
    additional context (e.g. enrichment results) merged into the record.
    """
    record = {
        "type": "text",
        "received_at": _now_iso(),
        "subject": subject or "",
        "message": message or "",
    }
    if extra:
        record.update(extra)

    name = "%s/text/%s-%s.json" % (FEEDBACK_PREFIX, _stamp(), uuid.uuid4().hex)
    blob = _bucket().blob(name)
    blob.upload_from_string(
        json.dumps(record, indent=2, sort_keys=True).encode("utf-8"),
        content_type="application/json",
    )
    return name


def save_voice_feedback(audio_bytes, content_type="audio/webm", subject="", extra=None):
    """Store raw voice feedback bytes + a JSON sidecar; return (audio_name, record_name).

    The audio is written verbatim; the sidecar captures who/when and any enrichment (e.g. a later
    transcript). Keeping them as two objects lets enrichment update the sidecar without rewriting
    the audio.
    """
    base = "%s/voice/%s-%s" % (FEEDBACK_PREFIX, _stamp(), uuid.uuid4().hex)
    audio_name = base + ".webm"
    record_name = base + ".json"

    _bucket().blob(audio_name).upload_from_string(
        audio_bytes or b"", content_type=content_type,
    )

    record = {
        "type": "voice",
        "received_at": _now_iso(),
        "subject": subject or "",
        "audio_object": audio_name,
        "content_type": content_type,
    }
    if extra:
        record.update(extra)

    _bucket().blob(record_name).upload_from_string(
        json.dumps(record, indent=2, sort_keys=True).encode("utf-8"),
        content_type="application/json",
    )
    return audio_name, record_name
