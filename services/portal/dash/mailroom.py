"""Client email intelligence for the Atrium 'Mail' tab (team-only) -- pull, archive, AI-summarize.

Connect the agency's mailboxes once (operator console -> Mailboxes), list each client's contact
emails/domains in their workspace, and the sync pulls ONLY the correspondence with those contacts,
archives it per client, and has the intel brain summarize each thread + keep a rolling per-client
digest. Two connector kinds, both keyless-on-disk (no secret files, no new deps):

  * dwd  -- a mailbox in OUR Google Workspace domain, read via the Gmail API with domain-wide
            delegation. The Workspace admin grants the dedicated `mail-sync` service account the
            gmail.readonly scope ONCE (enable_atrium_mail.ps1 prints the exact instructions); after
            that any @agoradatadriven.com mailbox connects by just typing its address. Tokens are
            minted per-run, KEYLESSLY: the runtime SA asks the IAM Credentials API to signJwt as the
            mail-sync SA (same signBlob posture as the large-creative signed uploads), then exchanges
            that JWT at Google's token endpoint for a one-hour Gmail access token. Nothing stored.
  * imap -- ANY other Google account the team holds credentials for (the "client gives us a custom
            email" case): generate an App Password on that account (needs 2-Step Verification) and
            paste it into the console. The sync reads over IMAP (stdlib imaplib + email -- no new
            package) using Gmail's X-GM-RAW extension, so the SAME Gmail query works on both
            connectors, and the All-Mail folder covers received AND sent.

Matching is query-side, not filter-side: the Gmail query is built FROM the client's contact list
(from:/to: each address or domain), so only client correspondence ever leaves the mailbox -- the
sync never trawls unrelated mail.

Gated + graceful, mirroring watcher.py/intel_ai.py: every failure returns a short human reason and
never raises to a route; a mailbox that can't connect is reported and skipped; no AI provider means
threads still archive, they just aren't summarized. All transports are injectable for tests
(_mail_localtest.py runs the whole pipeline with no network).
"""

import base64
import json
import os
import re

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
# signJwt on the DELEGATED SA (projects/- resolves the project from the SA email). The caller (the
# runtime SA) needs roles/iam.serviceAccountTokenCreator ON that SA -- enable_atrium_mail.ps1.
IAM_SIGNJWT = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/%s:signJwt"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

MAILBOX_KINDS = ("dwd", "imap")

# Per-run safety caps: one sync request must stay comfortably inside the web service's 300s cap
# (the hourly job has 900s). Dedup by message id makes re-runs cheap, so a capped first backfill
# simply finishes over the next few runs.
MAX_THREADS_PER_RUN = 60        # full-thread fetches per mailbox, per client, per sync
MAX_MESSAGES_PER_THREAD = 50
MAX_IMAP_MESSAGES = 300         # message BODIES fetched per mailbox per sync
MAX_LIST_IDS = 1000             # thread ids listed per run (the cheap pass that finds the backlog)
MAX_IMAP_SCAN = 2000            # UIDs scanned per run for their thread id (cheap batched pass)
_BODY_MAX_CHARS = 12000         # one message's stored body cap
_PARTICIPANT_CAP = 12
_TIMEOUT = 30

# How far back a sync looks. The FIRST sync for a client backfills a longer window; after that each
# run only re-queries a short overlap (message-id dedup absorbs the overlap). Env-tunable.
def first_sync_days():
    try:
        return int(os.environ.get("MAIL_FIRST_SYNC_DAYS", "90"))
    except (TypeError, ValueError):
        return 90


def sync_days():
    try:
        return int(os.environ.get("MAIL_SYNC_DAYS", "7"))
    except (TypeError, ValueError):
        return 7


def dwd_sa():
    """The domain-wide-delegation service account email (MAIL_DWD_SA), or "" when unset."""
    return os.environ.get("MAIL_DWD_SA", "").strip()


def dwd_configured():
    """True iff Workspace-mailbox (dwd) connections can mint tokens on this deploy."""
    return bool(dwd_sa())


# --- Transports (the ONLY networked code; injectable for tests) ----------------------------------
def _requests_post(url, headers, payload, timeout, form=False):
    """Default POST fetcher: JSON body normally, form-encoded when `form` (the token exchange)."""
    import requests  # lazy, matching the rest of the app
    if form:
        return requests.post(url, headers=headers, data=payload, timeout=timeout)
    return requests.post(url, headers=headers, json=payload, timeout=timeout)


def _requests_get(url, headers, params, timeout):
    """Default GET fetcher (Gmail API reads)."""
    import requests  # lazy
    return requests.get(url, headers=headers, params=params, timeout=timeout)


# --- Contact matching ----------------------------------------------------------------------------
def clean_contacts(contacts):
    """Normalize a contact list to lowercase emails/domains (junk dropped, order kept, de-duped).

    Accepts full addresses ("maya@riverdance.com") and bare domains ("riverdance.com") -- Gmail's
    from:/to: operators match either form."""
    out, seen = [], set()
    for c in contacts or []:
        c = (c or "").strip().lower().lstrip("@")
        if not c or " " in c or "." not in c:
            continue
        if not re.match(r"^[a-z0-9][a-z0-9._%+-]*(@[a-z0-9.-]+)?\.[a-z]{2,}$", c):
            continue
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def gmail_query(contacts, days=None):
    """The Gmail search covering all correspondence with `contacts` ("" when no usable contact).

    `{a b}` is Gmail's OR group; from:/to: accept an address or a bare domain. The SAME string works
    on the Gmail API (q=) and on IMAP (X-GM-RAW), which is what keeps the two connectors identical.
    """
    cleaned = clean_contacts(contacts)
    if not cleaned:
        return ""
    terms = []
    for c in cleaned:
        terms.append("from:%s" % c)
        terms.append("to:%s" % c)
    q = "{%s} -in:chats -category:promotions -category:social" % " ".join(terms)
    if days:
        q += " newer_than:%dd" % int(days)
    return q


# --- DWD: keyless delegated Gmail token -----------------------------------------------------------
def dwd_access_token(mailbox_email, poster=None, token_fetcher=None, now=None):
    """A one-hour Gmail access token for `mailbox_email`, minted via the delegation SA.

    Returns (token, error). Keyless: the runtime SA calls iamcredentials signJwt AS the mail-sync
    SA (it holds tokenCreator on it), and the signed assertion (iss=mail-sync, sub=the mailbox) is
    exchanged at Google's token endpoint. `poster`/`token_fetcher`/`now` are test seams."""
    sa = dwd_sa()
    if not sa:
        return "", "Workspace delegation isn't configured on this deploy (run enable_atrium_mail.ps1)"
    import intel_ai  # reuse the runtime-SA token plumbing (incl. the VERTEX_ACCESS_TOKEN dev override)
    gcp_token = intel_ai._gcp_access_token(token_fetcher)
    if not gcp_token:
        return "", "could not get GCP credentials to sign the delegation token"
    if now is None:
        import time
        now = int(time.time())
    claims = {"iss": sa, "sub": (mailbox_email or "").strip().lower(), "scope": GMAIL_SCOPE,
              "aud": TOKEN_ENDPOINT, "iat": int(now), "exp": int(now) + 3600}
    fn = poster or _requests_post
    try:
        resp = fn(IAM_SIGNJWT % sa,
                  {"Authorization": "Bearer " + gcp_token, "Content-Type": "application/json"},
                  {"payload": json.dumps(claims)}, _TIMEOUT)
    except Exception as exc:
        return "", "could not reach the IAM signing API (%s)" % type(exc).__name__
    if getattr(resp, "status_code", 0) >= 400:
        return "", ("the runtime SA can't sign as %s -- grant it Token Creator on that SA "
                    "(enable_atrium_mail.ps1)" % sa)
    signed = (_safe_json(resp) or {}).get("signedJwt", "")
    if not signed:
        return "", "the IAM API returned no signed token"
    try:
        resp = fn(TOKEN_ENDPOINT, {"Content-Type": "application/x-www-form-urlencoded"},
                  {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                   "assertion": signed}, _TIMEOUT, form=True)
    except Exception as exc:
        return "", "could not reach Google's token endpoint (%s)" % type(exc).__name__
    data = _safe_json(resp) or {}
    if getattr(resp, "status_code", 0) >= 400 or not data.get("access_token"):
        desc = (data.get("error_description") or data.get("error") or "").lower()
        if ("unauthorized_client" in desc or "access_denied" in desc
                or "unauthorized to retrieve access tokens" in desc
                or "not authorized for any of the scopes" in desc):
            return "", ("the Workspace domain-wide-delegation grant for %s isn't in effect -- in "
                        "admin.google.com -> Security -> API controls -> Domain-wide delegation, "
                        "authorize the mail-sync client id (enable_atrium_mail.ps1 prints it) with "
                        "scope gmail.readonly; a fresh grant can take a few minutes to propagate"
                        % mailbox_email)
        return "", "token exchange failed (%s)" % (desc[:120] or "no detail")
    return data["access_token"], ""


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return None


# --- DWD: Gmail API pull ---------------------------------------------------------------------------
def gmail_pull(token, query, getter=None, max_threads=MAX_THREADS_PER_RUN, known=None):
    """All matching threads via the Gmail API, normalized. Returns (threads, error, backlog).

    Each thread: {id, subject, participants[], last_date, messages:[{id, from, to, date, body}]}.
    The LISTING pass is cheap and wide (up to MAX_LIST_IDS ids); full fetches are capped at
    `max_threads`. `known` = thread ids already archived for this mailbox: UNARCHIVED threads are
    fetched first (so repeated runs drain a big backfill window instead of refetching the same
    newest ones), then known threads with the remaining budget (to catch new replies). `backlog` =
    matching unarchived threads left unfetched this run -- 0 means the window is fully drained."""
    fn = getter or _requests_get
    headers = {"Authorization": "Bearer " + token}
    known = known or set()
    ids, page = [], ""
    try:
        while len(ids) < MAX_LIST_IDS:
            params = {"q": query, "maxResults": 100}
            if page:
                params["pageToken"] = page
            resp = fn(GMAIL_API + "/threads", headers, params, _TIMEOUT)
            if getattr(resp, "status_code", 0) >= 400:
                return None, _gmail_error(resp), 0
            data = _safe_json(resp) or {}
            ids += [t.get("id") for t in data.get("threads") or [] if t.get("id")]
            page = data.get("nextPageToken") or ""
            if not page:
                break
    except Exception as exc:
        return None, "could not reach the Gmail API (%s)" % type(exc).__name__, 0
    fresh = [t for t in ids if t not in known]
    order = fresh[:max_threads]
    order += [t for t in ids if t in known][:max_threads - len(order)]
    backlog = max(0, len(fresh) - max_threads)
    threads = []
    for tid in order:
        try:
            resp = fn(GMAIL_API + "/threads/" + tid, headers, {"format": "full"}, _TIMEOUT)
        except Exception as exc:
            return threads, "thread fetch failed (%s)" % type(exc).__name__, backlog
        if getattr(resp, "status_code", 0) >= 400:
            continue  # one unreadable thread must not sink the run
        t = _normalize_api_thread(_safe_json(resp) or {})
        if t:
            threads.append(t)
    return threads, "", backlog


def _gmail_error(resp):
    """A SHORT human reason from a Gmail API error response."""
    data = _safe_json(resp) or {}
    msg = ((data.get("error") or {}).get("message") or "") if isinstance(data.get("error"), dict) else ""
    low = msg.lower()
    if "delegation denied" in low or "forbidden" in low or getattr(resp, "status_code", 0) == 403:
        return "Gmail refused access for this mailbox -- check the domain-wide-delegation grant"
    if getattr(resp, "status_code", 0) == 401:
        return "the Gmail token was rejected (delegation problem)"
    return (msg[:120] or "Gmail API error (HTTP %s)" % getattr(resp, "status_code", "?"))


def _normalize_api_thread(raw):
    """One Gmail-API thread -> the normalized shape (None when it holds nothing usable).

    Machine-sent messages (robots, receipts, bulk -- see is_automated) are dropped per message;
    a thread that is ALL machine mail returns None and never lands in the archive."""
    msgs = []
    for m in (raw.get("messages") or [])[:MAX_MESSAGES_PER_THREAD]:
        heads = {(h.get("name") or "").lower(): h.get("value") or ""
                 for h in ((m.get("payload") or {}).get("headers") or [])}
        labels = m.get("labelIds") or []
        if "SPAM" in labels or "CATEGORY_PROMOTIONS" in labels:
            continue
        if is_automated(heads.get("from", ""), heads):
            continue
        body = _api_body_text(m.get("payload") or {}) or (m.get("snippet") or "")
        msgs.append({
            "id": m.get("id") or "",
            "from": heads.get("from", ""),
            "to": ", ".join(filter(None, [heads.get("to", ""), heads.get("cc", "")])),
            "date": _internal_date_iso(m.get("internalDate")),
            "subject": heads.get("subject", ""),
            "body": clean_body(body),
        })
    if not msgs:
        return None
    msgs.sort(key=lambda m: m.get("date") or "")
    return _finish_thread(raw.get("id") or "", msgs)


def _internal_date_iso(ms):
    """Gmail's internalDate (epoch millis, as a string) -> ISO-8601 Z ('' when absent/garbage)."""
    try:
        import datetime
        dt = datetime.datetime.fromtimestamp(int(ms) / 1000.0, datetime.timezone.utc)
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return ""


def _api_body_text(payload):
    """The best text body from a Gmail-API payload tree: text/plain preferred, else stripped HTML."""
    plain, html = [], []

    def walk(part):
        mime = part.get("mimeType") or ""
        data = ((part.get("body") or {}).get("data") or "")
        if data and mime.startswith("text/"):
            try:
                text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            except Exception:
                text = ""
            if text:
                (plain if mime == "text/plain" else html).append(text)
        for p in part.get("parts") or []:
            walk(p)

    walk(payload)
    return "\n".join(plain) if plain else _strip_html("\n".join(html))


# --- IMAP: app-password pull ------------------------------------------------------------------------
def _imap_error_text(exc):
    """A readable message out of an imaplib exception (whose args are often raw BYTES)."""
    parts = []
    for a in getattr(exc, "args", ()) or ():
        parts.append(a.decode("utf-8", "replace") if isinstance(a, bytes) else str(a))
    return (" ".join(p for p in parts if p) or str(exc)).strip()


def _imap_friendly(exc, email):
    """Step-by-step guidance for the connect failures operators actually hit (None = not one)."""
    up = _imap_error_text(exc).upper()
    if "APPLICATION-SPECIFIC PASSWORD REQUIRED" in up:
        return ("%s needs an APP password -- its normal Google password never works over IMAP. "
                "Sign into that account, turn on 2-Step Verification, create an app password at "
                "myaccount.google.com/apppasswords, then re-add this mailbox with the 16-character "
                "code. (If that page says the setting isn't available, the account's Workspace "
                "admin has app passwords disabled.)" % (email or "this mailbox"))
    if "AUTHENTICATIONFAILED" in up or "INVALID CREDENTIALS" in up:
        return ("Gmail rejected the app password for %s -- regenerate it "
                "(myaccount.google.com/apppasswords) and re-add the mailbox to replace it."
                % (email or "this mailbox"))
    return None


def _imap_connect(mb):
    """A logged-in imaplib connection for mailbox dict `mb`, selected on All Mail (read-only).

    All Mail (the folder LIST flags as \\All) covers received AND sent in one place; when it can't
    be found (non-Gmail host) we fall back to INBOX. Raises on connect/login failure -- the caller
    turns that into a friendly error."""
    import imaplib
    host = (mb.get("host") or "imap.gmail.com").strip()
    conn = imaplib.IMAP4_SSL(host, 993)
    conn.login(mb.get("email") or "", mb.get("app_password") or "")
    box = "INBOX"
    try:
        typ, listing = conn.list()
        for row in listing or []:
            line = row.decode("utf-8", "replace") if isinstance(row, bytes) else str(row)
            if "\\All" in line:
                m = re.search(r'"([^"]+)"\s*$', line)
                if m:
                    box = '"%s"' % m.group(1)
                break
    except Exception:
        pass
    typ, _ = conn.select(box, readonly=True)
    if typ != "OK":
        conn.select("INBOX", readonly=True)
    return conn


def _imap_thrid_scan(conn, uids):
    """Cheap batched pass: {uid_bytes: hex_thread_id} for `uids` WITHOUT downloading bodies.

    One FETCH per 100 UIDs asking only for X-GM-THRID -- this is what lets a big backfill window
    be mapped in seconds so the expensive body fetches go to unarchived threads first."""
    out = {}
    for i in range(0, len(uids), 100):
        batch = b",".join(uids[i:i + 100])
        try:
            typ, fetched = conn.uid("FETCH", batch, "(X-GM-THRID)")
        except Exception:
            continue
        if typ != "OK" or not fetched:
            continue
        for item in fetched:
            blob = item[0] if isinstance(item, tuple) else item
            if not isinstance(blob, bytes):
                continue
            mt = re.search(rb"X-GM-THRID\s+(\d+)", blob)
            mu = re.search(rb"UID\s+(\d+)", blob)
            if mt:
                uid = (mu.group(1) if mu else blob.split(b" ", 1)[0])
                out[uid] = _hex_thrid(mt.group(1).decode("ascii"))
    return out


def imap_pull(mb, query, imap_factory=None, max_threads=MAX_THREADS_PER_RUN, known=None):
    """All matching threads over IMAP (Gmail X-GM-RAW search), normalized.
    Returns (threads, error, backlog) -- the same contract as gmail_pull.

    Thread identity is Gmail's X-GM-THRID (hex-normalized so it matches the API's thread ids);
    message identity is the Message-ID header (UID fallback). A cheap batched X-GM-THRID scan maps
    the whole window first; message bodies are then fetched for UNARCHIVED (`known`) threads
    first, newest first, within the body budget -- so repeated runs drain a backfill window."""
    factory = imap_factory or _imap_connect
    known = known or set()
    try:
        conn = factory(mb)
    except Exception as exc:
        friendly = _imap_friendly(exc, mb.get("email"))
        if friendly:
            return None, friendly, 0
        return None, "could not connect to %s (%s: %s)" % (
            mb.get("host") or "imap.gmail.com", type(exc).__name__,
            _imap_error_text(exc)[:140] or "no detail"), 0
    try:
        try:
            typ, data = conn.uid("SEARCH", "X-GM-RAW", '"%s"' % query.replace('"', ""))
        except Exception:
            typ, data = "NO", None
        if typ != "OK":
            return None, "the mailbox refused the search (X-GM-RAW needs a Gmail-backed account)", 0
        uids = (data[0].split() if data and data[0] else [])[-MAX_IMAP_SCAN:]
        thrid_by_uid = _imap_thrid_scan(conn, uids)
        uids_by_thread = {}
        for uid in uids:  # search order is oldest->newest; keep that within each thread
            key = thrid_by_uid.get(uid) or ("uid-%s" % uid.decode("ascii", "replace"))
            uids_by_thread.setdefault(key, []).append(uid)
        # Newest threads first (by their newest UID); unarchived threads before known ones.
        ordered = sorted(uids_by_thread, key=lambda k: -max(int(u) for u in uids_by_thread[k]))
        ordered = ([k for k in ordered if k not in known] + [k for k in ordered if k in known])
        fresh_total = sum(1 for k in uids_by_thread if k not in known)
        threads, body_budget, fetched_fresh = [], MAX_IMAP_MESSAGES, 0
        for key in ordered:
            if len(threads) >= max_threads or body_budget <= 0:
                break
            msgs = []
            for uid in uids_by_thread[key][-MAX_MESSAGES_PER_THREAD:]:
                if body_budget <= 0:
                    break
                try:
                    typ, fetched = conn.uid("FETCH", uid, "(X-GM-THRID RFC822)")
                except Exception:
                    continue
                if typ != "OK" or not fetched:
                    continue
                body_budget -= 1
                _thrid, raw = _imap_parts(fetched)
                if raw is None:
                    continue
                msg = _parse_rfc822(raw, fallback_id="uid-%s" % uid.decode("ascii", "replace"))
                if msg is not None:
                    msgs.append(msg)
            if not msgs:
                continue
            msgs.sort(key=lambda m: m.get("date") or "")
            threads.append(_finish_thread(key, msgs))
            if key not in known:
                fetched_fresh += 1
        threads.sort(key=lambda t: t.get("last_date") or "", reverse=True)
        return threads, "", max(0, fresh_total - fetched_fresh)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _imap_parts(fetched):
    """(thrid_string, rfc822_bytes) out of one imaplib FETCH response (None, None when unusable)."""
    thrid, raw = "", None
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2:
            head = item[0].decode("utf-8", "replace") if isinstance(item[0], bytes) else str(item[0])
            m = re.search(r"X-GM-THRID\s+(\d+)", head)
            if m:
                thrid = m.group(1)
            raw = item[1]
        elif isinstance(item, bytes):
            m = re.search(rb"X-GM-THRID\s+(\d+)", item)
            if m:
                thrid = m.group(1).decode("ascii")
    return thrid, raw


def _hex_thrid(thrid):
    """Gmail's decimal X-GM-THRID -> the API's hex thread-id form ('' when absent/garbage)."""
    try:
        return format(int(thrid), "x")
    except (TypeError, ValueError):
        return ""


def _parse_rfc822(raw, fallback_id=""):
    """One raw RFC-822 message -> the normalized message dict (None when unparseable)."""
    import email
    import email.utils
    try:
        msg = email.message_from_bytes(raw if isinstance(raw, bytes) else bytes(raw))
    except Exception:
        return None
    if is_automated(_decode_header(msg.get("From")), dict(msg.items())):
        return None  # machine-sent (robot address / auto-submitted / bulk) -- not a person
    mid = (msg.get("Message-ID") or "").strip() or fallback_id
    date_iso = ""
    try:
        dt = email.utils.parsedate_to_datetime(msg.get("Date") or "")
        if dt is not None:
            import datetime
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            date_iso = (dt.astimezone(datetime.timezone.utc)
                        .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    except Exception:
        pass
    return {
        "id": mid,
        "from": _decode_header(msg.get("From")),
        "to": ", ".join(filter(None, [_decode_header(msg.get("To")), _decode_header(msg.get("Cc"))])),
        "date": date_iso,
        "subject": _decode_header(msg.get("Subject")),
        "body": clean_body(_rfc822_body_text(msg)),
    }


def _decode_header(value):
    """RFC-2047 header decode ('=?utf-8?...?=' -> readable text); plain values pass through."""
    if not value:
        return ""
    import email.header
    try:
        out = []
        for part, charset in email.header.decode_header(value):
            if isinstance(part, bytes):
                out.append(part.decode(charset or "utf-8", "replace"))
            else:
                out.append(part)
        return "".join(out).strip()
    except Exception:
        return str(value)


def _rfc822_body_text(msg):
    """The best text body of an email.message: text/plain preferred, else stripped HTML."""
    plain, html = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        if (part.get("Content-Disposition") or "").lower().startswith("attachment"):
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        (plain if ctype == "text/plain" else html).append(text)
    return "\n".join(plain) if plain else _strip_html("\n".join(html))


# --- Shared normalization ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def _finish_thread(tid, msgs):
    """Stamp the thread-level fields derived from its (date-sorted) messages."""
    participants, seen = [], set()
    for m in msgs:
        for addr in _EMAIL_RE.findall("%s %s" % (m.get("from", ""), m.get("to", ""))):
            a = addr.lower()
            if a not in seen and len(participants) < _PARTICIPANT_CAP:
                seen.add(a)
                participants.append(a)
    subject = ""
    for m in msgs:
        s = re.sub(r"^\s*((re|fwd?|fw)\s*:\s*)+", "", m.get("subject") or "", flags=re.I).strip()
        if s:
            subject = s
            break
    return {"id": tid, "subject": subject or "(no subject)", "participants": participants,
            "last_date": msgs[-1].get("date") or "", "messages": msgs}


def _strip_html(html):
    """Crude-but-safe HTML -> text: drop script/style, tags, entities; collapse whitespace."""
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    import html as _html
    text = _html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n\s*", "\n\n", text)).strip()


def clean_body(text):
    """Trim a message body for storage: drop quoted-reply tails and cap the length.

    Quoted chains ('>' lines and the 'On ... wrote:' marker onward) balloon every reply with the
    whole prior thread; the thread archive already holds each message once, so the quotes are pure
    duplication -- for storage, retrieval, AND the summarizer's token bill."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    m = re.search(r"(?m)^\s*On .{4,120}wrote:\s*$", text)
    if m:
        text = text[:m.start()]
    lines = [ln for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", "\n".join(lines)).strip()
    if len(text) > _BODY_MAX_CHARS:
        text = text[:_BODY_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return text


# --- Focus on real people: drop machine-sent mail ----------------------------------------------------
# The query already restricts to the client's contacts, but a domain contact (riverdance.com) also
# matches that domain's robots -- receipts, notifications, bounces, newsletters. Those poison the
# summaries and the digest, so they are dropped per MESSAGE at parse time (a thread that is ALL
# machine mail simply never lands). Deliberately CONSERVATIVE: List-Unsubscribe alone never counts
# (Google Groups stamps it onto real human mail relayed through a client's group alias); the
# triggers are a robot sender address, Auto-Submitted, Precedence bulk/junk/auto_reply, and the
# autoresponder headers.
_AUTOMATED_FROM = re.compile(
    r"(?:^|[<\s\"'])(?:no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|"
    r"bounces?[^@\s]*|notifications?|alerts?|newsletters?)@", re.I)


def is_automated(sender, headers):
    """True when a message is machine-sent (robot address / auto-submitted / bulk), not a person."""
    if _AUTOMATED_FROM.search(sender or ""):
        return True
    h = {(k or "").lower(): (v or "") for k, v in (headers or {}).items()}
    auto = h.get("auto-submitted", "").strip().lower()
    if auto and auto != "no":
        return True
    if h.get("precedence", "").strip().lower() in ("bulk", "junk", "auto_reply"):
        return True
    if h.get("x-autoreply") or h.get("x-autorespond"):
        return True
    return False


# --- Response stats: who owes whom, and how fast do WE reply -----------------------------------------
# Pure code over the archived messages (no AI): is the last word the client's, and how long do
# client messages sit before an AGORA reply lands? These numbers feed the Mail tab's stats strip,
# the digest's REPLIES section, and the Assistant's responsiveness snapshot -- so "is the person
# answering our email doing a good job?" gets judged against real, computed numbers.
def _iso_dt(s):
    import datetime
    try:
        return datetime.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _sender_email(text):
    m = _EMAIL_RE.search(text or "")
    return m.group(0).lower() if m else ""


def thread_stats(thread, agency_addrs, agency_domains=()):
    """{awaiting_reply, avg_response_hours, replies_measured} for one thread.

    A message is "agency-side" when its sender is a connected mailbox address, or shares a
    Workspace (dwd) mailbox's domain -- so a VA answering from info@ counts as us. The response
    clock starts at the FIRST unanswered client message and stops at the next agency reply."""
    waiting_from, deltas, last_is_agency = None, [], None
    for m in thread.get("messages") or []:
        sender = _sender_email(m.get("from", ""))
        is_agency = bool(sender) and (sender in agency_addrs or sender.split("@")[-1] in agency_domains)
        d = _iso_dt(m.get("date"))
        if is_agency:
            if waiting_from is not None and d is not None:
                deltas.append((d - waiting_from).total_seconds() / 3600.0)
            waiting_from = None
        elif waiting_from is None and d is not None:
            waiting_from = d
        last_is_agency = is_agency
    return {
        "awaiting_reply": last_is_agency is False,
        "avg_response_hours": (round(sum(deltas) / len(deltas), 1) if deltas else None),
        "replies_measured": len(deltas),
    }


def stats_line(entries):
    """One factual sentence of responsiveness numbers from the INDEX entries ('' when empty).

    Handed to the digest model verbatim (computed, so it can't hallucinate the numbers) and shown
    nowhere else -- the Mail tab renders the same numbers from the view model."""
    entries = [e for e in entries or [] if e.get("id")]
    if not entries:
        return ""
    waiting = [e for e in entries if e.get("awaiting_reply")]
    hours = [e["avg_response_hours"] for e in entries
             if isinstance(e.get("avg_response_hours"), (int, float))]
    bits = ["%d thread(s)" % len(entries), "%d awaiting an AGORA reply" % len(waiting)]
    dates = [e.get("last_date") for e in waiting if e.get("last_date")]
    if dates:
        bits.append("oldest waiting since %s" % min(dates)[:10])
    if hours:
        bits.append("average AGORA reply time %.1f hours" % (sum(hours) / len(hours)))
    return "Responsiveness numbers (computed from the raw messages, trustworthy): %s." % "; ".join(bits)


# --- Connection test (the console's Test button) -----------------------------------------------------
def test_mailbox(mb, poster=None, getter=None, token_fetcher=None, imap_factory=None):
    """Prove a mailbox connects. Returns (ok, message) -- message is human-facing either way."""
    kind = (mb or {}).get("kind") or ""
    if kind == "dwd":
        token, err = dwd_access_token(mb.get("email", ""), poster=poster, token_fetcher=token_fetcher)
        if err:
            return False, err
        fn = getter or _requests_get
        try:
            resp = fn(GMAIL_API + "/profile", {"Authorization": "Bearer " + token}, {}, _TIMEOUT)
        except Exception as exc:
            return False, "could not reach the Gmail API (%s)" % type(exc).__name__
        if getattr(resp, "status_code", 0) >= 400:
            return False, _gmail_error(resp)
        data = _safe_json(resp) or {}
        return True, "Connected -- %s threads visible." % (data.get("threadsTotal", "?"))
    if kind == "imap":
        factory = imap_factory or _imap_connect
        try:
            conn = factory(mb)
        except Exception as exc:
            friendly = _imap_friendly(exc, mb.get("email"))
            if friendly:
                return False, friendly
            return False, "could not connect (%s)" % (_imap_error_text(exc)[:140]
                                                      or type(exc).__name__)
        try:
            conn.logout()
        except Exception:
            pass
        return True, "Connected."
    return False, "unknown mailbox kind"


# --- AI: per-thread summary + the rolling client digest ----------------------------------------------
def _mail_model(ws):
    """The model the mail brain uses: the Assistant's choice -> the intel brain's -> the default."""
    import intel_ai
    for mid in (((ws or {}).get("assistant") or {}).get("model") or "",
                ((ws or {}).get("intel_ai") or {}).get("model") or "",
                intel_ai.default_model()):
        mid = (mid or "").strip()
        if mid and intel_ai.model_available(mid):
            return mid
    return ""


_SUMMARY_SYSTEM = (
    "You summarize one email thread between the marketing agency AGORA and its client \"%s\". "
    "Some AGORA replies may be written by an assistant answering on the agency's behalf. "
    "Return STRICT JSON with TWO summaries and nothing else:\n"
    "  \"summary\" -- 1-3 sentences for AGORA's INTERNAL workspace: what the thread is about, any "
    "decision made, what is still open and WHO owes the next reply. Be concrete -- names, dates, "
    "amounts. If an AGORA reply looks slow, curt, wrong, or unhelpful, say so bluntly here.\n"
    "  \"client_summary\" -- 1-2 sentences the CLIENT reads in their own workspace feed: a clear, "
    "warm-professional recap of what was discussed and decided in this thread. They were ON the "
    "thread, so nothing is secret -- but no internal notes, no performance commentary, no "
    "'who owes whom'.\n"
    "Shape: {\"summary\": \"...\", \"client_summary\": \"...\"}"
)


def summarize_thread(client_name, thread, model, ai_fetcher=None, token_fetcher=None, usage_out=None):
    """One thread -> (internal_summary, client_summary, error) in ONE model call.

    The internal summary runs the Mail tab + digest (blunt, includes reply-quality observations);
    the client summary is the friendly recap the sync mirrors onto the client-visible
    Communications tab. ("", "", reason) on failure."""
    import intel_ai
    meta = intel_ai.model_meta(model)
    if meta is None:
        return "", "", "no AI model available"
    lines = ["Subject: %s" % thread.get("subject", "")]
    for m in (thread.get("messages") or [])[-12:]:
        body = (m.get("body") or "")[:1500]
        lines.append("--- %s | from %s | to %s\n%s"
                     % (m.get("date", ""), m.get("from", ""), m.get("to", ""), body))
    raw, err, _think = intel_ai._call(meta, _SUMMARY_SYSTEM % (client_name or "the client"),
                                      "\n".join(lines), ai_fetcher, 1024, token_fetcher,
                                      usage_out=usage_out, think=False)
    if err:
        return "", "", err
    obj = intel_ai._parse_json(raw)
    if not isinstance(obj, dict) or not str(obj.get("summary") or "").strip():
        return "", "", "the model returned nothing usable"
    return (str(obj.get("summary")).strip(),
            str(obj.get("client_summary") or "").strip(), "")


_DIGEST_SYSTEM = (
    "You keep AGORA's internal relationship briefing for the client \"%s\", built from the summaries "
    "of every recent email thread with them. Write a SHORT plain-text briefing the account team can "
    "scan in 30 seconds, exactly in this shape:\n"
    "STATUS: one sentence on the overall state of the relationship right now.\n"
    "NEEDS ACTION: one '- ' bullet per open item that needs an AGORA reply or delivery (who owes "
    "what, by when if stated). Write '- nothing outstanding' if truly nothing.\n"
    "RECENT: 2-4 '- ' bullets of what happened lately worth knowing.\n"
    "REPLIES: 1-2 '- ' bullets judging how well AGORA is keeping up with this client's email -- "
    "reply speed against the responsiveness numbers you are given (they are computed from the raw "
    "messages, trust them), threads left hanging, and any quality concerns visible in the thread "
    "summaries (replies that look rushed, wrong, or unhelpful). AGORA's replies may be written by "
    "an assistant on the agency's behalf -- judge them the way a manager reviewing that "
    "assistant's work would.\n"
    "No markdown headings beyond those four labels, no preamble. "
    "Return STRICT JSON and nothing else: {\"digest\": \"...\"}"
)


def build_digest(client_name, entries, model, ai_fetcher=None, token_fetcher=None, usage_out=None,
                 stats=""):
    """The rolling digest from the stored thread summaries. Returns (digest_text, error).

    `stats` is the computed responsiveness sentence (stats_line) -- passed verbatim so the REPLIES
    judgement rests on real numbers, not the model's guess."""
    import intel_ai
    meta = intel_ai.model_meta(model)
    if meta is None:
        return "", "no AI model available"
    rows = []
    for e in (entries or [])[:30]:
        if not e.get("summary"):
            continue
        rows.append("[%s] %s%s -- %s" % (e.get("last_date", "")[:10], e.get("subject", ""),
                                         " (awaiting an AGORA reply)" if e.get("awaiting_reply") else "",
                                         e.get("summary", "")))
    if not rows:
        return "", "no summarized threads yet"
    user = ((stats + "\n\n") if stats else "") + "Thread summaries, newest first:\n" + "\n".join(rows)
    raw, err, _think = intel_ai._call(meta, _DIGEST_SYSTEM % (client_name or "the client"),
                                      user,
                                      ai_fetcher, 2048, token_fetcher, usage_out=usage_out,
                                      think=False)
    if err:
        return "", err
    obj = intel_ai._parse_json(raw)
    digest = (obj or {}).get("digest") if isinstance(obj, dict) else None
    return (str(digest).strip(), "") if digest else ("", "the model returned nothing usable")


# --- The sync (what the hourly job AND the tab's Sync-now button run) ---------------------------------
def sync_client(client, ws=None, mailboxes=None, days=None, poster=None, getter=None,
                token_fetcher=None, imap_factory=None, ai_fetcher=None, summarize=True):
    """Pull + archive (+ optionally AI-summarize) one client's mail across every connected mailbox.

    Returns {ok, new_messages, new_threads, summarized, backlog, errors:[...]} and never raises.
    Always writes the per-thread archive objects + the small index + the last_sync stamp. When
    `summarize` is True it ALSO writes per-thread summaries, the rolling digest, the client-facing
    Communications mirror, and folds AI spend into the Assistant tally. `summarize=False` is the
    fast, free "just pull the mail" path (the Sync-now button) -- archiving only, no AI calls; the
    hourly job (summarize=True) fills the summaries later."""
    import intel_ai
    import workspace
    result = {"ok": True, "new_messages": 0, "new_threads": 0, "summarized": 0, "errors": []}
    if ws is None:
        ws = workspace.load_workspace(client)
    if ws is None:
        return {"ok": False, "new_messages": 0, "new_threads": 0, "summarized": 0,
                "errors": ["no workspace"]}
    contacts = workspace.mail_contacts(ws)
    if not clean_contacts(contacts):
        workspace.mark_mail_sync(client, error="no client contact emails set -- add them on the Mail tab")
        return {"ok": False, "new_messages": 0, "new_threads": 0, "summarized": 0,
                "errors": ["no client contact emails set"]}
    if mailboxes is None:
        mailboxes = workspace.mail_mailboxes()
    active = [m for m in mailboxes if m.get("email")]
    if not active:
        workspace.mark_mail_sync(client, error="no mailboxes connected -- add one in the operator console")
        return {"ok": False, "new_messages": 0, "new_threads": 0, "summarized": 0,
                "errors": ["no mailboxes connected"]}

    # The look-back window: the WIDE first-sync window stays in force until a run drains it
    # completely (backlog 0 across every mailbox -> the `backfilled` flag latches); only then do
    # runs drop to the short overlap window. So "scrape all of it" is a promise kept across runs,
    # not just a first attempt.
    mail_prev = ws.get("mail") or {}
    backfilled = bool(mail_prev.get("backfilled"))
    query = gmail_query(contacts, days=days or (sync_days() if backfilled else first_sync_days()))
    changed = []   # (key, thread_dict) for every thread that gained messages this run
    backlog_total = 0
    # Who counts as "us" for the response stats: every connected mailbox address, plus the whole
    # domain of a Workspace (dwd) mailbox -- so a VA answering from info@ is agency-side.
    agency_addrs = {(m.get("email") or "").lower() for m in active}
    agency_domains = {(m.get("email") or "").split("@")[-1].lower()
                      for m in active if m.get("kind") == "dwd" and "@" in (m.get("email") or "")}

    for mb in active:
        prefix = (mb.get("id") or "mb") + "_"
        already = {str(t.get("id", ""))[len(prefix):]
                   for t in (mail_prev.get("threads") or [])
                   if str(t.get("id", "")).startswith(prefix)}
        if mb.get("kind") == "dwd":
            token, err = dwd_access_token(mb.get("email", ""), poster=poster,
                                          token_fetcher=token_fetcher)
            if err:
                result["errors"].append("%s: %s" % (mb.get("email", "?"), err))
                continue
            threads, err, backlog = gmail_pull(token, query, getter=getter, known=already)
        else:
            threads, err, backlog = imap_pull(mb, query, imap_factory=imap_factory, known=already)
        backlog_total += backlog or 0
        if err:
            result["errors"].append("%s: %s" % (mb.get("email", "?"), err))
        for t in threads or []:
            # Thread ids are mailbox-local, so the archive key is scoped by mailbox: the same
            # conversation seen from two connected mailboxes stays two entries (honest + simple).
            key = "%s_%s" % (mb.get("id", "mb"), t["id"])
            stored = workspace.read_mail_thread(client, key)
            merged, added = _merge_thread(stored, t)
            if not added and stored is not None:
                continue
            merged["mailbox"] = mb.get("email", "")
            stats = thread_stats(merged, agency_addrs, agency_domains)
            merged["stats"] = stats
            workspace.write_mail_thread(client, key, merged)
            entry = {"id": key, "subject": merged.get("subject", ""),
                     "participants": merged.get("participants") or [],
                     "last_date": merged.get("last_date", ""),
                     "message_count": len(merged.get("messages") or []),
                     "mailbox": mb.get("email", ""),
                     "awaiting_reply": stats["awaiting_reply"],
                     "avg_response_hours": stats["avg_response_hours"],
                     "summary": (stored or {}).get("summary", "")}
            dropped = workspace.upsert_mail_thread_entry(client, entry)
            for d in dropped:
                workspace.delete_mail_thread_object(client, d)
            result["new_messages"] += added
            if stored is None:
                result["new_threads"] += 1
            changed.append((key, merged))

    # Summarize what changed + refresh the digest (best-effort; archiving already succeeded).
    # One model call per thread yields BOTH voices: the internal summary (Mail tab + digest) and
    # the client-facing recap, which is mirrored onto the client-visible Communications tab's
    # Email Summary feed under a stable 'mail_<key>' id (re-summarizing UPDATES the entry).
    model = _mail_model(ws) if summarize else ""
    if changed and summarize and model:
        name = ws.get("display_name") or client
        for key, thread in changed:
            usage = {}
            summary, client_summary, err = summarize_thread(
                name, thread, model, ai_fetcher=ai_fetcher,
                token_fetcher=token_fetcher, usage_out=usage)
            _tally(client, model, usage)
            if summary:
                thread["summary"] = summary
                workspace.write_mail_thread(client, key, thread)
                workspace.set_mail_thread_summary(client, key, summary)
                result["summarized"] += 1
                if client_summary:
                    try:
                        workspace.upsert_email_summary(client, "mail_" + key,
                                                       thread.get("subject", ""), client_summary,
                                                       date=thread.get("last_date", ""))
                    except Exception:
                        pass  # the mirror is a nicety; never let it break the sync
            elif err and err not in result["errors"]:
                result["errors"].append("summarize: %s" % err)
        fresh = workspace.load_workspace(client)
        fresh_entries = (fresh.get("mail") or {}).get("threads") or []
        usage = {}
        digest, err = build_digest(name, fresh_entries, model,
                                   ai_fetcher=ai_fetcher, token_fetcher=token_fetcher,
                                   usage_out=usage, stats=stats_line(fresh_entries))
        _tally(client, model, usage)
        if digest:
            workspace.set_mail_digest(client, digest)
        elif err and "no summarized threads" not in err:
            result["errors"].append("digest: %s" % err)
    elif changed and summarize and not model:
        result["errors"].append("no AI provider configured -- threads archived without summaries")

    workspace.mark_mail_sync(client, error="; ".join(dict.fromkeys(result["errors"]))[:400],
                             backlog=backlog_total)
    # Latch the backfill flag only when the wide window came back fully drained AND clean --
    # after that, runs use the short overlap window.
    if not backfilled and backlog_total == 0 and not result["errors"]:
        try:
            workspace.set_mail_backfilled(client)
        except Exception:
            pass
    result["ok"] = not result["errors"]
    result["backlog"] = backlog_total
    return result


def _merge_thread(stored, fresh):
    """Merge freshly-pulled messages into the stored thread object. Returns (merged, added_count)."""
    if stored is None:
        return dict(fresh), len(fresh.get("messages") or [])
    have = {m.get("id") for m in stored.get("messages") or []}
    added = [m for m in fresh.get("messages") or [] if m.get("id") and m.get("id") not in have]
    if not added:
        return stored, 0
    msgs = (stored.get("messages") or []) + added
    msgs.sort(key=lambda m: m.get("date") or "")
    merged = dict(stored)
    merged["messages"] = msgs[-MAX_MESSAGES_PER_THREAD:]
    finished = _finish_thread(stored.get("id") or fresh.get("id") or "", merged["messages"])
    merged.update({k: finished[k] for k in ("subject", "participants", "last_date")})
    return merged, len(added)


def _tally(client, model, usage):
    """Fold one AI call's tokens into the client's Assistant spend tally (best-effort)."""
    if not usage:
        return
    import intel_ai
    import workspace
    try:
        cost = intel_ai.cost_of(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        workspace.add_assistant_usage(client, model, usage.get("input_tokens", 0),
                                      usage.get("output_tokens", 0), cost)
    except Exception:
        pass
