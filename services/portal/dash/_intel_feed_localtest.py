"""Local smoke test for the daily Market Intelligence auto-refresh -- runs entirely off-cloud.

Exercises the whole pipeline with NO network: intel_feed parsing (RSS + Atom), the URL builder, the
workspace topics + replace_auto_intel data layer, and intel_refresh.refresh_client with an INJECTED
fetcher. Proves auto entries are written with real fields and that hand-added entries survive a
refresh. No GCS, no ADC, no `requests` network call.

    python _intel_feed_localtest.py        # prints PASS / FAIL and exits 0 / 1
"""

import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="intel_localtest_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP

import intel_feed          # noqa: E402  (must follow the env setup above)
import intel_refresh       # noqa: E402
import workspace           # noqa: E402

CLIENT = "feedtest"

# A Google-News-shaped RSS feed (per-item <source> publisher, RFC-822 pubDate).
_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Google News</title>
  <item>
    <title>Big RV brands post record summer bookings - RV Business</title>
    <link>https://news.google.com/rss/articles/abc123</link>
    <pubDate>Wed, 24 Jun 2026 10:00:00 GMT</pubDate>
    <description>&lt;a href="x"&gt;Big RV brands post record summer bookings&lt;/a&gt;&nbsp;RV Business</description>
    <source url="https://rvbusiness.com">RV Business</source>
  </item>
  <item>
    <title>Campground demand surges in the Midwest - Travel Daily</title>
    <link>https://news.google.com/rss/articles/def456</link>
    <pubDate>Tue, 23 Jun 2026 08:30:00 GMT</pubDate>
    <description>Demand is up sharply across the region this season.</description>
    <source url="https://traveldaily.com">Travel Daily</source>
  </item>
</channel></rss>"""

# An Atom-shaped publisher feed (channel <title> is the fallback source; <updated> is ISO).
_ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Search Engine Land</title>
  <entry>
    <title>Google Ads rolls out a new bidding control</title>
    <link rel="alternate" href="https://searchengineland.com/google-ads-bidding-12345"/>
    <updated>2026-06-24T12:00:00Z</updated>
    <summary>The new control lets advertisers cap spend per campaign.</summary>
  </entry>
</feed>"""


class _Resp(object):
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status


def _fetcher(url, timeout):
    """Inject feed bytes by URL shape -- no network. Atom for the PPC publisher feed, RSS otherwise."""
    if "searchengineland" in url:
        return _Resp(_ATOM)
    if "news.google.com" in url:
        return _Resp(_RSS)
    return _Resp(b"", status=404)


def _check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def run():
    print("[intel-localtest] WORKSPACE_LOCAL_DIR = %s" % _TMP)

    # 1. URL builder.
    url = intel_feed.google_news_url("RV industry")
    _check("google_news_url encodes the query", "q=RV%20industry" in url)
    _check("google_news_url is blank for empty input", intel_feed.google_news_url("  ") == "")

    # 2. RSS parsing -- real title (publisher tail trimmed), real link, real source + date.
    rows = intel_feed.parse_feed(_RSS)
    _check("RSS parsed two items", len(rows) == 2)
    _check("publisher tail trimmed off title",
           rows[0]["title"] == "Big RV brands post record summer bookings")
    _check("real publisher link kept", rows[0]["link"] == "https://news.google.com/rss/articles/abc123")
    _check("source from <source>", rows[0]["source"] == "RV Business")
    _check("RFC-822 date -> ISO", rows[0]["date"] == "2026-06-24")

    # 3. Atom parsing -- href link, channel-title fallback source, ISO date.
    arows = intel_feed.parse_feed(_ATOM)
    _check("Atom parsed one entry", len(arows) == 1)
    _check("Atom href link", arows[0]["link"] == "https://searchengineland.com/google-ads-bidding-12345")
    _check("Atom fallback source = feed title", arows[0]["source"] == "Search Engine Land")
    _check("Atom ISO date", arows[0]["date"] == "2026-06-24")

    # 4. fetch_feed via the injected fetcher (no network), and graceful failure.
    _check("fetch_feed returns rows", len(intel_feed.fetch_feed("https://news.google.com/x", fetcher=_fetcher)) == 2)
    _check("fetch_feed caps to limit", len(intel_feed.fetch_feed("https://news.google.com/x", limit=1, fetcher=_fetcher)) == 1)
    _check("404 feed -> []", intel_feed.fetch_feed("https://nope.example/x", fetcher=_fetcher) == [])
    _check("bad url -> []", intel_feed.fetch_feed("", fetcher=_fetcher) == [])
    _check("garbage XML -> []", intel_feed.parse_feed(b"not xml <") == [])

    # 5. Topics data layer.
    workspace.save_workspace(CLIENT, {"display_name": "Feed Test", "intel": {}})
    workspace.set_intel_topics(CLIENT, "RV industry, , motorhome sales\ncampground trends")
    ws = workspace.load_workspace(CLIENT)
    _check("topics cleaned + deduped", workspace.get_intel_topics(ws) ==
           ["RV industry", "motorhome sales", "campground trends"])

    # 6. replace_auto_intel preserves a hand-added entry, swaps auto ones.
    workspace.add_intel_entry(CLIENT, "business_research", {"title": "Manual note"})
    workspace.replace_auto_intel(CLIENT, "business_research",
                                 [{"title": "Auto A", "auto": True}, {"title": "Auto B"}])
    br = workspace.load_workspace(CLIENT)["intel"]["business_research"]
    titles = sorted(e["title"] for e in br)
    _check("manual entry survives + two auto added", titles == ["Auto A", "Auto B", "Manual note"])
    _check("auto entries flagged auto",
           sum(1 for e in br if e.get("auto")) == 2)
    # A second refresh replaces only the auto ones -> still exactly one manual + two auto.
    workspace.replace_auto_intel(CLIENT, "business_research", [{"title": "Auto C"}])
    br2 = workspace.load_workspace(CLIENT)["intel"]["business_research"]
    _check("second refresh keeps manual, swaps auto",
           sorted(e["title"] for e in br2) == ["Auto C", "Manual note"])

    # 7. Editing an auto entry PINS it (drops the auto flag -> survives the next refresh).
    workspace.replace_auto_intel(CLIENT, "media_buying", [{"title": "Auto MB", "auto": True}])
    auto_mb = workspace.load_workspace(CLIENT)["intel"]["media_buying"][0]
    workspace.update_intel_entry(CLIENT, "media_buying", auto_mb["id"], {"title": "Pinned MB"})
    workspace.replace_auto_intel(CLIENT, "media_buying", [{"title": "Fresh MB"}])
    mb = workspace.load_workspace(CLIENT)["intel"]["media_buying"]
    _check("edited auto entry is pinned across refresh",
           sorted(e["title"] for e in mb) == ["Fresh MB", "Pinned MB"])

    # 8. refresh_client with NO model selected does nothing (there is NO news-feed fallback -- the
    #    AI-curation path is exercised in _intel_ai_localtest with an injected model transport).
    workspace.save_workspace(CLIENT, {"display_name": "Feed Test", "intel": {},
                                      "intel_topics": ["RV industry"]})
    counts = intel_refresh.refresh_client(CLIENT, fetcher=_fetcher)
    _check("no model -> nothing filled", counts == {"media_buying": 0, "business_research": 0, "ai": False})
    _check("no model -> reason recorded",
           "model" in (workspace.load_workspace(CLIENT).get("intel_ai", {}).get("last_error", "").lower()))

    # 9. refresh_client on an unseeded client is a safe no-op.
    _check("missing workspace -> zeros",
           intel_refresh.refresh_client("nope", fetcher=_fetcher) ==
           {"media_buying": 0, "business_research": 0, "ai": False})


def main():
    try:
        run()
    except AssertionError as exc:
        print("\n[FAIL] %s" % exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        print("\n[ERROR] %s" % exc)
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n[PASS] intel auto-refresh data layer + feed parsing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
