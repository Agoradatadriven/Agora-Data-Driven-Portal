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

import os

import requests
from flask import (
    Flask,
    Response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import platform_sso
import store
import feedback as feedback_store

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
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "/")
    if request.method == "GET":
        if authed():
            return redirect(next_url or "/")
        return render_template("login.html", next=next_url, error=None)

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    granted = store.verify_portal_login(email, password)
    if not granted:
        return render_template("login.html", next=next_url, email=email,
                               error="Incorrect email or password."), 401

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
    pill = (
        '<div id="ag-portal-chrome" style="position:fixed;top:12px;right:12px;z-index:2147483647;'
        'display:flex;gap:8px;align-items:center;font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">'
        '<a href="/" style="padding:6px 12px;border-radius:999px;background:#141b33;color:#eaf0ff;'
        'border:1px solid #26314f;text-decoration:none;font-size:12px;">All dashboards</a>'
        '<a href="/logout" style="padding:6px 12px;border-radius:999px;background:#141b33;'
        'color:#ff5c7a;border:1px solid rgba(255,92,122,0.4);text-decoration:none;font-size:12px;">'
        'Log out</a>'
        '<a href="/" title="Send feedback" style="padding:6px 12px;border-radius:999px;'
        'background:#5b8cff;color:#06122e;text-decoration:none;font-size:12px;font-weight:700;">'
        'Feedback</a>'
        '</div>'
    )
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
