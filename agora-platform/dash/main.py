"""Portal / CRM front-door for Agora Data Driven (Cloud Run service `platform-dash`).

What this is:
  ONE Cloud Run service at portal.agoradatadriven.com that is (a) a single login over every
  client dashboard and (b) a reverse proxy that serves each dashboard under /d/<client>/...
  behind that single login. It is designed to GROW INTO A CRM: the registry is one private JSON
  in GCS (agora-data-driven-platform-dash/platform.json) and the `# CRM:` markers below show
  where client records, notes, and tasks attach as the portal expands.

Auth model (two cooperating layers):
  1. Portal session -- a signed Flask session cookie set on successful /login.
  2. SSO -- on login the portal ALSO mints the shared .agoradatadriven.com-scoped cookie via
     platform_sso.mint_sso_cookie. That cookie is presented to every <c>.agoradatadriven.com
     dashboard so a portal login is trusted additively (each dashboard's own password still works).

Reverse proxy (/d/<client>/...):
  The portal logs into the upstream <client>-dash service ONCE, server-side, using that dash's own
  Secret-Manager password (store.get_client_dash_password), holds the upstream session in a cookie
  jar, and proxies requests/responses. It injects a small logout pill + feedback widget into proxied
  HTML so the user always has a way back to the portal. This means the end user only ever types the
  PORTAL password -- they never see the per-dashboard password.

Org policy forbids public Cloud Run: deploy with --no-invoker-iam-check (never
--allow-unauthenticated); this app does its OWN auth in-process.
"""

import datetime
import os

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import atrium_view
import brand
import notify
import platform_sso
import store
import workspace
import feedback as feedback_store

# Product name for the Atrium client workspace (kept in one place so it is easy to change).
WORKSPACE_NAME = "Agora Atrium"

# --- Configuration from the environment --------------------------------------------------
# SESSION_SECRET signs the Flask session cookie; mounted from Secret Manager at deploy time.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
# SSO_SECRET is the shared HMAC key (Secret Manager `platform-sso-key`) the portal signs the
# .agoradatadriven.com SSO cookie with and every dashboard verifies against.
SSO_SECRET = os.environ.get("SSO_SECRET", "")
# COOKIE_DOMAIN scopes the SSO cookie across all subdomains (leading dot). Defaults to the
# platform_sso constant so a missing env var is still correct.
COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", platform_sso.COOKIE_DOMAIN)
# REGION is needed to address the upstream Cloud Run dashboards (set by enable_super_admin.ps1).
REGION = os.environ.get("REGION", "asia-southeast1")

app = Flask(__name__)
app.secret_key = SESSION_SECRET

# Cookie hardening. SameSite=None + Secure is REQUIRED for the cross-subdomain portal flow
# (portal.agoradatadriven.com -> <c>.agoradatadriven.com): a Lax/Strict cookie would be dropped
# on the cross-site navigation. HttpOnly keeps the session out of JS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)
# Cap request bodies. The largest legitimate POST is a voice feedback note; keep it bounded.
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MiB

# Per-upstream-dashboard proxy sessions, keyed by client key. Each holds the upstream <c>-dash
# login cookie so we only log into a dashboard ONCE server-side, then reuse the session. This is
# an in-process cache; on a cold start it is rebuilt lazily on first proxy request.
_upstream_sessions = {}

# Hop-by-hop headers must NOT be forwarded when proxying (RFC 7230 sec 6.1) -- forwarding them
# corrupts the proxied response (e.g. a stale Content-Length, double Transfer-Encoding).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length",
}


# --- Auth helpers ------------------------------------------------------------------------
def authed():
    """True iff the current request has a valid portal session."""
    return session.get("ok") is True


def current_user():
    """The logged-in portal user's identifier (email), or None."""
    return session.get("user")


def allowed_clients():
    """Client keys this session may open -- ["*"] for super-admin, else a concrete list."""
    return session.get("clients") or []


def can_open(client_key):
    """True iff the session is allowed to open `client_key` (super-admin "*" opens everything)."""
    allowed = allowed_clients()
    return "*" in allowed or client_key in allowed


def is_superadmin():
    """A "*" grant marks a super-admin (the operator console)."""
    return "*" in allowed_clients()


def _visible_clients():
    """The client dicts this session is allowed to see, for the portal landing page."""
    clients = store.list_clients()
    if is_superadmin():
        return clients
    allowed = set(allowed_clients())
    return [c for c in clients if c.get("key") in allowed]


def _brand_ctx():
    """Shared brand assets for every rendered page (the AGORA mark + favicon, from brand.py).

    The deployed container only bundles dash/, so the mark lives in brand.py rather than being read
    from Creatives/ at runtime; this keeps the portal/login chrome in step with the Atrium sidebar.
    """
    return {"agora_logo": brand.AGORA_LOGO_LIGHT, "favicon": brand.FAVICON_DATA_URI}


# --- Routes: auth + landing --------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    if not authed():
        return redirect(url_for("login", next="/"))
    return render_template(
        "portal.html",
        user=current_user(),
        clients=_visible_clients(),
        is_admin=authed(),
        is_superadmin=is_superadmin(),
        **_brand_ctx(),
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "/")
    if request.method == "GET":
        if authed():
            return redirect(next_url or "/")
        return render_template("login.html", next=next_url, error=None, **_brand_ctx())

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    granted = store.verify_portal_login(email, password)
    if not granted:
        return render_template("login.html", next=next_url, email=email,
                               error="Incorrect email or password.", **_brand_ctx()), 401

    # Establish the portal session.
    session["ok"] = True
    session["user"] = email
    session["clients"] = granted

    resp = redirect(next_url or "/")
    # Mint the shared SSO cookie so the dashboards trust this portal login additively. Only do this
    # when the signing key is configured; without it SSO is simply inert (the dashboards' own
    # password gate still works), so a missing key must never break portal login.
    if SSO_SECRET:
        cookie_value = platform_sso.mint_sso_cookie(SSO_SECRET, granted, subject=email)
        resp.set_cookie(
            platform_sso.COOKIE_NAME,
            cookie_value,
            domain=COOKIE_DOMAIN,
            secure=True,
            httponly=True,
            samesite="None",
            max_age=platform_sso.DEFAULT_TTL_SECONDS,
        )
    return resp


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    resp = redirect(url_for("login"))
    # Clear the shared SSO cookie too (expire it on the same domain it was set).
    resp.set_cookie(
        platform_sso.COOKIE_NAME, "", domain=COOKIE_DOMAIN,
        secure=True, httponly=True, samesite="None", expires=0,
    )
    return resp


# --- Reverse proxy: /d/<client>/<path> ---------------------------------------------------
def _upstream_base_url(client_key):
    """Resolve the upstream dashboard's base URL.

    Prefer the client's subdomain (so the .agoradatadriven.com SSO cookie reaches it); fall back to
    the dash service name's default custom domain. The registry holds the canonical subdomain.
    """
    client = store.get_client(client_key)
    subdomain = (client or {}).get("subdomain") or ("%s.agoradatadriven.com" % client_key)
    return "https://%s" % subdomain


def _ensure_upstream_login(client_key):
    """Return a requests.Session logged into the upstream <client>-dash, creating it if needed.

    Logs in ONCE server-side using the dashboard's own Secret-Manager password and holds the
    upstream session cookie. Reused across proxied requests so the end user only ever enters the
    portal password.
    """
    sess = _upstream_sessions.get(client_key)
    if sess is not None:
        return sess

    sess = requests.Session()
    base = _upstream_base_url(client_key)
    try:
        dash_password = store.get_client_dash_password(client_key)
        # The dashboard's /login is a tiny form POST; a 302 (redirect to /) means success.
        sess.post(
            "%s/login" % base,
            data={"password": dash_password},
            allow_redirects=False,
            timeout=15,
        )
    except Exception:
        # Login failure is non-fatal here: we still return the session so the proxied request can
        # surface the upstream's own 401/login page rather than a 500. The user can then use the
        # dashboard's own password if needed.
        pass

    _upstream_sessions[client_key] = sess
    return sess


def _inject_portal_chrome(html, client_key):
    """Inject a logout pill + feedback widget into proxied dashboard HTML.

    Keeps the user oriented inside the portal frame: a way back/out, and a one-line feedback box
    that posts to the portal's own /feedback. Inserted just before </body> when present.
    """
    # Brand-aligned floating chrome: clean white pills + a green feedback CTA, readable on any
    # dashboard background (mirrors Creatives/brand.json -- green CTA, charcoal text).
    _pill = ("padding:7px 13px;border-radius:999px;background:#fff;border:1px solid #E7E8EE;"
             "text-decoration:none;font-size:12px;font-weight:600;"
             "box-shadow:0 4px 14px rgba(16,24,40,.16);")
    pill = (
        '<div id="ag-portal-chrome" style="position:fixed;top:12px;right:12px;z-index:2147483647;'
        'display:flex;gap:8px;align-items:center;font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">'
        '<a href="/" style="%scolor:#353535;">All dashboards</a>'
        '<a href="/logout" style="%scolor:#E5413E;">Log out</a>'
        '<a href="/" title="Send feedback" style="padding:7px 13px;border-radius:999px;'
        'background:#4FAB4A;color:#fff;text-decoration:none;font-size:12px;font-weight:700;'
        'box-shadow:0 4px 14px rgba(79,171,74,.34);">Feedback</a>'
        '</div>'
    ) % (_pill, _pill)
    marker = "</body>"
    if marker in html:
        return html.replace(marker, pill + marker, 1)
    return html + pill


@app.route("/d/<client>/", defaults={"subpath": ""}, methods=["GET", "POST"])
@app.route("/d/<client>/<path:subpath>", methods=["GET", "POST"])
def proxy(client, subpath):
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    if store.get_client(client) is None:
        return Response("Unknown dashboard", status=404, mimetype="text/plain")

    sess = _ensure_upstream_login(client)
    base = _upstream_base_url(client)
    target = "%s/%s" % (base, subpath)
    if request.query_string:
        target = "%s?%s" % (target, request.query_string.decode("latin-1"))

    # Forward the request to the upstream dashboard, preserving method and body. We drop the inbound
    # Host header so requests sets it from the target URL.
    fwd_headers = {k: v for k, v in request.headers if k.lower() != "host"}
    try:
        upstream = sess.request(
            method=request.method,
            url=target,
            headers=fwd_headers,
            data=request.get_data(),
            allow_redirects=False,
            timeout=30,
        )
    except Exception:
        return Response("Upstream dashboard unavailable.", status=502, mimetype="text/plain")

    # Strip hop-by-hop headers from the upstream response before relaying it.
    resp_headers = [(k, v) for k, v in upstream.headers.items()
                    if k.lower() not in _HOP_BY_HOP]

    content_type = upstream.headers.get("Content-Type", "")
    body = upstream.content
    # Inject portal chrome only into HTML responses; leave JSON/assets untouched.
    if "text/html" in content_type.lower():
        try:
            html = body.decode("utf-8", errors="replace")
            html = _inject_portal_chrome(html, client)
            body = html.encode("utf-8")
        except Exception:
            # If injection fails for any reason, relay the original body unchanged.
            pass

    return Response(body, status=upstream.status_code, headers=resp_headers)


# --- Admin + super-admin consoles --------------------------------------------------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    """Admin console -- add client dashboards to the registry (the seed of the CRM)."""
    if not authed():
        return redirect(url_for("login", next="/admin"))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")

    message = None
    if request.method == "POST":
        key = request.form.get("key", "").strip()
        name = request.form.get("name", "").strip()
        if key:
            # CRM: add_client appends a client record. As the CRM grows, this is where contact
            # details, plan, and onboarding state would be captured alongside the dashboard link.
            store.add_client(key, name or None)
            message = "Added client '%s'." % key
        else:
            message = "Client key is required."

    return render_template(
        "portal.html",
        user=current_user(),
        clients=_visible_clients(),
        is_admin=True,
        is_superadmin=is_superadmin(),
        admin_message=message,
        **_brand_ctx(),
    )


@app.route("/superadmin", methods=["GET", "POST"])
def superadmin():
    """Super-admin console -- set/reveal client portal passwords (operator helpdesk).

    reveal_password returns the RECOVERABLE plaintext kept beside each pbkdf2 hash (see store.py
    for the deliberate trade-off comment) so an operator can read a password back to a client.
    """
    if not authed():
        return redirect(url_for("login", next="/superadmin"))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")

    message = None
    revealed = None
    if request.method == "POST":
        action = request.form.get("action", "")
        key = request.form.get("key", "").strip()
        if action == "set_password" and key:
            new_pw = request.form.get("password", "")
            try:
                store.set_client_password(key, new_pw)
                message = "Password set for '%s'." % key
            except KeyError:
                message = "Unknown client '%s'." % key
        elif action == "reveal" and key:
            revealed = store.reveal_password(key)
            message = ("Password for '%s': %s" % (key, revealed)) if revealed \
                else "No recoverable password stored for '%s'." % key

    return render_template(
        "portal.html",
        user=current_user(),
        clients=_visible_clients(),
        is_admin=True,
        is_superadmin=True,
        superadmin_message=message,
        revealed_password=revealed,
        **_brand_ctx(),
    )


# --- Feedback (text + voice) -------------------------------------------------------------
@app.route("/feedback", methods=["POST"])
def feedback():
    """Store user feedback in the portal's own private bucket; optionally enrich it with AI.

    Accepts either a text `message` field or an uploaded `audio` file (voice note). Enrichment
    (feedback_ai) is a no-op unless configured, so this route never depends on an LLM being wired.
    """
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")

    subject = current_user() or ""
    audio_file = request.files.get("audio")

    try:
        if audio_file is not None:
            audio_bytes = audio_file.read()
            # Optional AI: transcribe the voice note. Gracefully None if unconfigured.
            import feedback_ai
            transcript = feedback_ai.transcribe_voice(audio_bytes,
                                                      content_type=audio_file.mimetype or "audio/webm")
            extra = {"transcript": transcript} if transcript else None
            feedback_store.save_voice_feedback(
                audio_bytes,
                content_type=audio_file.mimetype or "audio/webm",
                subject=subject,
                extra=extra,
            )
        else:
            message = request.form.get("message", "")
            if not message.strip():
                return Response('{"error":"empty"}', status=400, mimetype="application/json")
            # Optional AI: summarise/interpret the text. Gracefully None if unconfigured.
            import feedback_ai
            summary = feedback_ai.summarize_text(message)
            extra = {"summary": summary} if summary else None
            feedback_store.save_text_feedback(message, subject=subject, extra=extra)
    except Exception:
        # Never fail loudly on feedback -- it must not block the user. Report a soft error.
        return Response('{"error":"could_not_store"}', status=500, mimetype="application/json")

    return Response('{"ok":true}', mimetype="application/json")


# --- Agora Atrium: the co-branded client workspace (the next step on the CRM growth path) -------
# Atrium is additive: it reuses the SAME session auth (authed / can_open / is_superadmin) and the
# SAME private bucket as the registry (per-client workspace/<c>.json via workspace.py). No new
# service, bucket, SA, IAM, or domain. Client-facing routes live under /w/<c>/; team management
# extends the operator console under /admin/atrium/.
ATRIUM_TABS = {"overview", "dashboard", "leadgen", "organic", "calendar", "conversations", "settings"}


@app.template_filter("atrium_dt")
def _atrium_dt(iso):
    """Format a stored ISO-8601 Z timestamp as 'Jun 18, 3:40 PM' (cross-platform, no %- codes)."""
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return iso
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return "%s %d, %d:%02d %s" % (dt.strftime("%b"), dt.day, hour12, dt.minute, ampm)


def _atrium_json_gate(client):
    """Shared gate for Atrium POST actions: returns a JSON error Response, or None when allowed."""
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    if not can_open(client):
        return Response('{"error":"forbidden"}', status=403, mimetype="application/json")
    return None


def _bool_field(name):
    """Read a checkbox-style form field as a bool."""
    return request.form.get(name, "0") in ("1", "true", "True", "on")


def _client_sender_name(user):
    """A human-ish name for a client message sender, derived from the login email."""
    if user and "@" in user:
        return user.split("@")[0].split(".")[0].title()
    return "Client"


@app.route("/w/<client>/", defaults={"tab": "overview"}, methods=["GET"])
@app.route("/w/<client>/<tab>", methods=["GET"])
def atrium(client, tab):
    """Render the Atrium workspace shell for `client`, with `tab` active (client-facing)."""
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response("No workspace exists for this client yet.", status=404, mimetype="text/plain")
    if tab not in ATRIUM_TABS:
        tab = "overview"
    user = current_user()
    view = atrium_view.build(ws, client, user, tab)
    return render_template(
        "atrium.html",
        workspace_name=WORKSPACE_NAME,
        ws=ws,
        view=view,
        user=user,
        user_notify=workspace.get_notify(ws, user or ""),
        is_superadmin=is_superadmin(),
        favicon=brand.FAVICON_DATA_URI,
    )


@app.route("/w/<client>/approve", methods=["POST"])
@app.route("/w/<client>/request-changes", methods=["POST"])
def atrium_decide(client):
    """Approve or request changes on a content piece (client-facing); notifies the AGORA team."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    decision = "approved" if request.path.endswith("/approve") else "changes"
    content_id = request.form.get("content_id", "").strip()
    note = request.form.get("note", None)
    try:
        item = workspace.decide_content(client, content_id, decision, note=note)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    notify.client_decided(client, item, decision, current_user())
    return jsonify(ok=True, status=item.get("status"), decided_at=item.get("decided_at", ""))


@app.route("/w/<client>/save-note", methods=["POST"])
def atrium_save_note(client):
    """Persist a client's recommendation note on a content piece (silent)."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    try:
        workspace.set_content_note(client, content_id, request.form.get("note", ""))
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True)


@app.route("/w/<client>/send-message", methods=["POST"])
def atrium_send_message(client):
    """Append a client message to a conversation (client-facing); notifies the AGORA team."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    conv_id = request.form.get("conversation_id", "").strip()
    body = request.form.get("body", "").strip()
    if not body:
        return Response('{"error":"empty"}', status=400, mimetype="application/json")
    try:
        conv, message = workspace.add_message(
            client, conv_id, "client", _client_sender_name(current_user()),
            body, set_status="awaiting_reply",
        )
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    notify.client_messaged(client, conv, current_user())
    return jsonify(ok=True, message=message, status=conv.get("status"))


@app.route("/w/<client>/save-notify", methods=["POST"])
def atrium_save_notify(client):
    """Save the logged-in user's notification preferences for this workspace (silent)."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    user = current_user()
    if not user:
        return Response('{"error":"no_user"}', status=400, mimetype="application/json")
    prefs = {k: _bool_field(k) for k in ("master", "content", "replies", "summary", "status", "news")}
    freq = request.form.get("frequency", "instant")
    if freq in ("instant", "daily"):
        prefs["frequency"] = freq
    try:
        workspace.set_notify(client, user, prefs)
    except KeyError:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    return jsonify(ok=True)


# --- Atrium team management (super-admin operator console) --------------------------------------
def _atrium_admin_redirect(client, msg):
    """Redirect back to a client's Atrium management page with a flash message."""
    return redirect(url_for("admin_atrium_client", client=client, msg=msg))


@app.route("/admin/atrium", methods=["GET"])
def admin_atrium():
    """List the clients whose Atrium workspaces the operator can manage."""
    if not authed():
        return redirect(url_for("login", next="/admin/atrium"))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    clients = []
    for c in store.list_clients():
        clients.append({"key": c.get("key"), "name": c.get("name"),
                        "has_workspace": workspace.workspace_exists(c.get("key"))})
    return render_template("admin_atrium.html", clients=clients, client=None,
                           user=current_user(), workspace_name=WORKSPACE_NAME,
                           **_brand_ctx())


@app.route("/admin/atrium/<client>", methods=["GET"])
def admin_atrium_client(client):
    """Manage one client's Atrium workspace: campaigns, content, conversations, metrics."""
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client)
    return render_template("admin_atrium.html", clients=None, client=client, ws=ws,
                           user=current_user(), workspace_name=WORKSPACE_NAME,
                           msg=request.args.get("msg"), **_brand_ctx())


@app.route("/admin/atrium/<client>/campaign", methods=["POST"])
def admin_atrium_campaign(client):
    """Add a new campaign or edit an existing one's strategy + AI summary."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    campaign_id = request.form.get("campaign_id", "").strip()
    strategy = {
        "what": request.form.get("what", "").strip(),
        "why": request.form.get("why", "").strip(),
        "next": request.form.get("next", "").strip(),
    }
    ai_summary = request.form.get("ai_summary", "").strip()
    if campaign_id:
        try:
            workspace.update_campaign(client, campaign_id, strategy=strategy, ai_summary=ai_summary)
            return _atrium_admin_redirect(client, "Campaign updated.")
        except KeyError:
            return _atrium_admin_redirect(client, "Unknown campaign.")
    channel = "paid" if request.form.get("channel") == "paid" else "organic"
    name = request.form.get("name", "").strip()
    if not name:
        return _atrium_admin_redirect(client, "Campaign name is required.")
    workspace.add_campaign(client, channel, name, request.form.get("eyebrow", "").strip(),
                           strategy=strategy, ai_summary=ai_summary)
    return _atrium_admin_redirect(client, "Campaign added.")


@app.route("/admin/atrium/<client>/content", methods=["POST"])
def admin_atrium_content(client):
    """Add a content piece to a campaign (status -> awaiting) and notify the client per prefs."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    campaign_id = request.form.get("campaign_id", "").strip()
    content = {
        "ref": request.form.get("ref", "").strip(),
        "type_tag": request.form.get("type_tag", "").strip(),
        "sub_tag": request.form.get("sub_tag", "").strip(),
        "platform": request.form.get("platform", "").strip(),
        "caption": request.form.get("caption", "").strip(),
        "thumb_kind": request.form.get("thumb_kind", "").strip(),
    }
    if content["ref"]:
        content["id"] = content["ref"]
    try:
        item = workspace.add_content(client, campaign_id, content)
    except KeyError:
        return _atrium_admin_redirect(client, "Unknown campaign.")
    ws = workspace.load_workspace(client)
    notify.team_added_content(client, ws, item)
    return _atrium_admin_redirect(client, "Content added and the client was notified.")


@app.route("/admin/atrium/<client>/conversation", methods=["POST"])
def admin_atrium_conversation(client):
    """Start a new conversation thread, optionally with an opening AGORA message."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    subject = request.form.get("subject", "").strip()
    if not subject:
        return _atrium_admin_redirect(client, "Subject is required.")
    conv = workspace.add_conversation(client, subject)
    opening = request.form.get("body", "").strip()
    if opening:
        sender_name = request.form.get("sender_name", "").strip() or "Maya"
        workspace.add_message(client, conv["id"], "agora", sender_name, opening)
        ws = workspace.load_workspace(client)
        notify.team_replied(client, ws, workspace._find_conversation(ws, conv["id"]), sender_name)
    return _atrium_admin_redirect(client, "Conversation started.")


@app.route("/admin/atrium/<client>/reply", methods=["POST"])
def admin_atrium_reply(client):
    """Reply to a conversation as the AGORA team; notifies the client per their prefs."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    conv_id = request.form.get("conversation_id", "").strip()
    body = request.form.get("body", "").strip()
    if not body:
        return _atrium_admin_redirect(client, "Message is empty.")
    sender_name = request.form.get("sender_name", "").strip() or "Maya"
    new_status = "resolved" if _bool_field("resolve") else "awaiting_reply"
    try:
        conv, _msg = workspace.add_message(client, conv_id, "agora", sender_name, body,
                                           set_status=new_status)
    except KeyError:
        return _atrium_admin_redirect(client, "Unknown conversation.")
    ws = workspace.load_workspace(client)
    notify.team_replied(client, ws, conv, sender_name)
    return _atrium_admin_redirect(client, "Reply sent and the client was notified.")


@app.route("/admin/atrium/<client>/metrics", methods=["POST"])
def admin_atrium_metrics(client):
    """Update the headline counts (today + split) and the six KPI metric values/trends."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client)
    if ws is None:
        return _atrium_admin_redirect(client, "No workspace to edit.")

    def _int(name, fallback):
        try:
            return int(request.form.get(name, fallback))
        except (TypeError, ValueError):
            return fallback

    today = {
        "leads": _int("today_leads", ws.get("today", {}).get("leads", 0)),
        "visitors": _int("today_visitors", ws.get("today", {}).get("visitors", 0)),
        "bookings": _int("today_bookings", ws.get("today", {}).get("bookings", 0)),
    }
    split = {
        "paid": _int("split_paid", ws.get("split", {}).get("paid", 0)),
        "organic": _int("split_organic", ws.get("split", {}).get("organic", 0)),
    }
    workspace.set_overview_counts(client, today=today, split=split)

    metrics = []
    for i, m in enumerate(ws.get("metrics", [])):
        metrics.append({
            "icon": m.get("icon", "trending"),
            "label": m.get("label", ""),
            "value": request.form.get("metric_value_%d" % i, m.get("value", "")).strip(),
            "trend": request.form.get("metric_trend_%d" % i, m.get("trend", "")).strip(),
            "trend_up": _bool_field("metric_up_%d" % i),
        })
    if metrics:
        workspace.set_metrics(client, metrics)
    return _atrium_admin_redirect(client, "Metrics updated.")


@app.route("/healthz", methods=["GET"])
def healthz():
    return Response("ok", mimetype="text/plain")


# CRM: future routes attach here as the portal grows beyond dashboards --
#   /clients/<key>            -- a client record page (contact, plan, onboarding state)
#   /clients/<key>/notes      -- CRM notes timeline
#   /clients/<key>/tasks      -- CRM tasks / follow-ups
# All would read/write the same private platform.json via store.py (no database).


if __name__ == "__main__":
    # Local dev only; in Cloud Run gunicorn (see Dockerfile) serves main:app.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
