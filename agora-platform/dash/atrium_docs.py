"""Read a strategy Google Doc and turn it into an Atrium AI summary (optional + graceful).

Atrium lets an admin attach a Google Doc to a campaign's strategy; the "Generate AI summary from
doc" action reads that doc and writes the client-facing AI summary. Like feedback_ai.py, this is
DELIBERATELY a graceful no-op chain unless explicitly configured -- the portal must deploy and run
with no Docs/AI wired at all.

Two independent, optional capabilities, each fail-closed:
  1. Doc fetch (this module) -- gated on ATRIUM_DOCS_ENABLED=1. Reads the doc text via the Google
     Drive API using the runtime service account's Application Default Credentials. The doc must be
     shared with that SA (we do NOT use domain-wide delegation). `googleapiclient` is imported
     LAZILY, so it is NOT a hard dependency: if the package is absent or the SA can't read the doc,
     fetch returns None.
  2. Summarisation (feedback_ai.summarize_strategy) -- gated on FEEDBACK_AI_ENABLED + ANTHROPIC_API_KEY.

`generate_summary(doc_url)` ties them together and ALWAYS degrades gracefully:
  * Claude available  -> an AI-written summary            (source "ai")
  * doc readable only -> a trimmed excerpt of the doc     (source "excerpt")
  * neither           -> ("", "none")                     (the admin can still type a summary by hand)

NOTE: enabling capability (1) is the documented, opt-in deviation from Atrium's "no new infra" rule:
it requires enabling the Docs/Drive API, adding `google-api-python-client` to requirements.txt, and
sharing the strategy doc with the platform-dash runtime SA. A default deploy is unaffected.
"""

import os
import re

# Drive's read-only scope is all we need to export a doc to plain text.
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# How much of the doc to keep when falling back to a plain excerpt (no AI).
_EXCERPT_CHARS = 600


def _enabled():
    """True iff doc fetching is switched on. Fail-closed otherwise."""
    return os.environ.get("ATRIUM_DOCS_ENABLED", "") in ("1", "true", "True")


def doc_id_from_url(doc_url):
    """Extract the Google Doc id from a share/edit URL (or accept a bare id). None if not found."""
    if not doc_url:
        return None
    text = doc_url.strip()
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
        return text
    return None


def fetch_doc_text(doc_url):
    """Return the plain text of a Google Doc, or None if disabled / unreadable / SDK missing.

    Uses the runtime SA's ADC with the Drive read-only scope and exports the doc to text/plain. The
    Google API client is imported LAZILY so an unconfigured deploy has no hard dependency on it.
    """
    if not _enabled():
        return None
    doc_id = doc_id_from_url(doc_url)
    if not doc_id:
        return None
    try:
        import google.auth  # part of google-cloud-* deps already present
        from googleapiclient.discovery import build  # optional; absent on a default deploy

        creds, _project = google.auth.default(scopes=[_DRIVE_SCOPE])
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        data = service.files().export(fileId=doc_id, mimeType="text/plain").execute()
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        return (data or "").strip() or None
    except Exception:
        # Best-effort: a missing package, an unshared doc, or a network error must never raise into
        # the request path -- the caller falls back to a hand-typed summary.
        return None


def _excerpt(text):
    """A short, clean excerpt of the doc for the no-AI fallback (first paragraphs, trimmed)."""
    collapsed = re.sub(r"\n{2,}", "\n", (text or "").strip())
    if len(collapsed) <= _EXCERPT_CHARS:
        return collapsed
    cut = collapsed[:_EXCERPT_CHARS]
    # Prefer to break on a sentence/space boundary rather than mid-word.
    for sep in (". ", "\n", " "):
        idx = cut.rfind(sep)
        if idx > _EXCERPT_CHARS // 2:
            return cut[: idx + (1 if sep == ". " else 0)].strip() + " …"
    return cut.strip() + " …"


def generate_summary(doc_url):
    """Produce an AI summary from a strategy doc. Returns (summary, source).

    source is one of:
      "ai"      -- Claude wrote the summary from the doc text,
      "excerpt" -- AI is off, so we returned a trimmed excerpt of the doc,
      "none"    -- the doc could not be read (disabled / unshared / no URL).
    Never raises: every failure degrades to ("", "none") so the action stays usable.
    """
    text = fetch_doc_text(doc_url)
    if not text:
        return "", "none"
    try:
        import feedback_ai
        summary = feedback_ai.summarize_strategy(text)
    except Exception:
        summary = None
    if summary:
        return summary, "ai"
    return _excerpt(text), "excerpt"
