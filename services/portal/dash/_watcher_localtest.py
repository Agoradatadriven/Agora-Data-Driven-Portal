"""Off-cloud test for the Watcher tab (no GCS, no network) -- the parser, the data layer, and the
Flask routes.

Stubs google.cloud.storage and points the workspace store at a temp dir (like _atrium_smoketest),
stubs watcher's YouTube fetchers with canned pages, then proves: channel resolution, playlist
paging, transcript batching, workspace CRUD (registry + the per-channel archive object), the
team-only route gating, and the click-to-expand transcript GET.

Run: python _watcher_localtest.py        # prints PASS / FAIL, exits 0 / 1
"""

import os
import shutil
import sys
import tempfile
import types

# 1. Stub google.cloud.storage BEFORE importing main (store/feedback construct a client at import).
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in this test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

_TMP = tempfile.mkdtemp(prefix="atrium_watcher_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"

import seed_workspace   # noqa: E402
import watcher          # noqa: E402
import workspace        # noqa: E402
import main             # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}
CLIENT_LOGIN = {"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]}

_CHANNEL_ID = "UC" + "a" * 22
_CHANNEL_HTML = ('<html><head><meta property="og:title" content="Data With Dana &amp; Co">'
                 '</head><body>"channelId":"%s"</body></html>' % _CHANNEL_ID)


def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def _video_renderer(vid, title):
    return {"playlistVideoRenderer": {"videoId": vid, "title": {"runs": [{"text": title}]}}}


def _video_lockup(vid, title, ago=""):
    """The 2025+ lockupViewModel shape (what live YouTube now serves for playlist items)."""
    meta = {"title": {"content": title}}
    if ago:
        meta["metadata"] = {"contentMetadataViewModel": {"metadataRows": [
            {"metadataParts": [{"text": {"content": "12K views"}}, {"text": {"content": ago}}]}]}}
    return {"lockupViewModel": {"contentId": vid, "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                                "metadata": {"lockupMetadataViewModel": meta}}}


def _browse_pages():
    """Two canned browse responses: page 1 (2 classic-renderer videos + a continuation), page 2
    (lockupViewModel videos, done) -- so BOTH item shapes are proven to parse."""
    page1 = {"contents": {"stuff": [
        _video_renderer("vid00000001", "How to model churn"),
        _video_renderer("vid00000002", "SQL window functions"),
        {"continuationItemRenderer": {"continuationEndpoint": {
            "continuationCommand": {"token": "TOKEN-2"}}}},
    ]}}
    page2 = {"onResponseReceivedActions": [
        _video_lockup("vid00000002", "SQL window functions"),   # duplicate: must de-dupe
        _video_lockup("vid00000003", "Pandas in production", ago="2 weeks ago"),
    ]}
    return {"first": page1, "TOKEN-2": page2}


def run():
    seed_workspace.seed(register_client=False)
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()

    # --- watcher.py: channel resolution + playlist paging (injected fetchers, no network) --------
    info = watcher.resolve_channel("@datawithdana", fetcher=lambda url: _CHANNEL_HTML)
    _check("resolve_channel finds id + title",
           info["ok"] and info["channel_id"] == _CHANNEL_ID and info["title"] == "Data With Dana & Co")
    _check("resolve_channel rejects a non-youtube link",
           watcher.resolve_channel("https://example.com/foo")["ok"] is False)

    pages = _browse_pages()

    def poster(url, payload):
        return pages[payload.get("continuation", "first")]

    listing = watcher.list_videos(_CHANNEL_ID, poster=poster)
    _check("list_videos pages + de-dupes (3 unique videos)",
           listing["ok"] and [v["id"] for v in listing["videos"]]
           == ["vid00000001", "vid00000002", "vid00000003"])
    _check("list_videos rejects a bad id", watcher.list_videos("nope")["ok"] is False)
    _check("lockup upload age captured", listing["videos"][2]["published_text"] == "2 weeks ago")

    import datetime as _dt
    _now = _dt.datetime(2026, 7, 12, tzinfo=_dt.timezone.utc)
    _check("published_estimate: weeks", watcher.published_estimate("2 weeks ago", _now) == "2026-06-28")
    _check("published_estimate: years",
           watcher.published_estimate("Streamed 1 year ago", _now) in ("2025-07-11", "2025-07-12"))
    _check("published_estimate: garbage is empty", watcher.published_estimate("hello") == "")

    # A rate-limit is a session condition: the batch stops, reports blocked, and NO video is
    # marked failed -- the next fetch resumes over the exact same missing set.
    real_fetch_fn = watcher.fetch_transcript
    watcher.fetch_transcript = lambda vid: {
        "ok": False, "transcript": "", "language": "", "generated": False,
        "error": "YouTube is rate-limiting or blocking this server right now — re-run later.",
        "permanent": False}
    blocked_vids = [{"id": "a", "transcript": "", "error": ""}, {"id": "b", "transcript": "", "error": ""}]
    n, blocked = watcher.fetch_transcripts_batch(blocked_vids, pause=0)
    _check("rate-limit stops the batch WITHOUT poisoning videos",
           n == 0 and blocked is True and blocked_vids[0]["error"] == "" and blocked_vids[1]["error"] == "")
    watcher.fetch_transcript = real_fetch_fn

    # --- watcher.py: transcript fetch error paths (package stubbed, no network) ------------------
    real_import = watcher._import_transcript_api

    def _raise_import():
        raise ImportError("not installed")

    watcher._import_transcript_api = _raise_import
    r = watcher.fetch_transcript("vid00000001")
    _check("missing package degrades to a friendly error",
           r["ok"] is False and "not installed" in r["error"])

    class _Track:
        language_code = "en"
        is_generated = False

        def fetch(self):
            return [{"text": "hello"}, {"text": "world  again"}]

    class _Api1x:  # the 1.x instance API surface
        def __init__(self, *a, **k):
            pass

        def list(self, vid):
            return [_Track()]

    fake = types.ModuleType("youtube_transcript_api")
    fake.YouTubeTranscriptApi = _Api1x
    watcher._import_transcript_api = lambda: fake
    r = watcher.fetch_transcript("vid00000001")
    _check("stubbed 1.x API returns normalized text",
           r["ok"] and r["transcript"] == "hello world again" and r["language"] == "en")

    class _Disabled(Exception):
        pass

    _Disabled.__name__ = "TranscriptsDisabled"

    class _ApiRaises(_Api1x):
        def list(self, vid):
            raise _Disabled()

    fake.YouTubeTranscriptApi = _ApiRaises
    r = watcher.fetch_transcript("vid00000001")
    _check("disabled subtitles is a PERMANENT error", r["ok"] is False and r["permanent"] is True)
    watcher._import_transcript_api = real_import

    # --- workspace.py: registry + per-channel archive object -------------------------------------
    entry = workspace.add_watcher_channel(CLIENT, {"url": "u", "title": "T", "channel_id": _CHANNEL_ID,
                                                   "video_count": 3})
    _check("channel registered", workspace.find_watcher_channel(
        workspace.load_workspace(CLIENT), entry["id"])["title"] == "T")
    marker = "TRANSCRIPT-MARKER-93f1"
    workspace.write_watcher_videos(CLIENT, entry["id"], [{"id": "v1", "transcript": marker}])
    _check("archive object round-trips",
           workspace.read_watcher_videos(CLIENT, entry["id"])[0]["transcript"] == marker)
    obj_path = os.path.join(_TMP, workspace.watcher_object_name(CLIENT, entry["id"]))
    _check("archive is its OWN object (not in the workspace JSON)",
           os.path.isfile(obj_path)
           and marker not in open(os.path.join(_TMP, "workspace", CLIENT + ".json")).read())
    workspace.delete_watcher_channel(CLIENT, entry["id"])
    _check("delete removes registry entry + object",
           workspace.watcher_channels(workspace.load_workspace(CLIENT)) == []
           and not os.path.isfile(obj_path))

    # --- Routes: add -> fetch -> expand -> refresh -> delete (fetchers stubbed) ------------------
    with c.session_transaction() as s:
        s.update(SUPER)

    real_resolve, real_list, real_fetch = (watcher.resolve_channel, watcher.list_videos,
                                           watcher.fetch_transcript)
    watcher.resolve_channel = lambda url, fetcher=None: {
        "ok": True, "channel_id": _CHANNEL_ID, "title": "Data With Dana",
        "url": "https://www.youtube.com/channel/" + _CHANNEL_ID, "error": ""}
    watcher.list_videos = lambda cid, poster=None: {"ok": True, "error": "", "videos": [
        {"id": "vid00000001", "title": "How to model churn"},
        {"id": "vid00000002", "title": "SQL window functions"}]}
    watcher.fetch_transcript = lambda vid: {
        "ok": True, "transcript": "transcript for " + vid, "language": "en",
        "generated": False, "error": "", "permanent": False}

    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "add", "url": "@datawithdana"})
    _check("op=add returns ok", r.status_code == 200 and r.get_json()["ok"] is True)
    chan = r.get_json()["channel"]
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "add", "url": "@datawithdana"})
    _check("duplicate channel is refused", r.get_json()["ok"] is False)

    ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), chan)
    _check("channel classified with defaults (youtube creator, no AI -> empty industry)",
           ch["platform"] == "youtube" and ch["kind"] == "creator" and ch["industry"] == "")

    # Hand-edit the classification, then flip it via the AI label op (AI stubbed).
    r = c.post("/w/%s/admin/watcher" % CLIENT,
               data={"op": "meta", "channel_id": chan, "industry": "Data Science", "kind": "competitor"})
    ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), chan)
    _check("op=meta sets industry + kind",
           r.get_json()["ok"] is True and ch["industry"] == "Data Science" and ch["kind"] == "competitor")
    _check("op=meta rejects a bogus kind",
           c.post("/w/%s/admin/watcher" % CLIENT,
                  data={"op": "meta", "channel_id": chan, "kind": "frenemy"}).get_json()["ok"] is False)
    real_autolabel = main._watcher_autolabel
    main._watcher_autolabel = lambda title, titles: ("AI Automation", "")
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "label", "channel_id": chan})
    ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), chan)
    _check("op=label re-runs the AI industry label",
           r.get_json()["industry"] == "AI Automation" and ch["industry"] == "AI Automation")
    main._watcher_autolabel = real_autolabel
    body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
    _check("filter bar + creator grid render (industry option present)",
           'id="ax-wt-fsearch"' in body and 'id="ax-wt-cgrid"' in body and "AI Automation" in body)

    body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
    _check("watcher tab renders pending cards",
           "How to model churn" in body and "Transcript not fetched yet" in body)

    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "fetch", "channel_id": chan})
    data = r.get_json()
    _check("op=fetch pulls both transcripts", data["ok"] and data["done"] == 2 and data["remaining"] == 0)
    ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), chan)
    _check("registry counts updated", ch["transcript_count"] == 2 and ch["failed_count"] == 0)

    r = c.get("/w/%s/watcher/video/%s/vid00000001" % (CLIENT, chan))
    _check("expand GET serves the FULL transcript",
           r.status_code == 200 and r.get_json()["transcript"] == "transcript for vid00000001")

    watcher.list_videos = lambda cid, poster=None: {"ok": True, "error": "", "videos": [
        {"id": "vid00000009", "title": "NEW upload"},
        {"id": "vid00000001", "title": "How to model churn"},
        {"id": "vid00000002", "title": "SQL window functions"}]}
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "refresh", "channel_id": chan})
    _check("op=refresh adds only the new video", r.get_json()["new"] == 1)
    vids = workspace.read_watcher_videos(CLIENT, chan)
    _check("new video is prepended, old transcripts kept",
           vids[0]["id"] == "vid00000009" and vids[1]["transcript"] == "transcript for vid00000001")

    # --- Safe pull: queue the channel for the local slow scraper ---------------------------------
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "safe_pull", "channel_id": chan})
    _check("op=safe_pull queues the channel", r.get_json()["ok"] is True and
           workspace.watcher_safe_pull_queue(workspace.load_workspace(CLIENT)) == [chan])
    c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "safe_pull", "channel_id": chan})
    _check("op=safe_pull is idempotent",
           workspace.watcher_safe_pull_queue(workspace.load_workspace(CLIENT)) == [chan])
    body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
    _check("queued card renders the Safe-pull note", "Safe pull queued" in body)

    # --- Team-only gating: a client must never see or touch Watcher ------------------------------
    with c.session_transaction() as s:
        s.clear()
        s.update(CLIENT_LOGIN)
    body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
    _check("client hitting /watcher is bounced (no watcher pane in the DOM)",
           'data-pane="watcher"' not in body and "How to model churn" not in body)
    _check("client POST is forbidden",
           c.post("/w/%s/admin/watcher" % CLIENT,
                  data={"op": "delete", "channel_id": chan}).status_code == 403)
    _check("client transcript GET is forbidden",
           c.get("/w/%s/watcher/video/%s/vid00000001" % (CLIENT, chan)).status_code == 403)

    with c.session_transaction() as s:
        s.clear()
        s.update(SUPER)
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "delete", "channel_id": chan})
    _check("op=delete removes the channel AND its safe-pull entry", r.get_json()["ok"] is True
           and workspace.watcher_channels(workspace.load_workspace(CLIENT)) == []
           and workspace.watcher_safe_pull_queue(workspace.load_workspace(CLIENT)) == [])

    watcher.resolve_channel, watcher.list_videos, watcher.fetch_transcript = (
        real_resolve, real_list, real_fetch)


if __name__ == "__main__":
    try:
        run()
        print("PASS")
    except AssertionError as exc:
        print("FAIL: %s" % exc)
        sys.exit(1)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
