"""Daily Market Intelligence refresh -- an AI brain curates REAL news into every client's intel tab.

Runs as a Cloud Run JOB (`intel-refresh`) on a daily Cloud Scheduler tick, REUSING the platform-dash
image + runtime SA. No new service/bucket/SA: it writes the SAME `workspace/<c>.json` objects the
app already does, and it reads public RSS over HTTPS (intel_feed) -- keyless. The ONLY added infra is
the two provider API keys (GEMINI_API_KEY / DEEPSEEK_API_KEY), mounted from Secret Manager, and even
those are optional (see the fallback below).

RESEARCH METHOD = RETRIEVE-THEN-CURATE (see intel_ai.py). For each client and each of the two
sections we:
  1. RETRIEVE a pool of REAL candidate articles from keyless Google News RSS + publisher feeds
     (intel_feed). Media Buying News is universal (ad-platform queries + Search Engine Land PPC);
     Business Research is per-client, keyed off the client's own `intel_topics` (falling back to a
     generic marketing set). The FIRST run for a client pulls a 12-MONTH window (backfill); every
     run after pulls just the last few days.
  2. CURATE with the client's selected model (intel_ai.curate) -- it picks the most relevant items,
     writes a client-facing 1-2 sentence summary, and keeps the REAL link/source/date. The admin's
     tunable per-section prompt steers what to pick.
  3. REPLACE only the section's AUTO entries (workspace.replace_auto_intel), so hand-added / pinned
     entries are preserved.

Gated + graceful, like feedback_ai: the job is a logged no-op unless INTEL_AUTO_ENABLED=1. If a
client has no model selected, or no provider key is configured, or the model call fails, that
section FALLS BACK to the plain-RSS fill (the previous behaviour) -- the tab always fills. A dead
feed / a client with no workspace is logged and skipped, never fatal. Off-cloud testable via
WORKSPACE_LOCAL_DIR + REGISTRY_LOCAL_DIR; `refresh_client` takes injectable `fetcher` (RSS) and
`ai_fetcher` (LLM) seams so the whole pipeline runs with no network in tests.
"""

import os
import sys

import intel_ai
import intel_feed
import store
import workspace

# --- What each section pulls --------------------------------------------------------------------
# Media Buying News is universal -- the same ad-platform updates matter to every client.
MEDIA_BUYING_FEEDS = (
    "https://searchengineland.com/category/ppc/feed",
)
MEDIA_BUYING_QUERIES = (
    "Google Ads update",
    "Meta Ads Manager update",
    "TikTok advertising",
    "LinkedIn Ads update",
)

# Business Research fixed publisher feeds (keyless RSS) -- the universal floor. The per-client
# QUERIES come from the client's own intel_topics (workspace.get_intel_topics); when a client has no
# topics set, this generic marketing set is used so the section still fills.
BUSINESS_RESEARCH_FEEDS = (
    "https://www.marketingdive.com/feeds/news/",
    "https://www.searchenginejournal.com/feed/",
)
BUSINESS_RESEARCH_FALLBACK_QUERIES = (
    "digital marketing industry trends",
    "advertising industry news",
    "consumer marketing trends",
)

# Final entries kept per section, and the larger candidate pool the AI curates FROM.
_PER_SECTION = 6                 # daily
_BACKFILL_PER_SECTION = 12       # the first 12-month fill shows more
_CANDIDATE_POOL = 24             # real articles handed to the model to choose among
_DAILY_WINDOW = "7d"             # Google-News `when:` recency for the daily run
_BACKFILL_WINDOW = "12m"         # ...and for the first-run 12-month backfill

# Heading/source defaults that make an auto entry read like the hand-written ones.
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
    """Shape a parsed feed row into an intel entry dict (the plain-RSS fallback, no AI)."""
    body = (row.get("body") or "").strip()
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


def _gather(feeds, queries, limit, window=None, fetcher=None):
    """Fetch every feed + query, dedupe, sort newest-first, return up to `limit` RAW rows.

    Rows carry {title, link, body, source, date} -- the shape both the AI curate path (as
    candidates) and the plain-RSS fallback (via _to_entry) consume."""
    rows = []
    for url in feeds:
        rows.extend(intel_feed.fetch_feed(url, limit=limit, fetcher=fetcher))
    for q in queries:
        url = intel_feed.google_news_url(q, window=window)
        rows.extend(intel_feed.fetch_feed(url, limit=limit, fetcher=fetcher))
    rows = _dedupe([r for r in rows if r.get("title")])
    # Newest first; dateless entries fall to the bottom (mirrors atrium_view.intel_sections).
    rows.sort(key=lambda r: (r.get("date") or ""), reverse=True)
    return rows[:limit]


def _business_queries(ws):
    """The Business-Research search queries for a client: its own intel_topics, else the fallback."""
    topics = workspace.get_intel_topics(ws)
    return tuple(topics) if topics else BUSINESS_RESEARCH_FALLBACK_QUERIES


def _fill_section(client, ws, section, feeds, queries, heading, per_section, window,
                  model, prompt, ai_fetcher, fetcher):
    """Retrieve candidates and fill one section -- AI-curated when possible, plain-RSS otherwise.

    Returns (count, used_ai). Only replaces the section when we actually produced entries, so a
    transient feed/model outage never wipes yesterday's still-useful auto entries."""
    candidates = _gather(feeds, queries, _CANDIDATE_POOL, window=window, fetcher=fetcher)
    if not candidates:
        return 0, False

    entries = None
    used_ai = False
    if model:
        entries = intel_ai.curate(
            section,
            ws.get("display_name") or client,
            workspace.get_intel_topics(ws),
            candidates,
            prompt=prompt,
            model=model,
            limit=per_section,
            heading_default=heading,
            fetcher=ai_fetcher,
        )
        used_ai = entries is not None
    if not entries:  # no model, or the model path failed -> plain-RSS fallback
        entries = [_to_entry(r, heading) for r in candidates[:per_section]]

    if entries:
        workspace.replace_auto_intel(client, section, entries)
    return len(entries), used_ai


def refresh_client(client, ws=None, fetcher=None, ai_fetcher=None):
    """Rebuild both auto sections for one client. Returns a summary dict.

    `ws` may be passed to avoid a reload; `fetcher` is the intel_feed (RSS) seam and `ai_fetcher` is
    the intel_ai (LLM transport) seam -- both for tests. Skips (returns zeros) if the client has no
    workspace yet. On the first run for a client (not yet backfilled) it pulls a 12-month window;
    after that, the short daily window."""
    if ws is None:
        ws = workspace.load_workspace(client)
    if ws is None:
        return {"media_buying": 0, "business_research": 0, "ai": False}

    cfg = workspace.get_intel_ai(ws)
    model = cfg.get("model") or ""
    if model and not intel_ai.model_available(model):
        model = ""  # selected model's provider key isn't configured -> fall back, note it

    backfilling = not workspace.intel_backfilled(ws)
    window = _BACKFILL_WINDOW if backfilling else _DAILY_WINDOW
    per_section = _BACKFILL_PER_SECTION if backfilling else _PER_SECTION

    try:
        media_n, media_ai = _fill_section(
            client, ws, "media_buying", MEDIA_BUYING_FEEDS, MEDIA_BUYING_QUERIES,
            _MEDIA_HEADING, per_section, window, model, cfg.get("media_prompt"), ai_fetcher, fetcher)
        biz_n, biz_ai = _fill_section(
            client, ws, "business_research", BUSINESS_RESEARCH_FEEDS, _business_queries(ws),
            _BUSINESS_HEADING, per_section, window, model, cfg.get("business_prompt"),
            ai_fetcher, fetcher)
    except Exception as exc:
        workspace.mark_intel_run(client, model, error=str(exc)[:200])
        raise

    used_ai = bool(model) and (media_ai or biz_ai)
    # Latch backfilled once we've produced anything, so the next run uses the daily window.
    did_fill = (media_n + biz_n) > 0
    workspace.mark_intel_run(
        client, model if used_ai else "",
        error="" if (used_ai or not model) else "model call failed; used news-feed fallback",
        backfilled=True if (backfilling and did_fill) else None)
    return {"media_buying": media_n, "business_research": biz_n, "ai": used_ai}


def refresh_all(fetcher=None, ai_fetcher=None):
    """Refresh every registered client (skipping the worked-example `template`). Returns a summary."""
    summary = {}
    for c in store.list_clients():
        key = c.get("key")
        if not key or key == "template":
            continue
        try:
            counts = refresh_client(key, fetcher=fetcher, ai_fetcher=ai_fetcher)
        except Exception as exc:  # one bad client must not sink the whole run
            print("[intel-refresh] %s FAILED: %s" % (key, exc), file=sys.stderr)
            continue
        summary[key] = counts
        print("[intel-refresh] %s -> media_buying=%d business_research=%d ai=%s"
              % (key, counts["media_buying"], counts["business_research"], counts["ai"]))
    return summary


def main():
    """Job entry point. No-op (logs why) unless INTEL_AUTO_ENABLED=1."""
    if not _enabled():
        print("[intel-refresh] disabled (set INTEL_AUTO_ENABLED=1 to run); nothing to do.")
        return
    brain = intel_ai.default_model() or "(none configured -> news-feed fallback)"
    print("[intel-refresh] starting daily refresh (brain available: %s)" % brain)
    summary = refresh_all()
    print("[intel-refresh] done -- %d client(s) refreshed" % len(summary))


if __name__ == "__main__":
    main()
