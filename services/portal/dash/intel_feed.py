"""Market Intelligence feeds -- turn Google News RSS + publisher feeds into intel entries.

Pure and infra-free, mirroring atrium_health.py: it fetches public RSS/Atom XML over HTTPS with a
LAZY `requests` import (the only live dependency, already pinned) and parses it with the STDLIB
(`xml.etree.ElementTree`) -- so there is NO new dependency and NO API key. Every step degrades
gracefully: a dead/blocked feed yields `[]` (never an exception), so a refresh run can never 500.

Two feed kinds give real headlines + real publisher links + real dates:
  * a Google News SEARCH feed  -> https://news.google.com/rss/search?q=<query>   -- real news for ANY
    keyword, which is what makes Business Research per-client (keyed off the client's own topics).
  * a fixed PUBLISHER feed     -> a vendor's own RSS (e.g. Search Engine Land PPC) -- the universal
    Media Buying News that applies to every client.

Tests inject their own `fetcher` (see _intel_feed_localtest.py), so the parsing/normalisation is
exercised with NO network. The only function that touches the network is `_requests_fetch`.
"""

import datetime
import email.utils
import re
import xml.etree.ElementTree as ET

# A real-browser-ish UA so feeds (and Google News) don't 403 a bare python-requests client. Same
# posture as atrium_health._UA.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_DEFAULT_TIMEOUT = 12  # seconds; a slow feed must never hang the whole refresh run.
_TAG_RE = re.compile(r"<[^>]+>")            # crude HTML-tag stripper for feed descriptions
_WS_RE = re.compile(r"\s+")                  # whitespace collapser
# A bare '&' that is NOT a valid XML entity (e.g. a raw '&nbsp;' or '&' in a title) makes strict XML
# parsing fail. Real feeds emit these; escape them so we can retry instead of dropping the feed.
_BAD_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)")


# --- Building feed URLs -------------------------------------------------------------------------
def google_news_url(query, lang="en", country="US"):
    """The Google News RSS SEARCH url for `query` (keyless, real links). Returns "" for a blank query.

    e.g. google_news_url("RV industry") ->
        https://news.google.com/rss/search?q=RV%20industry&hl=en&gl=US&ceid=US:en
    """
    from urllib.parse import quote  # stdlib; local so importing this module is side-effect free

    q = (query or "").strip()
    if not q:
        return ""
    hl = lang or "en"
    gl = country or "US"
    return (
        "https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s:%s"
        % (quote(q), hl, gl, gl, hl)
    )


# --- Fetching (the ONLY networked function) -----------------------------------------------------
def _requests_fetch(url, timeout):
    """Default fetcher: a single GET following redirects. Lazy `requests` import (live path only)."""
    import requests  # lazy: tests inject their own fetcher, so the import is never needed off-cloud

    return requests.get(
        url,
        timeout=timeout,
        allow_redirects=True,
        headers={"User-Agent": _UA, "Accept": "application/rss+xml,application/xml,text/xml,*/*"},
    )


# --- Parsing (pure stdlib) ----------------------------------------------------------------------
def _localname(tag):
    """The local name of a (possibly namespaced) XML tag: '{ns}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(el):
    """Trimmed text of an element, or "" if it's None/empty."""
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _clean(html):
    """Strip HTML tags and collapse whitespace from a feed description/summary."""
    if not html:
        return ""
    txt = _TAG_RE.sub(" ", html)
    # A couple of the most common entities feeds emit; everything else is left as-is (harmless).
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return _WS_RE.sub(" ", txt).strip()


def _iso_date(raw):
    """Normalise an RSS RFC-822 (`Wed, 24 Jun 2026 10:00:00 GMT`) or Atom ISO date to 'yyyy-mm-dd'.

    Returns "" if the date is absent or unparseable -- a dateless entry is still usable (it just
    sorts to the bottom of the intel list, like any hand-added entry without a date).
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    # RSS pubDate -- RFC 822/2822.
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is not None:
            return dt.date().isoformat()
    except (TypeError, ValueError):
        pass
    # Atom updated/published -- ISO-8601 (allow a trailing Z).
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except ValueError:
        return ""


def _entry_from_item(item):
    """Map one RSS <item> or Atom <entry> element to {title, link, body, source, date}, or None."""
    title = link = body = source = date = ""
    for child in list(item):
        name = _localname(child.tag)
        if name == "title" and not title:
            title = _text(child)
        elif name == "link" and not link:
            # RSS: text node. Atom: href attribute (prefer rel="alternate"/no rel).
            link = _text(child) or child.attrib.get("href", "")
        elif name in ("description", "summary", "content") and not body:
            body = _clean(_text(child))
        elif name == "source" and not source:
            source = _text(child)  # Google News carries the publisher here
        elif name in ("pubDate", "published", "updated") and not date:
            date = _iso_date(_text(child))
    if not (title or body):
        return None
    # Google News titles read "Headline - Publisher"; if we know the source, trim that tail so the
    # headline is clean and the publisher shows once (as the source tag).
    if source and title.endswith(" - " + source):
        title = title[: -(len(source) + 3)].strip()
    return {"title": title, "link": link, "body": body, "source": source, "date": date}


def parse_feed(xml_bytes):
    """Parse RSS or Atom bytes into a list of {title, link, body, source, date} dicts.

    Handles both `<item>` (RSS) and `<entry>` (Atom). The feed's own channel/feed `<title>` is used
    as a fallback `source` for entries that don't carry one (publisher feeds). Returns [] on any
    parse error -- malformed XML never raises out of here.
    """
    if not xml_bytes:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        # Most often a stray/undefined entity (e.g. a raw '&nbsp;'). Escape bad ampersands and retry
        # once before giving up, so a single bad char never costs us the whole feed.
        try:
            text = xml_bytes.decode("utf-8", "replace") if isinstance(xml_bytes, bytes) else xml_bytes
            root = ET.fromstring(_BAD_AMP_RE.sub("&amp;", text))
        except ET.ParseError:
            return []

    # Channel/feed title -> fallback source (publisher feeds put it here, not per-item).
    feed_source = ""
    for el in root.iter():
        if _localname(el.tag) == "title":
            feed_source = _text(el)
            break

    out = []
    for el in root.iter():
        if _localname(el.tag) in ("item", "entry"):
            row = _entry_from_item(el)
            if row is None:
                continue
            if not row["source"]:
                row["source"] = feed_source
            out.append(row)
    return out


# --- The one call a caller needs ----------------------------------------------------------------
def fetch_feed(url, limit=8, timeout=_DEFAULT_TIMEOUT, fetcher=None):
    """Fetch `url` and return up to `limit` normalised entries (newest as the feed orders them).

    Never raises: a network error, a non-200, or unparseable XML all yield []. `fetcher` is an
    injection seam for tests (default: a real `requests` GET); it is called as fetcher(url, timeout)
    and must return an object with `.status_code` and `.content` (a `requests.Response` shape).
    """
    if not url:
        return []
    fn = fetcher or _requests_fetch
    try:
        resp = fn(url, timeout)
    except Exception:
        return []
    status = getattr(resp, "status_code", 0)
    if status and status >= 400:
        return []
    content = getattr(resp, "content", None)
    if content is None:
        text = getattr(resp, "text", "")
        content = text.encode("utf-8") if isinstance(text, str) else b""
    rows = parse_feed(content)
    return rows[: max(0, int(limit))] if limit else rows
