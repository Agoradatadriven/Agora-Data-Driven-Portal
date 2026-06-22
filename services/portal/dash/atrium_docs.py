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
    """True iff the private-doc (Drive API) path is switched on. Fail-closed otherwise."""
    return os.environ.get("ATRIUM_DOCS_ENABLED", "") in ("1", "true", "True")


def _no_public_fetch():
    """True iff the keyless public-doc fetch is switched OFF (locked-down deploys / hermetic tests)."""
    return os.environ.get("ATRIUM_DOCS_NO_PUBLIC_FETCH", "") in ("1", "true", "True")


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


def _fetch_via_drive(doc_id):
    """Private-doc path: export the doc via the Drive API using the runtime SA's ADC. None on any error.

    The Google API client is imported LAZILY so an unconfigured deploy has no hard dependency on it.
    """
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
        return None


def _fetch_via_public_export(doc_id):
    """Public-doc path: the keyless export endpoint for a doc shared 'anyone with the link'. None otherwise.

    Needs no API, key, or service account -- just an outbound GET to docs.google.com -- so a shared
    doc summarises even in local preview / a default deploy. A non-public doc redirects to a Google
    sign-in HTML page, so we accept ONLY a text/plain body and reject anything else as unreadable.
    The doc id is validated (doc_id_from_url) and the host is hard-coded, so there is no SSRF surface.
    """
    if _no_public_fetch():
        return None
    try:
        import requests  # already a portal dependency (used by the dashboard proxy)

        url = "https://docs.google.com/document/d/%s/export?format=txt" % doc_id
        resp = requests.get(url, timeout=10, allow_redirects=True,
                            headers={"User-Agent": "AgoraAtrium/1.0"})
        if resp.status_code != 200:
            return None
        if "text/plain" not in resp.headers.get("Content-Type", "").lower():
            return None
        text = (resp.text or "").strip()
        if not text or text.startswith("<"):   # an HTML sign-in/error page is not the doc
            return None
        return text[:100000]
    except Exception:
        return None


def fetch_doc_text(doc_url):
    """Return the plain text of a Google Doc, or None if it cannot be read.

    Two keyless-by-default tiers, each fail-closed (a missing package, an unshared doc, or a network
    error never raises into the request path -- the caller degrades):
      1. PRIVATE docs -- the Drive API using the runtime SA's ADC. Opt-in (ATRIUM_DOCS_ENABLED +
         google-api-python-client + the doc shared with that SA); tried first when enabled.
      2. PUBLIC docs ('anyone with the link can view') -- the keyless export endpoint. No infra, so a
         pasted link works in local preview and a default deploy. Disable with ATRIUM_DOCS_NO_PUBLIC_FETCH=1.
    """
    doc_id = doc_id_from_url(doc_url)
    if not doc_id:
        return None
    if _enabled():
        text = _fetch_via_drive(doc_id)
        if text:
            return text
    return _fetch_via_public_export(doc_id)


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


def generate_strategy(doc_url):
    """Produce a campaign's three strategy sections from a strategy doc. Returns (strategy, source).

    strategy is a dict {"what", "why", "next"} of client-facing paragraphs (mapping positionally to
    the Insight / Action / What to do next? columns), or None when the doc could not be read.
    source is one of:
      "ai"      -- Claude wrote the three sections from the doc text,
      "excerpt" -- AI is off, so we put a trimmed excerpt of the doc in "what" (why/next blank),
      "none"    -- the doc could not be read (disabled / unshared / no URL).
    Never raises: every failure degrades so the caller can still create the campaign (with the doc
    link saved) and let the admin fill the sections by hand.
    """
    text = fetch_doc_text(doc_url)
    if not text:
        return None, "none"
    try:
        import feedback_ai
        sections = feedback_ai.summarize_strategy_sections(text)
    except Exception:
        sections = None
    if sections:
        return {"what": sections.get("what", ""), "why": sections.get("why", ""),
                "next": sections.get("next", "")}, "ai"
    return {"what": _excerpt(text), "why": "", "next": ""}, "excerpt"
