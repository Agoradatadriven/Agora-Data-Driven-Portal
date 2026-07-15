"""Off-cloud test for the Mail tab (no GCS, no network, no IMAP server) -- the connectors, the data
layer, the sync pipeline, and the Flask routes.

Stubs google.cloud.storage and points the stores at a temp dir (like _watcher_localtest), injects
fake transports into mailroom (the signJwt/token POSTs, the Gmail API GETs, an in-memory IMAP
connection, and a canned LLM fetcher), then proves: contact cleaning + the Gmail query, the keyless
DWD token mint, both pull paths normalizing to the same thread shape, quoted-reply stripping,
the mailbox registry (passwords never leak to templates), the per-thread archive objects, a full
sync_client run (archive + summaries + digest + spend tally), the Assistant chunks, the team-only
route gating, and the console Mailboxes pane.

Run: python _mail_localtest.py        # prints PASS / FAIL, exits 0 / 1
"""

import base64
import json
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

_TMP = tempfile.mkdtemp(prefix="atrium_mail_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["DEEPSEEK_API_KEY"] = "test-key"        # makes an AI provider "configured" for the brain
os.environ["MAIL_DWD_SA"] = "mail-sync@agora-data-driven.iam.gserviceaccount.com"

import assistant_ai     # noqa: E402
import mail_refresh     # noqa: E402
import mailroom         # noqa: E402
import seed_workspace   # noqa: E402
import workspace        # noqa: E402
import main             # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}
CLIENT_LOGIN = {"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]}


def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


# --- Canned transports ----------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _b64url(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _api_message(mid, frm, to, subject, body, epoch_ms, extra=None, labels=None):
    heads = [{"name": "From", "value": frm}, {"name": "To", "value": to},
             {"name": "Subject", "value": subject}]
    for k, v in (extra or {}).items():
        heads.append({"name": k, "value": v})
    return {"id": mid, "internalDate": str(epoch_ms), "snippet": body[:60],
            "labelIds": labels or [], "payload": {
                "mimeType": "multipart/alternative",
                "headers": heads,
                "parts": [{"mimeType": "text/plain", "body": {"data": _b64url(body)}}],
            }}


_GMAIL_THREAD = {"id": "18c9aa01", "messages": [
    _api_message("m1", "Maya Reyes <maya@riverdanceresort.com>", "info@agoradatadriven.com",
                 "June budget", "Can we raise the Meta budget to $4k?", 1751500000000),
    _api_message("m2", "info@agoradatadriven.com", "maya@riverdanceresort.com",
                 "Re: June budget", "Yes - confirming $4k from Monday.\n\nOn Tue, Maya wrote:\n> Can we raise it?",
                 1751590000000),
]}


# An ALL-machine thread (noreply sender + bulk headers): KEPT now, tiered "noise".
_GMAIL_AUTOMATED = {"id": "18c9bb02", "messages": [
    _api_message("m9", "Riverdance Bookings <noreply@riverdanceresort.com>",
                 "info@agoradatadriven.com", "Your weekly booking report",
                 "Automated weekly report. Unsubscribe below.", 1751500000000,
                 extra={"List-Unsubscribe": "<mailto:u@x.com>", "Precedence": "bulk"},
                 labels=["CATEGORY_PROMOTIONS"]),
]}
# A security alert from Google: MUST be kept + tiered "security" (the old filter wrongly dropped it).
_GMAIL_SECURITY = {"id": "18c9cc03", "messages": [
    _api_message("m10", "Google <no-reply@accounts.google.com>", "info@agoradatadriven.com",
                 "Security alert: app password created for Atrium",
                 "An app password was created for your account.", 1751600000000),
]}


def _gmail_getter(url, headers, params, timeout):
    if url.endswith("/threads"):
        return _Resp({"threads": [{"id": "18c9aa01"}, {"id": "18c9bb02"}, {"id": "18c9cc03"}]})
    if url.endswith("/threads/18c9aa01"):
        return _Resp(_GMAIL_THREAD)
    if url.endswith("/threads/18c9bb02"):
        return _Resp(_GMAIL_AUTOMATED)
    if url.endswith("/threads/18c9cc03"):
        return _Resp(_GMAIL_SECURITY)
    if url.endswith("/profile"):
        return _Resp({"emailAddress": "info@agoradatadriven.com", "threadsTotal": 42})
    return _Resp({"error": {"message": "unexpected url " + url}}, status=404)


def _dwd_poster(url, headers, payload, timeout, form=False):
    if "signJwt" in url:
        claims = json.loads(payload["payload"])
        assert claims["sub"] == "info@agoradatadriven.com" and "gmail.readonly" in claims["scope"]
        return _Resp({"signedJwt": "signed.jwt.blob"})
    if url == mailroom.TOKEN_ENDPOINT:
        assert form and payload["assertion"] == "signed.jwt.blob"
        return _Resp({"access_token": "delegated-token"})
    return _Resp({}, status=500)


_RFC822 = (b"Message-ID: <msg-77@mail.gmail.com>\r\n"
           b"From: Diego <diego@clientmail.com>\r\n"
           b"To: projects@gmail.com\r\n"
           b"Subject: Landing page copy\r\n"
           b"Date: Tue, 08 Jul 2026 10:15:00 +0800\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
           b"Draft attached - can you review by Friday?\r\n")


class _FakeImap:
    """One UID (11), thread id 415904. Handles the cheap batched THRID scan AND the body fetch."""
    def __init__(self):
        self.logged_out = False

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            assert args[0] == "X-GM-RAW" and "from:" in args[1]
            return "OK", [b"11"]
        if cmd == "FETCH":
            spec = args[-1]
            if "RFC822" not in spec:                      # the cheap thread-id-only scan pass
                return "OK", [b"11 (UID 11 X-GM-THRID 415904)"]
            return "OK", [(b"11 (X-GM-THRID 415904 RFC822 {%d}" % len(_RFC822), _RFC822), b")"]
        return "NO", None

    def logout(self):
        self.logged_out = True


def _ai_fetcher(url, headers, payload, timeout):
    """The intel_ai transport: answers the summary + digest prompts (DeepSeek shape).

    The digest reply embeds [stats-ok] ONLY when the computed responsiveness numbers arrived in
    the user turn -- proving sync/route wiring passes stats_line() through."""
    system = payload["messages"][0]["content"]
    user = payload["messages"][1]["content"]
    if "summarize one email thread" in system:
        content = json.dumps({"summary": "Budget raised to $4k; AGORA owes the updated plan.",
                              "client_summary": "Friendly recap: we agreed to raise the Meta "
                                                "budget to $4k starting Monday."})
    else:
        marker = " [stats-ok]" if "Responsiveness numbers" in user else ""
        content = json.dumps({"digest": "STATUS: healthy.\nNEEDS ACTION:\n- send the updated plan"
                                        "\nRECENT:\n- budget raised\nREPLIES:\n- replies are prompt"
                                        + marker})
    return _Resp({"choices": [{"message": {"content": content}}],
                  "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


def run():
    seed_workspace.seed(register_client=False)
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()

    # --- mailroom: pure helpers -------------------------------------------------------------------
    _check("clean_contacts keeps emails + domains, drops junk",
           mailroom.clean_contacts(["Maya@RiverDance.com", "riverdance.com", "not a contact", "", "x"])
           == ["maya@riverdance.com", "riverdance.com"])
    q = mailroom.gmail_query(["maya@riverdance.com", "riverdance.com"], days=7)
    _check("gmail_query ORs from/to per contact + window",
           "{from:maya@riverdance.com to:maya@riverdance.com from:riverdance.com to:riverdance.com}" in q
           and "newer_than:7d" in q and "-in:chats" in q)
    _check("gmail_query with no usable contact is empty", mailroom.gmail_query(["???"]) == "")
    _check("X-GM-THRID normalizes to the API's hex form", mailroom._hex_thrid("415904") == "658a0")
    cleaned = mailroom.clean_body("New copy below.\n\nOn Tue, 8 Jul 2026, Maya Reyes wrote:\n> old stuff\n> more old")
    _check("clean_body strips the quoted tail", cleaned == "New copy below." )
    _check("HTML fallback strips to text",
           "Hello world" in mailroom._strip_html("<div>Hello <b>world</b><style>x{}</style></div>"))

    _check("is_automated: noreply sender", mailroom.is_automated("Bookings <noreply@rd.com>", {}))
    _check("is_automated: Auto-Submitted",
           mailroom.is_automated("maya@rd.com", {"Auto-Submitted": "auto-generated"}))
    _check("is_automated: Precedence bulk",
           mailroom.is_automated("maya@rd.com", {"Precedence": "bulk"}))
    _check("is_automated: Google-Groups human mail is KEPT (List-Unsubscribe alone never counts)",
           not mailroom.is_automated("team@client.com",
                                     {"List-Unsubscribe": "<mailto:u@x>", "Precedence": "list"}))
    _check("is_automated: a plain person is kept", not mailroom.is_automated("Maya <maya@rd.com>", {}))

    st = mailroom.thread_stats({"messages": [
        {"from": "maya@x.com", "date": "2026-07-01T00:00:00Z"},
        {"from": "info@agoradatadriven.com", "date": "2026-07-02T01:00:00Z"},
        {"from": "maya@x.com", "date": "2026-07-03T00:00:00Z"},
    ]}, {"info@agoradatadriven.com"})
    _check("thread_stats: reply clock + who owes whom",
           st["awaiting_reply"] is True and st["avg_response_hours"] == 25.0
           and st["replies_measured"] == 1)
    _check("stats_line formats the computed numbers",
           "1 awaiting an AGORA reply" in mailroom.stats_line(
               [{"id": "a", "awaiting_reply": True, "last_date": "2026-07-03T00:00:00Z",
                 "avg_response_hours": 25.0}]))

    # --- mailroom: keyless DWD token + Gmail API pull (injected transports) ------------------------
    tok, err = mailroom.dwd_access_token("info@agoradatadriven.com", poster=_dwd_poster,
                                         token_fetcher=lambda: "gcp-token")
    _check("dwd token mints via signJwt + exchange", tok == "delegated-token" and err == "")
    threads, err, backlog = mailroom.gmail_pull(tok, q, getter=_gmail_getter)
    _check("gmail_pull KEEPS every thread now (machine mail no longer dropped)",
           err == "" and len(threads) == 3 and backlog == 0)
    _by = {t["subject"]: t for t in threads}
    _check("the human thread parsed with both messages",
           _by["June budget"] and len(_by["June budget"]["messages"]) == 2)
    _check("messages carry an `automated` flag",
           _by["Your weekly booking report"]["messages"][0]["automated"] is True
           and _by["June budget"]["messages"][0]["automated"] is False)

    # classify_thread: the four tiers, rules-only.
    ca, cd = mailroom._contact_sets(["maya@riverdanceresort.com", "riverdanceresort.com"])
    _check("classify: security wins (Google app-password alert is NOT dropped, it escalates)",
           mailroom.classify_thread(_by["Security alert: app password created for Atrium"], ca, cd)
           == "security")
    _check("classify: an all-automated newsletter is noise",
           mailroom.classify_thread(_by["Your weekly booking report"], ca, cd) == "noise")
    _check("classify: human mail matching a client contact is client",
           mailroom.classify_thread(_by["June budget"], ca, cd) == "client")
    _check("classify: human mail NOT from the client is operations (shared mailbox)",
           mailroom.classify_thread(
               {"messages": [{"from": "vendor@printco.com", "to": "info@agoradatadriven.com",
                              "subject": "Quote", "automated": False}]}, ca, cd) == "operations")
    _check("classify: in a client-OWNED mailbox, any human mail counts as client",
           mailroom.classify_thread(
               {"messages": [{"from": "lead@random.com", "to": "gab@meloyelo.nz",
                              "subject": "Interested", "automated": False}]},
               set(), set(), client_owned=True) == "client")
    # Backfill: with the real thread already 'known', a second pull refetches nothing new but the
    # listing still sees it (backlog stays 0 because it's known, not unfetched).
    threads2, err2, backlog2 = mailroom.gmail_pull(tok, q, getter=_gmail_getter,
                                                   known={"18c9aa01", "18c9bb02", "18c9cc03"})
    _check("gmail_pull skips known threads first (backfill drains, doesn't refetch)",
           err2 == "" and backlog2 == 0)
    _check("api reply lost its quoted tail", "old" not in threads[0]["messages"][1]["body"]
           and "confirming $4k" in threads[0]["messages"][1]["body"])
    _check("participants collected", "maya@riverdanceresort.com" in threads[0]["participants"])

    def _refused(url, headers, payload, timeout, form=False):
        if "signJwt" in url:
            return _Resp({"signedJwt": "signed.jwt.blob"})
        return _Resp({"error": "unauthorized_client", "error_description": "unauthorized_client"}, status=400)
    _tok, err = mailroom.dwd_access_token("x@agoradatadriven.com", poster=_refused,
                                          token_fetcher=lambda: "gcp-token")
    _check("a missing Workspace grant reads as a delegation error", "domain-wide-delegation" in err)

    def _refused_longform(url, headers, payload, timeout, form=False):
        if "signJwt" in url:
            return _Resp({"signedJwt": "signed.jwt.blob"})
        return _Resp({"error": "unauthorized_client",
                      "error_description": "Client is unauthorized to retrieve access tokens "
                                           "using this method, or client not authorized for any "
                                           "of the scopes requested."}, status=401)
    _tok, err = mailroom.dwd_access_token("x@agoradatadriven.com", poster=_refused_longform,
                                          token_fetcher=lambda: "gcp-token")
    _check("Google's long-form delegation refusal maps to the same guidance",
           "domain-wide-delegation" in err and "propagate" in err)

    # --- mailroom: IMAP pull (fake connection) ------------------------------------------------------
    threads, err, backlog = mailroom.imap_pull({"email": "projects@gmail.com", "kind": "imap"},
                                               q, imap_factory=lambda mb: _FakeImap())
    _check("imap_pull normalizes to the same shape", err == "" and len(threads) == 1
           and backlog == 0 and threads[0]["id"] == "658a0"
           and threads[0]["subject"] == "Landing page copy"
           and threads[0]["messages"][0]["id"] == "<msg-77@mail.gmail.com>"
           and threads[0]["messages"][0]["date"].startswith("2026-07-08"))

    def _badlogin(mb):
        raise RuntimeError("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
    _thr, err, _bk = mailroom.imap_pull({"email": "x@gmail.com", "kind": "imap"}, q,
                                        imap_factory=_badlogin)
    _check("a rejected app password reads as a friendly error", "app password" in err)

    def _normalpw(mb):
        raise RuntimeError(b"[ALERT] Application-specific password required: "
                           b"https://support.google.com/accounts/answer/185833 (Failure)")
    _thr, err, _bk = mailroom.imap_pull({"email": "gab@meloyelo.nz", "kind": "imap"},
                                        q, imap_factory=_normalpw)
    _check("a NORMAL password (not an app password) gets step-by-step guidance",
           "APP password" in err and "apppasswords" in err and "b'" not in err)

    # --- workspace: the mailbox registry ------------------------------------------------------------
    try:
        workspace.add_mailbox("not-an-email", "imap", app_password="x")
        raise AssertionError("bad email accepted")
    except ValueError:
        print("  [OK] add_mailbox rejects a bad email")
    try:
        workspace.add_mailbox("a@b.com", "imap")
        raise AssertionError("imap without password accepted")
    except ValueError:
        print("  [OK] add_mailbox requires the imap app password")
    mb1 = workspace.add_mailbox("info@agoradatadriven.com", "dwd")
    mb2 = workspace.add_mailbox("projects@gmail.com", "imap", app_password="abcd efgh ijkl mnop")
    mbC = workspace.add_mailbox("gab@meloyelo.nz", "imap", app_password="p" * 16, client="riverdance")
    _check("a mailbox can be ASSIGNED to a client (dedicated inbox)",
           workspace.find_mailbox(mbC["id"])["client"] == "riverdance"
           and next(b for b in workspace.public_mailboxes() if b["id"] == mbC["id"])["client"] == "riverdance")
    workspace.delete_mailbox(mbC["id"])
    _check("app password stored without spaces",
           workspace.find_mailbox(mb2["id"])["app_password"] == "abcdefghijklmnop")
    same = workspace.add_mailbox("projects@gmail.com", "imap", app_password="qqqqqqqqqqqqqqqq")
    _check("re-adding upserts by email (same id, new password)",
           same["id"] == mb2["id"] and workspace.find_mailbox(mb2["id"])["app_password"] == "q" * 16)
    pub = workspace.public_mailboxes()
    _check("public_mailboxes never carries a password",
           len(pub) == 2 and all("app_password" not in b for b in pub)
           and any(b["has_password"] for b in pub))

    # --- workspace: contacts + thread objects --------------------------------------------------------
    contacts = workspace.set_mail_contacts(CLIENT, "Maya@riverdanceresort.com, riverdanceresort.com\n dupe@x.com, dupe@x.com")
    _check("set_mail_contacts parses/dedupes the textarea",
           contacts == ["maya@riverdanceresort.com", "riverdanceresort.com", "dupe@x.com"])
    marker = {"id": "18c9aa01", "subject": "S", "participants": [], "last_date": "2026-07-08T00:00:00Z",
              "messages": [{"id": "m1", "body": "MAIL-MARKER-71ce"}]}
    workspace.write_mail_thread(CLIENT, "mbX_18c9aa01", marker)
    _check("thread archive round-trips",
           workspace.read_mail_thread(CLIENT, "mbX_18c9aa01")["messages"][0]["body"] == "MAIL-MARKER-71ce")
    obj_path = os.path.join(_TMP, workspace.mail_thread_object_name(CLIENT, "mbX_18c9aa01"))
    _check("archive is its OWN object (not in the workspace JSON)",
           os.path.isfile(obj_path)
           and "MAIL-MARKER-71ce" not in open(os.path.join(_TMP, "workspace", CLIENT + ".json")).read())
    workspace.upsert_mail_thread_entry(CLIENT, {"id": "mbX_18c9aa01", "subject": "S",
                                                "last_date": "2026-07-08T00:00:00Z",
                                                "message_count": 1, "mailbox": "x@y.com"})
    workspace.set_mail_thread_summary(CLIENT, "mbX_18c9aa01", "sum!")
    _check("index entry + summary stored",
           workspace.mail_threads(workspace.load_workspace(CLIENT))[0]["summary"] == "sum!")
    workspace.upsert_mail_thread_entry(CLIENT, {"id": "mbX_18c9aa01", "subject": "S2",
                                                "last_date": "2026-07-09T00:00:00Z",
                                                "message_count": 2, "mailbox": "x@y.com"})
    ws_now = workspace.load_workspace(CLIENT)
    _check("upsert keeps the summary when the update carries none",
           workspace.mail_threads(ws_now)[0]["summary"] == "sum!"
           and workspace.mail_threads(ws_now)[0]["subject"] == "S2")
    workspace.delete_mail_thread(CLIENT, "mbX_18c9aa01")
    _check("delete removes index entry + object",
           workspace.mail_threads(workspace.load_workspace(CLIENT)) == []
           and not os.path.isfile(obj_path))

    # --- sync_client end-to-end (everything injected) -------------------------------------------------
    result = mailroom.sync_client(CLIENT, mailboxes=workspace.mail_mailboxes(),
                                  poster=_dwd_poster, getter=_gmail_getter,
                                  token_fetcher=lambda: "gcp-token",
                                  imap_factory=lambda mb: _FakeImap(), ai_fetcher=_ai_fetcher)
    _check("sync pulls every thread now (human + security + noise from dwd, human from imap)",
           result["ok"] and result["new_threads"] == 4)
    ws = workspace.load_workspace(CLIENT)
    entries = workspace.mail_threads(ws)
    tiers = sorted(e.get("tier") for e in entries)
    _check("entries carry tiers (security/client/operations-or-client/noise)",
           "security" in tiers and "noise" in tiers and "client" in tiers)
    _check("noise threads are NOT summarized (cost control)", result["summarized"] == 3
           and all(not e.get("summary") for e in entries if e.get("tier") == "noise"))
    digest_body = (ws.get("mail") or {}).get("digest", {}).get("body", "")
    _check("digest written with a REPLIES judgement", "NEEDS ACTION" in digest_body
           and "REPLIES" in digest_body)
    _check("digest was judged against the COMPUTED responsiveness numbers", "[stats-ok]" in digest_body)
    by_subject = {e["subject"]: e for e in entries}
    _check("response stats stamped on the entries (imap thread awaits us, api thread answered in 25h)",
           by_subject["Landing page copy"]["awaiting_reply"] is True
           and by_subject["June budget"]["awaiting_reply"] is False
           and by_subject["June budget"]["avg_response_hours"] == 25.0)
    mirrors = [e for e in workspace.communications_list(ws)
               if e.get("channel") == "email" and str(e.get("id", "")).startswith("mail_")]
    client_entries = [e for e in entries if e.get("tier") == "client"]
    _check("ONLY client-tier recaps mirror to the Communications feed (not ops/security/noise)",
           len(mirrors) == len(client_entries) and len(mirrors) >= 1
           and all("Friendly recap" in e["summary"] for e in mirrors))
    _n_mirrors = len(mirrors)
    workspace.upsert_email_summary(CLIENT, mirrors[0]["id"], mirrors[0]["title"], "updated recap")
    ws2 = workspace.load_workspace(CLIENT)
    again_mirrors = [e for e in workspace.communications_list(ws2)
                     if e.get("channel") == "email" and str(e.get("id", "")).startswith("mail_")]
    _check("re-mirroring UPDATES in place (no duplicates)",
           len(again_mirrors) == _n_mirrors and any(e["summary"] == "updated recap" for e in again_mirrors))
    _check("AI spend folded into the assistant tally",
           workspace.assistant_usage(ws)["calls"] >= 3)
    again = mailroom.sync_client(CLIENT, mailboxes=workspace.mail_mailboxes(),
                                 poster=_dwd_poster, getter=_gmail_getter,
                                 token_fetcher=lambda: "gcp-token",
                                 imap_factory=lambda mb: _FakeImap(), ai_fetcher=_ai_fetcher)
    _check("a re-run dedupes by message id (nothing new)",
           again["ok"] and again["new_messages"] == 0 and again["summarized"] == 0)
    none = mailroom.sync_client("no-such-client")
    _check("a missing workspace degrades", none["ok"] is False)

    # Archive-only sync (the Sync-now button path): pulls + archives with ZERO AI, so a fresh
    # client's mail lands fast and free. Prove no summaries/digest are written.
    import store as _store
    _store.add_client("archivetest", name="Archive Test")
    import seed_workspace as _sw
    # Reuse the seeded riverdance workspace shape via a fresh blank workspace.
    workspace.save_workspace("archivetest", {"display_name": "Archive Test", "mail": {}})
    workspace.set_mail_contacts("archivetest", "maya@riverdanceresort.com")
    ar = mailroom.sync_client("archivetest", mailboxes=[{"id": "mbD", "email": "info@agoradatadriven.com", "kind": "dwd"}],
                              poster=_dwd_poster, getter=_gmail_getter,
                              token_fetcher=lambda: "gcp-token", summarize=False)
    aws = workspace.load_workspace("archivetest")
    _check("archive-only sync pulls mail but writes NO summaries/digest",
           ar["ok"] and ar["new_threads"] >= 1 and ar["summarized"] == 0
           and not (aws.get("mail") or {}).get("digest", {}).get("body")
           and all(not t.get("summary") for t in workspace.mail_threads(aws))
           and all(t.get("tier") for t in workspace.mail_threads(aws)))

    # Refresh briefing = the on-demand AI pass over the archive-only client: it summarizes the
    # archived non-noise threads and builds the digest (nothing was summarized during the fast sync).
    rb = mailroom.refresh_briefing("archivetest", ai_fetcher=_ai_fetcher)
    aws2 = workspace.load_workspace("archivetest")
    _check("refresh_briefing summarizes archived threads on demand + builds a digest",
           rb["ok"] and rb["summarized"] >= 1 and "NEEDS ACTION" in (rb["digest"] or "")
           and any(t.get("summary") for t in workspace.mail_threads(aws2)))
    _check("refresh_briefing leaves noise unsummarized",
           all(not t.get("summary") for t in workspace.mail_threads(aws2) if t.get("tier") == "noise"))

    # --- mail_refresh: the job wrapper -----------------------------------------------------------------
    os.environ["MAIL_SYNC_ENABLED"] = "1"
    import store
    store.add_client(CLIENT, name="Riverdance RV Resort")   # refresh_all walks the registry
    summary = mail_refresh.refresh_all(poster=_dwd_poster, getter=_gmail_getter,
                                       token_fetcher=lambda: "gcp-token",
                                       imap_factory=lambda mb: _FakeImap(), ai_fetcher=_ai_fetcher)
    _check("refresh_all syncs the seeded client (no new mail on the re-run)",
           CLIENT in summary and summary[CLIENT]["new_messages"] == 0)

    # --- Assistant: mail chunks + fingerprint -----------------------------------------------------------
    ws = workspace.load_workspace(CLIENT)
    mail_threads = [workspace.read_mail_thread(CLIENT, t["id"]) for t in workspace.mail_threads(ws)]
    chunks = assistant_ai.build_chunks(ws, [], mail_threads=[t for t in mail_threads if t])
    mail_chunks = [ch for ch in chunks if ch["kind"] == "email"]
    _check("assistant chunks include the email threads + the responsiveness snapshot",
           len(mail_chunks) >= 3 and any("June budget" in ch["title"] for ch in mail_chunks))
    snap = next((ch for ch in mail_chunks if ch["id"] == "mail:responsiveness"), None)
    _check("assistant gets the computed responsiveness snapshot (the VA-accountability chunk)",
           snap is not None and "awaiting AGORA reply: YES" in snap["text"]
           and "25.0 hours" in snap["text"])
    fp1 = assistant_ai.fingerprint(ws, [])
    # Drop the OLDEST thread (the answered one) so the awaiting-reply thread survives for the
    # route render checks below.
    workspace.delete_mail_thread(CLIENT, workspace.mail_threads(ws)[-1]["id"])
    fp2 = assistant_ai.fingerprint(workspace.load_workspace(CLIENT), [])
    _check("fingerprint moves when mail changes", fp1 != fp2)

    # --- Routes: contacts / sync / thread GET / delete (sync monkeypatched) ------------------------------
    with c.session_transaction() as s:
        s.update(SUPER)
    r = c.post("/w/%s/admin/mail" % CLIENT, data={"op": "contacts", "contacts": "maya@riverdanceresort.com"})
    _check("op=contacts saves", r.status_code == 200 and r.get_json()["ok"] is True)
    body = c.get("/w/%s/conversations" % CLIENT).get_data(as_text=True)
    _check("Communications renders the folded-in Email intelligence panel (contacts + digest)",
           'data-pane="conversations"' in body and "maya@riverdanceresort.com" in body and "NEEDS ACTION" in body)
    _check("Communications shows the responsiveness strip",
           "ax-ml-statsrow" in body and "awaiting our reply" in body)
    _check("the old /w/<c>/mail URL still lands on Communications (back-compat)",
           'data-pane="conversations"' in c.get("/w/%s/mail" % CLIENT).get_data(as_text=True))
    _ws_cur = workspace.load_workspace(CLIENT)
    _cur_mirror = next((e for e in workspace.communications_list(_ws_cur)
                        if e.get("channel") == "email" and str(e.get("id", "")).startswith("mail_")), None)
    conv = c.get("/w/%s/conversations" % CLIENT).get_data(as_text=True)
    _check("the client-facing Communications tab carries the mirrored recap",
           _cur_mirror is not None and _cur_mirror["summary"] in conv)

    import mailroom as _mr
    real_sync = _mr.sync_client
    _seen = {}
    def _fake_sync(client, ws=None, summarize=True, **k):
        _seen["summarize"] = summarize
        return {"ok": True, "new_messages": 5, "new_threads": 1, "summarized": 0, "errors": [],
                "backlog": 2}
    _mr.sync_client = _fake_sync
    r = c.post("/w/%s/admin/mail" % CLIENT,
               data={"op": "sync", "contacts": "newcontact@meloyelo.nz"})
    _check("op=sync reports counts + is archive-only (summarize False) by default",
           r.get_json()["ok"] is True and r.get_json()["new_messages"] == 5
           and r.get_json()["backlog"] == 2 and _seen["summarize"] is False)
    _check("op=sync saved the contacts sent with it (no separate Save needed)",
           "newcontact@meloyelo.nz" in workspace.mail_contacts(workspace.load_workspace(CLIENT)))
    _mr.sync_client = real_sync

    key = workspace.mail_threads(workspace.load_workspace(CLIENT))[0]["id"]
    r = c.get("/w/%s/mail/thread/%s" % (CLIENT, key))
    _check("thread GET serves the full messages",
           r.status_code == 200 and len(r.get_json()["messages"]) >= 1)
    r = c.post("/w/%s/admin/mail" % CLIENT, data={"op": "delete", "thread_id": key})
    ws3 = workspace.load_workspace(CLIENT)
    _check("op=delete removes that thread from the index", r.get_json()["ok"] is True
           and key not in [t["id"] for t in workspace.mail_threads(ws3)])
    _check("op=delete also retracts the mirrored Communications entry",
           not any(e.get("id") == "mail_" + key for e in workspace.communications_list(ws3)))

    # --- Routes: the console Mailboxes pane ------------------------------------------------------------
    body = c.get("/admin/atrium").get_data(as_text=True)
    _check("console lists the connected mailboxes (password never rendered)",
           'data-pane="mailboxes"' in body and "projects@gmail.com" in body and ("q" * 16) not in body)
    r = c.post("/admin/mail", data={"op": "add", "email": "team@agoradatadriven.com", "kind": "dwd"})
    _check("console add redirects back to the pane",
           r.status_code == 302 and "section=mailboxes" in r.headers.get("Location", ""))
    _check("the dwd mailbox landed", any(b["email"] == "team@agoradatadriven.com"
                                         for b in workspace.mail_mailboxes()))
    real_test = _mr.test_mailbox
    _mr.test_mailbox = lambda mb, **k: (True, "Connected -- 42 threads visible.")
    r = c.post("/admin/mail", data={"op": "test", "mailbox_id": mb2["id"]})
    _check("op=test returns the connector's verdict", r.get_json()["ok"] is True
           and "42 threads" in r.get_json()["message"])
    _mr.test_mailbox = real_test
    r = c.post("/admin/mail", data={"op": "delete", "mailbox_id": mb2["id"]})
    _check("console delete disconnects", r.status_code == 302
           and workspace.find_mailbox(mb2["id"]) is None)

    # --- Team-only gating: a client must never see or touch Mail ---------------------------------------
    with c.session_transaction() as s:
        s.clear()
        s.update(CLIENT_LOGIN)
    body = c.get("/w/%s/mail" % CLIENT).get_data(as_text=True)
    _check("client hitting /mail is bounced (no mail pane in the DOM)",
           'data-pane="mail"' not in body)
    _check("client POST is forbidden",
           c.post("/w/%s/admin/mail" % CLIENT, data={"op": "sync"}).status_code == 403)
    _check("client thread GET is forbidden",
           c.get("/w/%s/mail/thread/whatever" % CLIENT).status_code == 403)
    _check("client mailbox management is forbidden",
           c.post("/admin/mail", data={"op": "delete", "mailbox_id": "x"}).status_code == 403)


if __name__ == "__main__":
    try:
        run()
        print("PASS")
    except AssertionError as exc:
        print("FAIL: %s" % exc)
        sys.exit(1)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
