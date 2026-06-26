"""Daily Market Intelligence refresh -- pull REAL news into every client's Atrium intel tab.

Runs as a Cloud Run JOB (`intel-refresh`) on a daily Cloud Scheduler tick, REUSING the platform-dash
image + runtime SA. No new service/bucket/SA/secret: it writes the SAME `workspace/<c>.json` objects
the app already does (the platform-dash web SA has objectAdmin on the registry bucket), and it reads
public RSS over HTTPS (intel_feed) -- no API key.

For each client it rebuilds two sections from real headlines + real publisher links + real dates:
  * Media Buying News  -- universal: fixed publisher feeds + Google-News ad-platform queries.
  * Business Research  -- per client: Google-News searches over the client's own `intel_topics`
    keywords (set by the team in the workspace), falling back to a generic marketing set.
It REPLACES each section's AUTO entries (workspace.replace_auto_intel) and PRESERVES anything the
team added or edited by hand. Newest items win; each section is capped so the tab stays a briefing.

Gated + graceful, like feedback_ai: it only does anything when INTEL_AUTO_ENABLED=1, and a dead
feed / a client with no workspace is logged and skipped -- never fatal. Off-cloud testable via
WORKSPACE_LOCAL_DIR + REGISTRY_LOCAL_DIR (the same backends the app uses), and `refresh_client`
takes an injectable `fetcher` so the whole pipeline runs with no network in tests.
"""

import os
import sys

import intel_feed
import store
import workspace

# --- What each section pulls --------------------------------------------------------------------
# Media Buying News is universal -- the same ad-platform updates matter to every client. A couple of
# stable publisher RSS feeds + Google-News queries for the big platforms. (Publisher feed URLs are
# the vendors' own RSS; if one ever dies, fetch_feed just returns [] and the queries still fill in.)
MEDIA_BUYING_FEEDS = (
    "https://searchengineland.com/category/ppc/feed",
)
MEDIA_BUYING_QUERIES = (
    "Google Ads update",
    "Meta Ads Manager update",
    "TikTok advertising",
)

# Business Research is universal + automatic too: fixed reputable marketing/industry publisher feeds
# (keyless RSS) + Google-News queries over evergreen marketing topics. No per-client keywords -- the
# tab fills itself daily from these legit sources; the team can still add/edit curated entries by hand.
BUSINESS_RESEARCH_FEEDS = (
    "https://www.marketingdive.com/feeds/news/",
    "https://www.searchenginejournal.com/feed/",
)
BUSINESS_RESEARCH_QUERIES = (
    "digital marketing industry trends",
    "advertising industry news",
    "consumer marketing trends",
)

# Per-section caps + the heading/source defaults that make an auto entry read like the hand-written
# ones (see the placeholders in atrium_view._INTEL_SECTIONS).
_PER_SECTION = 6
_BUSINESS_HEADING = "Industry News"
_MEDIA_HEADING = "Platform Update"
_BODY_MAX = 280


def _enabled():
    """True iff the daily auto-refresh is switched on. Fail-closed (default OFF), like feedback_ai."""
    return os.environ.get("INTEL_AUTO_ENABLED", "") in ("1", "true", "True")


def _dedupe(rows):
    """Drop entries that repeat a title or link (feeds + queries overlap), preserving order."""
    out, seen = [], set()
    for r in rows:
        key = (r.get("title") or "").strip().lower() or (r.get("link") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _to_entry(row, heading):
    """Shape a parsed feed row into an intel entry dict (heading/title/body/source/link/date)."""
    body = (row.get("body") or "").strip()
    # A description that just echoes the headline adds nothing -- drop it so the card stays clean.
    if body and row.get("title") and body.lower().startswith(row["title"].strip().lower()):
        body = ""
    if len(body) > _BODY_MAX:
        body = body[:_BODY_MAX].rsplit(" ", 1)[0] + "…"
    return {
        "heading": heading,
        "title": (row.get("title") or "").strip(),
        "body": body,
        "source": (row.get("source") or "").strip(),
        "link": (row.get("link") or "").strip(),
        "date": (row.get("date") or "").strip(),
    }


def _gather(feeds, queries, heading, limit, fetcher=None):
    """Fetch every feed + query, dedupe, sort newest-first, and return up to `limit` intel entries."""
    rows = []
    for url in feeds:
        rows.extend(intel_feed.fetch_feed(url, limit=limit + 2, fetcher=fetcher))
    for q in queries:
        url = intel_feed.google_news_url(q)
        rows.extend(intel_feed.fetch_feed(url, limit=limit + 2, fetcher=fetcher))
    rows = _dedupe([r for r in rows if r.get("title")])
    # Newest first; dateless entries fall to the bottom (mirrors atrium_view.intel_sections).
    rows.sort(key=lambda r: (r.get("date") or ""), reverse=True)
    return [_to_entry(r, heading) for r in rows[:limit]]


def refresh_client(client, ws=None, fetcher=None):
    """Rebuild both auto sections for one client. Returns {'media_buying': n, 'business_research': n}.

    `ws` may be passed to avoid a reload; `fetcher` is the intel_feed injection seam for tests.
    Skips (returns zeros) if the client has no workspace yet."""
    if ws is None:
        ws = workspace.load_workspace(client)
    if ws is None:
        return {"media_buying": 0, "business_research": 0}

    media = _gather(MEDIA_BUYING_FEEDS, MEDIA_BUYING_QUERIES, _MEDIA_HEADING, _PER_SECTION, fetcher)
    business = _gather(BUSINESS_RESEARCH_FEEDS, BUSINESS_RESEARCH_QUERIES,
                       _BUSINESS_HEADING, _PER_SECTION, fetcher)

    # Only replace a section when we actually pulled something, so a transient feed outage never
    # wipes yesterday's still-useful auto entries.
    if media:
        workspace.replace_auto_intel(client, "media_buying", media)
    if business:
        workspace.replace_auto_intel(client, "business_research", business)
    return {"media_buying": len(media), "business_research": len(business)}


def refresh_all(fetcher=None):
    """Refresh every registered client (skipping the worked-example `template`). Returns a summary."""
    summary = {}
    for c in store.list_clients():
        key = c.get("key")
        if not key or key == "template":
            continue
        try:
            counts = refresh_client(key, fetcher=fetcher)
        except Exception as exc:  # one bad client must not sink the whole run
            print("[intel-refresh] %s FAILED: %s" % (key, exc), file=sys.stderr)
            continue
        summary[key] = counts
        print("[intel-refresh] %s -> media_buying=%d business_research=%d"
              % (key, counts["media_buying"], counts["business_research"]))
    return summary


def main():
    """Job entry point. No-op (logs why) unless INTEL_AUTO_ENABLED=1."""
    if not _enabled():
        print("[intel-refresh] disabled (set INTEL_AUTO_ENABLED=1 to run); nothing to do.")
        return
    print("[intel-refresh] starting daily refresh")
    summary = refresh_all()
    print("[intel-refresh] done -- %d client(s) refreshed" % len(summary))


if __name__ == "__main__":
    main()
