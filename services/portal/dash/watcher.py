"""YouTube channel watching for the Atrium 'Watcher' tab (team-only).

Three pure-ish helpers, one per step of the pipeline:

  * resolve_channel(url)      -- paste ANY channel link (@handle / /channel/UC... / /c/... / /user/...)
                                 and get back its canonical channel id + display title.
  * list_videos(channel_id)   -- EVERY video (id + title) on the channel, newest first, by paging the
                                 channel's uploads playlist through YouTube's own public web API
                                 (the "innertube" endpoint every browser hits -- keyless, no quota
                                 sign-up, no YouTube Data API key; matching Atrium's "no new infra"
                                 rule).
  * fetch_transcript(video_id)-- one video's full transcript via `youtube-transcript-api`, imported
                                 LAZILY so the portal deploys and every off-cloud test runs without
                                 the package installed (mirrors the anthropic/feedback_ai posture).

Every failure is caught and returned as {ok: False, error: <human sentence>} -- nothing here ever
raises to a route. Transcript failures also carry `permanent`: True means retrying is pointless
(subtitles disabled / video gone), False means transient (rate-limit, network) and worth a retry.

⚠️ Scraping posture: YouTube throttles or blocks datacenter IPs at volume. When that happens the
error says so and the fetch can simply be re-run later; for sustained volume set WATCHER_PROXY_URL
(a standard http(s) proxy URL) and both the listing calls and the transcript API route through it.
No proxy is configured by default -- a default deploy stays infra-free.

Testable off-cloud: `resolve_channel` and `list_videos` accept fetcher injections, and the
transcript-API import goes through `_import_transcript_api` so tests can stub the package.
"""

import os
import re
import time


# A polite, identifiable UA (matches atrium_health's posture).
_UA = "Mozilla/5.0 (compatible; AgoraAtriumWatcher/1.0; +https://agoradatadriven.com)"
# YouTube's own web-client browse endpoint (what youtube.com itself calls for playlist pages).
_BROWSE_URL = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
_WEB_CONTEXT = {"client": {"clientName": "WEB", "clientVersion": "2.20240304.00.00",
                           "hl": "en", "gl": "US"}}
# Hard ceiling on videos listed per channel -- keeps one channel's archive object bounded.
MAX_VIDEOS = 5000


def _proxies():
    """Optional egress proxy for all YouTube traffic ({} when WATCHER_PROXY_URL is unset)."""
    url = os.environ.get("WATCHER_PROXY_URL", "").strip()
    if not url:
        return {}
    return {"http": url, "https": url}


def _http_get(url):
    """GET `url` as text (en, consent pre-accepted so EU consent walls don't intercept)."""
    import requests  # lazy, matching the rest of the app
    resp = requests.get(url, timeout=20, proxies=_proxies() or None, headers={
        "User-Agent": _UA,
        "Accept-Language": "en-US,en;q=0.8",
        "Cookie": "SOCS=CAI",
    })
    resp.raise_for_status()
    return resp.text


def _http_post_json(url, payload):
    """POST a JSON `payload` and return the decoded JSON response."""
    import requests  # lazy
    resp = requests.post(url, json=payload, timeout=20, proxies=_proxies() or None, headers={
        "User-Agent": _UA,
        "Accept-Language": "en-US,en;q=0.8",
    })
    resp.raise_for_status()
    return resp.json()


def normalize_channel_url(url):
    """Turn whatever the operator pasted into a fetchable youtube.com channel URL ('' if hopeless)."""
    url = (url or "").strip()
    if not url:
        return ""
    if re.match(r"^UC[0-9A-Za-z_-]{22}$", url):
        return "https://www.youtube.com/channel/" + url
    if url.startswith("@"):
        return "https://www.youtube.com/" + url
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def resolve_channel(url, fetcher=None):
    """Resolve a pasted channel link to {ok, channel_id, title, url, error}.

    Fetches the channel page and reads the canonical UC... id + og:title out of the returned HTML
    (any URL shape YouTube serves -- @handle, /channel/, /c/, /user/ -- carries both).
    """
    fetcher = fetcher or _http_get
    page_url = normalize_channel_url(url)
    if not page_url or "youtu" not in page_url:
        return {"ok": False, "channel_id": "", "title": "", "url": page_url,
                "error": "That doesn't look like a YouTube channel link."}
    try:
        html = fetcher(page_url)
    except Exception as exc:
        return {"ok": False, "channel_id": "", "title": "", "url": page_url,
                "error": "Could not reach that channel page (%s)." % exc.__class__.__name__}
    m = (re.search(r'"channelId"\s*:\s*"(UC[0-9A-Za-z_-]{22})"', html or "")
         or re.search(r'itemprop="(?:identifier|channelId)"\s+content="(UC[0-9A-Za-z_-]{22})"', html or ""))
    if not m:
        return {"ok": False, "channel_id": "", "title": "", "url": page_url,
                "error": "Couldn't find a channel on that page — paste the channel's main URL "
                         "(like youtube.com/@name)."}
    channel_id = m.group(1)
    t = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    title = _unescape(t.group(1)) if t else channel_id
    return {"ok": True, "channel_id": channel_id, "title": title,
            "url": "https://www.youtube.com/channel/" + channel_id, "error": ""}


_VIDEO_ID = r"[0-9A-Za-z_-]{11}"
# YouTube's keyless oEmbed endpoint -- returns a video's title + author as JSON, no API key/quota.
_OEMBED_URL = "https://www.youtube.com/oembed?format=json&url="


def extract_video_id(url):
    """Pull the 11-char YouTube video id out of any link the operator might paste ('' if none).

    Handles watch?v=, youtu.be/, /shorts/, /embed/, /live/ URLs (query params and all) and a bare
    video id typed on its own; a channel/playlist link (no video) returns ''."""
    url = (url or "").strip()
    if not url:
        return ""
    if re.fullmatch(_VIDEO_ID, url):
        return url
    m = (re.search(r"[?&]v=(" + _VIDEO_ID + r")", url)
         or re.search(r"youtu\.be/(" + _VIDEO_ID + r")", url)
         or re.search(r"/shorts/(" + _VIDEO_ID + r")", url)
         or re.search(r"/embed/(" + _VIDEO_ID + r")", url)
         or re.search(r"/live/(" + _VIDEO_ID + r")", url))
    return m.group(1) if m else ""


def resolve_video(url, fetcher=None):
    """Resolve a SINGLE video link to {ok, video_id, title, author, url, error}.

    Reads the title/author from YouTube's keyless oEmbed endpoint; that lookup is best-effort, so a
    private/removed video (oEmbed 401/404) or a network hiccup still resolves -- the title just
    falls back to the id and the transcript fetch decides the real outcome. Only an unparseable link
    fails here."""
    fetcher = fetcher or _http_get
    video_id = extract_video_id(url)
    if not video_id:
        return {"ok": False, "video_id": "", "title": "", "author": "", "url": "",
                "error": "That doesn't look like a YouTube video link."}
    watch_url = "https://www.youtube.com/watch?v=" + video_id
    title, author = "", ""
    try:
        import json  # lazy, stdlib
        meta = json.loads(fetcher(_OEMBED_URL + watch_url))
        title = _unescape((meta.get("title") or "").strip())
        author = _unescape((meta.get("author_name") or "").strip())
    except Exception:
        pass  # title/author are a nicety; the transcript is what the operator is after
    return {"ok": True, "video_id": video_id, "title": title or ("Video " + video_id),
            "author": author, "url": watch_url, "error": ""}


def list_videos(channel_id, poster=None):
    """Every video on `channel_id` as {ok, videos: [{id, title}], error} (newest first).

    Pages the channel's uploads playlist (UU + the UC id's tail) through the public browse endpoint,
    following continuation tokens until the playlist is exhausted (or MAX_VIDEOS as a safety cap).
    """
    poster = poster or _http_post_json
    if not re.match(r"^UC[0-9A-Za-z_-]{22}$", channel_id or ""):
        return {"ok": False, "videos": [], "error": "Bad channel id."}
    uploads = "UU" + channel_id[2:]
    videos, seen = [], set()
    payload = {"context": _WEB_CONTEXT, "browseId": "VL" + uploads}
    try:
        while True:
            data = poster(_BROWSE_URL, payload)
            found, token = _walk_playlist(data)
            for v in found:
                if v["id"] not in seen:
                    seen.add(v["id"])
                    videos.append(v)
            if not token or len(videos) >= MAX_VIDEOS:
                break
            payload = {"context": _WEB_CONTEXT, "continuation": token}
    except Exception as exc:
        if videos:
            # A mid-pagination failure still returns what we have -- partial beats nothing.
            return {"ok": True, "videos": videos,
                    "error": "Listing stopped early (%s); re-run 'Check for new videos' to finish."
                             % exc.__class__.__name__}
        return {"ok": False, "videos": [],
                "error": "YouTube did not return the channel's video list (%s). Try again in a few "
                         "minutes." % exc.__class__.__name__}
    if not videos:
        return {"ok": False, "videos": [], "error": "That channel has no public videos."}
    return {"ok": True, "videos": videos, "error": ""}


def _walk_playlist(node, found=None, token_box=None):
    """Recursively collect the playlist's video items + the next continuation token from a browse
    response (YouTube nests them at varying depths, so we walk rather than hardcode a path).

    Two item shapes are handled: the classic `playlistVideoRenderer` and the 2025+ `lockupViewModel`
    (contentType LOCKUP_CONTENT_TYPE_VIDEO with the title under metadata.lockupMetadataViewModel) --
    YouTube serves one or the other depending on rollout, so we accept both.
    """
    if found is None:
        found, token_box = [], []
    if isinstance(node, dict):
        r = node.get("playlistVideoRenderer")
        if isinstance(r, dict) and r.get("videoId"):
            found.append({"id": r["videoId"], "title": _renderer_title(r),
                          "published_text": _renderer_published(r)})
        lk = node.get("lockupViewModel")
        if (isinstance(lk, dict) and lk.get("contentId")
                and lk.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO"):
            found.append({"id": lk["contentId"], "title": _lockup_title(lk),
                          "published_text": _lockup_published(lk)})
        cc = node.get("continuationCommand")
        if isinstance(cc, dict) and cc.get("token"):
            token_box.append(cc["token"])
        for v in node.values():
            _walk_playlist(v, found, token_box)
    elif isinstance(node, list):
        for v in node:
            _walk_playlist(v, found, token_box)
    return found, (token_box[0] if token_box else "")


def _renderer_title(renderer):
    """A playlistVideoRenderer's display title (runs-joined, with the simpleText fallback)."""
    title = renderer.get("title") or {}
    runs = title.get("runs") or []
    text = "".join(r.get("text", "") for r in runs) or title.get("simpleText") or ""
    return text or "(untitled video)"


def _lockup_title(lockup):
    """A lockupViewModel's display title (metadata.lockupMetadataViewModel.title.content)."""
    meta = (lockup.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    return ((meta.get("title") or {}).get("content") or "").strip() or "(untitled video)"


_AGO_RE = re.compile(r"\b(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago\b", re.I)


def _find_ago(texts):
    """The first 'N units ago' string in `texts` (YouTube's relative upload age), or ''."""
    for t in texts:
        if t and _AGO_RE.search(t):
            return _AGO_RE.search(t).group(0)
    return ""


def _renderer_published(renderer):
    """A playlistVideoRenderer's relative upload age ('2 weeks ago'), or '' when absent."""
    info = renderer.get("videoInfo") or {}
    return _find_ago([r.get("text", "") for r in (info.get("runs") or [])])


def _lockup_published(lockup):
    """A lockupViewModel's relative upload age, scraped from its metadata text rows."""
    meta = (lockup.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    cmv = (meta.get("metadata") or {}).get("contentMetadataViewModel") or {}
    texts = []
    for row in cmv.get("metadataRows") or []:
        for part in row.get("metadataParts") or []:
            texts.append(((part.get("text") or {}).get("content") or ""))
    return _find_ago(texts)


_AGO_DAYS = {"minute": 1 / 1440.0, "hour": 1 / 24.0, "day": 1, "week": 7, "month": 30.44, "year": 365.25}


def published_estimate(text, now=None):
    """Turn YouTube's relative age ('3 weeks ago') into an estimated ISO date ('2026-06-21').

    Coarse by nature (YouTube only says 'ago'), but good enough to sort and filter videos by
    date across creators. Returns '' when the text carries no parseable age."""
    m = _AGO_RE.search(text or "")
    if not m:
        return ""
    import datetime
    days = int(m.group(1)) * _AGO_DAYS[m.group(2).lower()]
    when = (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=days)
    return when.strftime("%Y-%m-%d")


def _unescape(text):
    import html
    return html.unescape(text or "")


def _import_transcript_api():
    """Import hook for youtube-transcript-api (separate so tests can stub the package)."""
    import youtube_transcript_api
    return youtube_transcript_api


# Error class names (matched by NAME so any library version works) that mean "this video will never
# have a transcript" -- no point retrying.
_PERMANENT_ERRORS = {
    "TranscriptsDisabled": "Subtitles are disabled on this video.",
    "NoTranscriptFound": "No transcript exists for this video.",
    "NoTranscriptAvailable": "No transcript exists for this video.",
    "VideoUnavailable": "This video is private, deleted, or unavailable.",
    "VideoUnplayable": "This video can't be played (region lock or membership).",
    "AgeRestricted": "This video is age-restricted, so its transcript can't be read.",
    "NotTranslatable": "No readable transcript exists for this video.",
}
_BLOCKED_ERRORS = {"IpBlocked", "RequestBlocked", "TooManyRequests"}


def fetch_transcript(video_id):
    """One video's full transcript: {ok, transcript, language, generated, error, permanent}.

    Prefers a human-made English track, then any English, then whatever exists (auto-captions
    included) -- 'keep it raw' means grab whatever YouTube has. The joined text is plain,
    whitespace-normalized prose.
    """
    out = {"ok": False, "transcript": "", "language": "", "generated": False,
           "error": "", "permanent": False}
    try:
        yta = _import_transcript_api()
    except ImportError:
        out["error"] = ("The transcript package (youtube-transcript-api) is not installed on this "
                        "server.")
        return out
    try:
        api_cls = yta.YouTubeTranscriptApi
        if hasattr(api_cls, "list_transcripts"):  # 0.6.x classmethod API
            listing = api_cls.list_transcripts(video_id, proxies=_proxies() or None)
        else:  # 1.x instance API
            api = _transcript_client(yta)
            listing = api.list(video_id)
        track = _pick_track(listing)
        if track is None:
            out.update(error="No transcript exists for this video.", permanent=True)
            return out
        fetched = track.fetch()
        text = " ".join(_snippet_text(s) for s in fetched)
        out.update(ok=True, transcript=re.sub(r"\s+", " ", text).strip(),
                   language=getattr(track, "language_code", "") or "",
                   generated=bool(getattr(track, "is_generated", False)))
        return out
    except Exception as exc:
        name = exc.__class__.__name__
        if name in _PERMANENT_ERRORS:
            out.update(error=_PERMANENT_ERRORS[name], permanent=True)
        elif name in _BLOCKED_ERRORS:
            out["error"] = ("YouTube is rate-limiting or blocking this server right now — re-run "
                            "the fetch later (or set WATCHER_PROXY_URL).")
        else:
            out["error"] = "Could not fetch the transcript (%s)." % name
        return out


def _transcript_client(yta):
    """A 1.x YouTubeTranscriptApi instance, routed through the optional proxy when configured."""
    proxies = _proxies()
    if proxies:
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig
            return yta.YouTubeTranscriptApi(proxy_config=GenericProxyConfig(
                http_url=proxies["http"], https_url=proxies["https"]))
        except Exception:
            pass  # proxy plumbing is best-effort; fall through to a direct client
    return yta.YouTubeTranscriptApi()


def _pick_track(listing):
    """Best transcript track from a TranscriptList: manual English > any English > first available."""
    tracks = list(listing)
    if not tracks:
        return None

    def score(t):
        lang = (getattr(t, "language_code", "") or "").lower()
        manual = not getattr(t, "is_generated", False)
        english = lang.startswith("en")
        return (0 if (manual and english) else 1 if english else 2 if manual else 3)
    return sorted(tracks, key=score)[0]


def _snippet_text(snippet):
    """One caption snippet's text -- 1.x returns objects with .text, 0.6.x returns dicts."""
    if isinstance(snippet, dict):
        return snippet.get("text", "")
    return getattr(snippet, "text", "") or ""


def fetch_transcripts_batch(videos, limit=8, pause=0.25):
    """Fetch transcripts for up to `limit` pending videos IN PLACE (mutates the video dicts).

    A video is pending when it has neither a transcript nor a recorded error. Returns
    (fetched, blocked): how many videos were resolved this call, and whether YouTube's
    rate-limiting cut the batch short.

    A rate-limit is a SESSION condition, not a fact about the video -- so it never writes an
    error onto the video (the video stays pending and the very next fetch resumes exactly where
    this one stopped, retrying only the still-missing ones). Only real per-video outcomes are
    recorded: a transcript, a permanent "no transcript exists", or a non-rate-limit fetch error.
    """
    from workspace import now_iso  # local import: avoids a cycle at module load
    done = 0
    for v in videos:
        if done >= limit:
            break
        if v.get("transcript") or v.get("error"):
            continue
        result = fetch_transcript(v.get("id", ""))
        if not result["ok"] and "rate-limiting" in result["error"]:
            return done, True
        v["fetched_at"] = now_iso()
        if result["ok"]:
            v["transcript"] = result["transcript"]
            v["language"] = result["language"]
            v["generated"] = result["generated"]
            v["error"] = ""
            v["permanent"] = False
        else:
            v["error"] = result["error"]
            v["permanent"] = bool(result["permanent"])
        done += 1
        if pause:
            time.sleep(pause)
    return done, False
