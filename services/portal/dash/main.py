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
import json
import os
import re
import secrets

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

import atrium_docs
import atrium_view
import audit
import brand
import google_oauth
import intel_ai
import intel_refresh
import notify
import platform_sso
import store
import sync_dash
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

# The admin "Apps" launcher deep-links (task 3): opening the portal as an admin unlocks Atrium admin,
# Skill Mastery, and the Website editor. These are env-overridable so a deploy can point at the real
# hosts; the defaults are the current production URLs. Skill Mastery moves behind a
# *.agoradatadriven.com custom domain (so the shared SSO cookie reaches it) in its own phase; until
# then this is the run.app URL.
SKILL_MASTERY_URL = os.environ.get(
    "SKILL_MASTERY_URL", "https://mastery-engine-c732u7m57a-uc.a.run.app")
WEBSITE_EDITOR_URL = os.environ.get("WEBSITE_EDITOR_URL", "https://agoradatadriven.com/?edit=1")
SENTINEL_URL = os.environ.get(
    "SENTINEL_URL", "https://sentinel-585951669065.asia-southeast1.run.app/login")

app = Flask(__name__)
app.secret_key = SESSION_SECRET

# Cookie hardening. SameSite=None + Secure is REQUIRED for the cross-subdomain portal flow
# (portal.agoradatadriven.com -> <c>.agoradatadriven.com): a Lax/Strict cookie would be dropped
# on the cross-site navigation. HttpOnly keeps the session out of JS.
# In production these MUST stay on; locally they are relaxed (PORTAL_SECURE_COOKIES=0) because a
# browser drops a Secure cookie over plain http://localhost, which would make login loop. The
# default is the secure production posture, so a normal deploy is unaffected.
_secure_cookies = os.environ.get("PORTAL_SECURE_COOKIES", "1") != "0"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=_secure_cookies,
    SESSION_COOKIE_SAMESITE="None" if _secure_cookies else "Lax",
)

# Local preview ("no password") mode. When PORTAL_DEV_NOAUTH=1 every request is auto-signed-in as a
# super-admin, so a developer can double-click a launcher and click straight through the whole portal
# -- all clients, all Atrium workspaces, and the in-place admin edit affordances -- with no login.
# It is DELIBERATELY tied to `not _secure_cookies` (i.e. the local http posture, PORTAL_SECURE_COOKIES=0):
# production always serves over https with secure cookies ON, so even if this env var ever leaked into
# a deploy it would stay inert. Only the local launcher (run_local.ps1) ever sets it.
DEV_NOAUTH = os.environ.get("PORTAL_DEV_NOAUTH", "") == "1" and not _secure_cookies
# Deliberate LIVE-demo "no password" mode. Unlike DEV_NOAUTH (interlocked to the local http posture
# so it stays inert in prod), DEMO_NOAUTH works over https with secure cookies ON -- an operator can
# flip it on for a presentation and every request is auto-signed-in as a super-admin. OFF by default;
# only a deploy that sets PORTAL_DEMO_NOAUTH=1 turns it on. To demo the client-facing view, log in with
# a client password (role is fixed at login -- there is no in-session "view as client" toggle).
DEMO_NOAUTH = os.environ.get("PORTAL_DEMO_NOAUTH", "") == "1"
AUTO_LOGIN = DEV_NOAUTH or DEMO_NOAUTH
# Cap request bodies. On Cloud Run the platform caps requests at ~32 MiB, so live large videos use the
# signed-URL direct-to-GCS path. In LOCAL dev (local-fs backend, no GCS to sign for) there is no such
# cap and no signing, so allow large in-app uploads (~1 GB) so the same "Upload .mp4" button works
# end-to-end off-cloud. _LOCAL_BACKEND is true exactly when the workspace runs on the local-fs backend.
_LOCAL_BACKEND = bool(os.environ.get("WORKSPACE_LOCAL_DIR"))
app.config["MAX_CONTENT_LENGTH"] = (1100 * 1024 * 1024) if _LOCAL_BACKEND else (32 * 1024 * 1024)

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


def real_superadmin():
    """True iff this session actually holds the super-admin ("*") grant."""
    return "*" in allowed_clients()


def is_superadmin():
    """Super-admin: the session holds the "*" grant. Role is fixed at LOGIN, not toggleable.

    There is deliberately no "view as client" override: who you are (admin vs client) is decided by
    which password you logged in with (store.verify_portal_login -> ["*"] for admin, else the client
    keys). Removing the in-session toggle keeps the admin/client boundary a hard, login-derived line.
    """
    return real_superadmin()


# THE super admin: the one account allowed to create/manage OTHER admin accounts. Everyone with "*"
# is an "admin" (full client access); only the super admin (info@, or an account whose role is
# "superadmin") sits above them. Env-overridable so a different deploy can nominate its own owner.
SUPER_ADMIN_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL", "info@agoradatadriven.com").strip().lower()


def current_account():
    """The registry account dict for the logged-in user, or None (env/bootstrap logins have none)."""
    user = current_user()
    return store.get_account(user) if user else None


def _impersonator():
    """THE super admin's real email while they're 'acting as' another user, else None.

    Set by /admin/impersonate and cleared by /admin/stop-impersonating. Its presence is what lets a
    templated banner offer 'Stop acting as' from anywhere the impersonated session lands."""
    return session.get("impersonator")


def _establish_session(email, granted):
    """Establish (or replace) the portal session for `email` with the `granted` client keys."""
    session["ok"] = True
    session["user"] = email
    session["clients"] = granted


def _safe_next(next_url):
    """Return next_url iff it's safe to redirect to (anti open-redirect): a same-origin relative path,
    or an http(s) URL within *.agoradatadriven.com. Otherwise None (caller falls back)."""
    if not next_url:
        return None
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url  # same-origin relative path
    try:
        from urllib.parse import urlparse
        u = urlparse(next_url)
        host = (u.hostname or "").lower()
        if u.scheme in ("http", "https") and (host == "agoradatadriven.com" or host.endswith(".agoradatadriven.com")):
            return next_url
    except Exception:
        pass
    return None


def _mint_sso_on(resp, granted, subject):
    """Attach the shared .agoradatadriven.com SSO cookie to `resp` (so dashboards + the website editor
    trust this login additively). No-op when SSO_SECRET is unset, so a missing key never breaks login."""
    if SSO_SECRET:
        cookie_value = platform_sso.mint_sso_cookie(SSO_SECRET, granted, subject=subject)
        resp.set_cookie(
            platform_sso.COOKIE_NAME, cookie_value, domain=COOKIE_DOMAIN,
            secure=True, httponly=True, samesite="None",
            max_age=platform_sso.DEFAULT_TTL_SECONDS,
        )
    return resp


def is_root_admin():
    """True iff this session is THE super admin -- may manage admin accounts.

    That is the configured SUPER_ADMIN_EMAIL (info@) or any account whose role is "superadmin". Must
    also hold full admin access ("*"), so a half-configured account can never escalate.
    """
    if not is_superadmin():
        return False
    if (current_user() or "").strip().lower() == SUPER_ADMIN_EMAIL:
        return True
    acct = current_account()
    return bool(acct and acct.get("role") == "superadmin")


def _resolve_login_email(email):
    """Client keys a VERIFIED email may open, or None if there's no active account for it.

    Used by the Google sign-in callback and by 'stop impersonating' to re-derive a real identity's
    grant. THE super admin (SUPER_ADMIN_EMAIL) always resolves to "*"; everyone else is looked up in
    the accounts registry (active only). None means "no account yet" -> route to request-access."""
    return store.resolve_google_login(email, super_admin_email=SUPER_ADMIN_EMAIL)


def _actor_role():
    """('email', 'superadmin'|'admin'|'client') for the current session -- who is acting, for audit."""
    role = "superadmin" if is_root_admin() else ("admin" if is_superadmin() else "client")
    return (current_user() or "system"), role


def _audit(client, action, detail=""):
    """Record one super-admin activity entry (who/what/which client) + optional team email alert.

    Best-effort: never raises into the request path. Powers the super-admin Activity tab so every
    admin/client action across all workspaces is visible there."""
    actor, role = _actor_role()
    audit.log_activity(client, actor, role, action, detail)
    notify.activity_alert(actor, role, client, action, detail)


def _trash(client, kind, label, payload, extra=None):
    """Soft-delete a removed item into the Trash so the super admin can restore it. Best-effort."""
    actor, role = _actor_role()
    audit.trash_put(client, kind, label, payload, actor=actor, role=role, extra=extra)


def _gen_password():
    """A readable, strong portal password (mirrors onboard_client._generate_password)."""
    return secrets.token_urlsafe(9)


@app.before_request
def _dev_auto_login():
    """In local preview mode (DEV_NOAUTH), establish a super-admin session for every request.

    This is what turns the double-click launcher into a "no password" portal: the session looks
    exactly like a real super-admin login (`clients == ["*"]`), so all the normal auth checks
    (authed / can_open / is_superadmin) pass and the in-place admin edit affordances render. It is a
    no-op unless DEV_NOAUTH is on, which only happens locally (see the DEV_NOAUTH definition above).
    """
    if AUTO_LOGIN and not session.get("ok"):
        session["ok"] = True
        # Sign in as THE super admin so the no-password preview matches the seeded operator identity
        # (its Profile + admin-account management all resolve to info@agoradatadriven.com).
        session["user"] = SUPER_ADMIN_EMAIL
        session["clients"] = ["*"]


# --- Central <head> injection: brand web font (always) + GTM container (opt-in) ------------------
# Both are injected once here (after_request) instead of in all the self-contained templates -- so
# they never touch the esprima JS gate or the "no Jinja in <script>" rule, and apply to EVERY portal
# HTML page. Reverse-proxied client dashboards (/d/<c>/) are skipped.
# Brand font: Roboto (per assets/brand.json) web-loaded so the "Roboto"-first template stacks render
# as Roboto (not a system font). Always on -- it's brand identity, not analytics.
_BRAND_FONT_HEAD = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700;900&display=swap" rel="stylesheet">'
)
# GTM: set GTM_CONTAINER_ID=GTM-XXXXXXX to load the container; unset (default) = no tag. GA4 is
# configured INSIDE the container in the GTM UI. The snippet is Google's standard install, verbatim.
_GTM_HEAD = (
    "<!-- Google Tag Manager -->"
    "<script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':"
    "new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],"
    "j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src="
    "'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);"
    "})(window,document,'script','dataLayer','__GTM_ID__');</script>"
    "<!-- End Google Tag Manager -->"
)
_GTM_BODY = (
    "<!-- Google Tag Manager (noscript) -->"
    "<noscript><iframe src=\"https://www.googletagmanager.com/ns.html?id=__GTM_ID__\""
    " height=\"0\" width=\"0\" style=\"display:none;visibility:hidden\"></iframe></noscript>"
    "<!-- End Google Tag Manager (noscript) -->"
)


@app.after_request
def _inject_head(resp):
    """Inject the brand web font (always) + GTM (when GTM_CONTAINER_ID is set) into every HTML page.

    Head additions go right after <head>; the GTM <noscript> goes right after <body>. No-op on non-
    HTML, streamed, and reverse-proxied (/d/) responses, and idempotent (never injects twice)."""
    if resp.direct_passthrough:
        return resp
    if "text/html" not in (resp.content_type or "").lower():
        return resp
    if request.path.startswith("/d/"):        # reverse-proxied client dashboards -- not our page
        return resp
    try:
        html = resp.get_data(as_text=True)
    except (RuntimeError, UnicodeDecodeError):
        return resp
    if not html:
        return resp

    gtm_id = os.environ.get("GTM_CONTAINER_ID", "").strip()
    gtm_present = bool(gtm_id) and (gtm_id in html)

    head_parts = []
    if "css2?family=Roboto" not in html:      # don't double up where a template @imports it
        head_parts.append(_BRAND_FONT_HEAD)
    if gtm_id and not gtm_present:
        head_parts.append(_GTM_HEAD.replace("__GTM_ID__", gtm_id))
    if head_parts:
        joined = "".join(head_parts)
        html = re.sub(r"<head[^>]*>", lambda m: m.group(0) + joined, html, count=1, flags=re.IGNORECASE)
    if gtm_id and not gtm_present:
        body = _GTM_BODY.replace("__GTM_ID__", gtm_id)
        html = re.sub(r"<body[^>]*>", lambda m: m.group(0) + body, html, count=1, flags=re.IGNORECASE)

    # Impersonation banner: while THE super admin is "acting as" another user, every page gets a
    # fixed escape-hatch bar (injected here so it reaches even the huge atrium.html without touching
    # it). It's pure HTML -- a GET link to /admin/stop-impersonating -- so no JS gate is involved.
    imp = _impersonator()
    if imp:
        from markupsafe import escape as _esc  # bundled with Flask/Jinja
        acting = str(_esc(current_user() or ""))
        real = str(_esc(imp))
        banner = (
            '<div style="position:fixed;left:0;right:0;bottom:0;z-index:2147483646;display:flex;'
            'gap:12px;align-items:center;justify-content:center;flex-wrap:wrap;background:#5A54DD;'
            "color:#fff;padding:9px 14px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
            'Roboto,Arial,sans-serif;font-size:13px;box-shadow:0 -4px 14px rgba(16,24,40,.18);">'
            '<span>Acting as <b>%s</b> &middot; signed in as <b>%s</b></span>'
            '<a href="/admin/stop-impersonating" style="background:#fff;color:#5A54DD;'
            'text-decoration:none;font-weight:800;border-radius:999px;padding:5px 13px;'
            'font-size:12px;">Stop acting as</a></div>'
        ) % (acting, real)
        html = re.sub(r"<body[^>]*>", lambda m: m.group(0) + banner, html, count=1, flags=re.IGNORECASE)

    resp.set_data(html)
    return resp


def _visible_clients():
    """The client dicts this session is allowed to see, for the portal landing page."""
    clients = store.list_clients()
    if real_superadmin():
        return clients
    allowed = set(allowed_clients())
    return [c for c in clients if c.get("key") in allowed]


def _brand_ctx():
    """Shared brand assets for every rendered page (the AGORA mark + favicon, from brand.py).

    The deployed container only bundles dash/, so the mark lives in brand.py rather than being read
    from assets/ at runtime; this keeps the portal/login chrome in step with the Atrium sidebar.
    """
    return {"agora_logo": brand.AGORA_LOGO_LIGHT, "favicon": brand.FAVICON_DATA_URI,
            "impersonating": _impersonator(), "google_enabled": google_oauth.is_configured()}


def _post_login_destination(granted, next_url):
    """Where to send a user immediately after a successful login.

    A client who can open exactly ONE workspace lands straight on that workspace's overview -- the
    company overview -- instead of the portal card list, so a single-client login feels like "their"
    app. Super-admins ("*") and users with several clients keep the card list, and an explicit deep
    link (any next_url other than the bare "/") always wins so /admin, a specific /w/<c>/ tab, or a
    proxied dashboard link is never hijacked.
    """
    if next_url and next_url not in ("/", ""):
        return next_url
    if "*" not in granted and len(granted) == 1:
        only = granted[0]
        if workspace.workspace_exists(only):
            return url_for("atrium", client=only, tab="dashboard")
    return next_url or "/"


# --- Routes: auth + landing --------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    if not authed():
        return redirect(url_for("login", next="/"))
    # The team console IS the landing page for the agency operator -- a super-admin never sees the
    # old client portal page; they go straight to /admin/atrium. Clients still land on their portal.
    if is_superadmin():
        return redirect(url_for("admin_atrium"))
    return render_template(
        "portal.html",
        user=current_user(),
        clients=_visible_clients(),
        is_admin=authed(),
        is_superadmin=is_superadmin(),
        **_brand_ctx(),
    )


@app.route("/dashboard/<client>")
def client_dashboard(client):
    """Standalone full-screen Looker Studio dashboard for ONE client, reachable from the portal
    'Open dashboard' button -- it shows their live report without entering the full Atrium workspace.
    Gated to whoever may open the client (super-admin opens any; a client opens only their own). The
    URL + height come from the workspace settings (atrium_view.dashboard)."""
    if not authed():
        return redirect(url_for("login", next="/dashboard/%s" % client))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client) or {}
    name = ws.get("display_name") or (store.get_client(client) or {}).get("name") or client
    return render_template("dashboard_view.html", client=client, name=name,
                           dash=atrium_view.dashboard(ws, client),
                           user=current_user(), **_brand_ctx())


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

    # Establish the portal session + mint the shared SSO cookie so the dashboards and the website
    # editor trust this login additively (a missing SSO_SECRET just makes the cookie inert).
    _establish_session(email, granted)
    resp = redirect(_post_login_destination(granted, next_url))
    return _mint_sso_on(resp, granted, email)


# --- Google Sign-In (central: the portal is the ONLY app that runs the OAuth flow) ----------------
@app.route("/auth/google/login", methods=["GET"])
def google_login():
    """Kick off the Google OAuth flow: stash a CSRF state + the post-login destination, then redirect
    to Google's consent screen. If Google sign-in isn't configured, fall back to the password login."""
    if not google_oauth.is_configured():
        return redirect(url_for("login"))
    next_url = request.args.get("next", "/")
    state = google_oauth.new_state()
    session["oauth_state"] = state
    session["oauth_next"] = next_url or "/"
    return redirect(google_oauth.auth_url(state, google_oauth.redirect_uri()))


@app.route("/auth/google/callback", methods=["GET"])
def google_callback():
    """Handle Google's redirect: verify state, exchange the code for a verified email, then log in.

    An email with an ACTIVE account (or THE super admin) is signed in exactly like a password login.
    An unknown/pending email is routed to the request-access page so they can ask an admin for access.
    """
    if not google_oauth.is_configured():
        return redirect(url_for("login"))
    if request.args.get("error"):
        return render_template("login.html", next="/",
                               error="Google sign-in was cancelled.", **_brand_ctx()), 401
    state = request.args.get("state", "")
    if not state or state != session.pop("oauth_state", None):
        return render_template("login.html", next="/",
                               error="Your sign-in link expired. Please try again.", **_brand_ctx()), 400
    email, oerr = google_oauth.exchange_code(request.args.get("code", ""),
                                             google_oauth.redirect_uri())
    if not email:
        return render_template("login.html", next="/",
                               error="Could not complete Google sign-in. Please try again.",
                               **_brand_ctx()), 400
    next_url = session.pop("oauth_next", "/") or "/"
    granted = _resolve_login_email(email)
    if not granted:
        # No active account yet -> let them file a request an admin approves in the console.
        pending = store.get_account(email)
        return render_template("request_access.html", email=email, next=next_url, sent=False,
                               pending=(pending is not None and pending.get("status") == "pending"),
                               **_brand_ctx())
    _establish_session(email, granted)
    resp = redirect(_post_login_destination(granted, next_url))
    return _mint_sso_on(resp, granted, email)


@app.route("/auth/request-access", methods=["POST"])
def request_access():
    """File a passwordless access request (from the Google-sign-in dead-end). Lands in the console's
    Access requests tab for an admin to approve + assign to a client or a role."""
    email = (request.form.get("email", "") or "").strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    if "@" not in email or "." not in domain:
        return render_template("request_access.html", email=email, sent=False,
                               error="Please enter a valid email address.", **_brand_ctx()), 400
    existing = store.get_account(email)
    if existing and existing.get("status") == "active":
        # Already has access -- don't downgrade them; just tell them to sign in.
        return render_template("request_access.html", email=email, sent=True, already=True, **_brand_ctx())
    name = (request.form.get("name", "") or "").strip() or email.split("@")[0]
    message = (request.form.get("message", "") or "").strip()
    store.upsert_google_account(email, name=name, role="client", clients=[], status="pending",
                                message=message, requested_name=name)
    try:
        notify.signup_requested(name, email)
    except Exception:
        pass
    return render_template("request_access.html", email=email, sent=True, **_brand_ctx())


# Stale /auth/* links (old marketing-site buttons) fall through to the login page instead of a
# 404. The REAL Google OAuth routes above (/auth/google/{login,callback}) are exact rules, so
# they always win over this catch-all; it only picks up paths nothing else serves.
@app.route("/auth/<path:rest>", methods=["GET"])
def auth_compat(rest=None):
    return redirect(url_for("login", next=request.args.get("next", "/")))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Self-service client sign-up -> creates a PENDING account an admin then approves.

    A visitor enters their company name, email, and a password; we record a `pending` client account
    in the registry (no client/workspace is created yet). They cannot log in until an admin approves
    the request from the team console (which creates the client + a blank workspace and activates the
    account). This keeps sign-up self-service for clients while the admin stays in control of access.
    """
    if request.method == "GET":
        return render_template("signup.html", error=None, sent=False, **_brand_ctx())

    company = request.form.get("company", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    # Friendly, specific validation (the design guidance: clear messages, email not username, no
    # confirm field -- the form offers a show-password toggle instead).
    domain = email.split("@")[-1] if "@" in email else ""
    if not company:
        error = "Please enter your company name."
    elif "@" not in email or "." not in domain:
        error = "Please enter a valid email address."
    elif len(password) < 6:
        error = "Please choose a password of at least 6 characters."
    elif store.get_account(email) is not None:
        error = "An account or pending request already exists for that email."
    else:
        error = None
    if error:
        return render_template("signup.html", error=error, sent=False,
                               company=company, email=email, **_brand_ctx()), 400

    store.add_account(email, password, name=company, role="client", clients=[],
                      status="pending", requested_name=company)
    # Let the team know a request is waiting (graceful: notify falls back to a stdout log if email
    # isn't configured, and never raises in a way that would fail the sign-up).
    try:
        notify.signup_requested(company, email)
    except Exception:
        pass
    return render_template("signup.html", error=None, sent=True,
                           company=company, email=email, **_brand_ctx())


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    # Honor ?next= so logging out from the marketing site (or a dashboard) returns there instead of
    # the portal login page. Guarded to same-origin paths + *.agoradatadriven.com to avoid an open
    # redirect; anything else falls back to the login page.
    resp = redirect(_safe_next(request.values.get("next", "")) or url_for("login"))
    # Clear the shared SSO cookie too (expire it on the same domain it was set).
    resp.set_cookie(
        platform_sso.COOKIE_NAME, "", domain=COOKIE_DOMAIN,
        secure=True, httponly=True, samesite="None", expires=0,
    )
    return resp


# --- Profile (any logged-in user: photo, display name, title, password) --------------------------
PROFILE_PHOTO_MAX_BYTES = 512 * 1024
_PROFILE_IMAGE_EXT = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _role_label(account):
    """Human label for an account's role (or 'Member' for env/bootstrap logins with no account)."""
    role = (account or {}).get("role")
    if role == "superadmin":
        return "Super administrator"
    if role == "admin":
        return "Administrator"
    if role == "client":
        return "Client"
    return "Administrator" if is_superadmin() else "Member"


@app.route("/profile", methods=["GET", "POST"])
def profile():
    """Self-service profile for ANY logged-in user (super admin, admin, client): upload a photo, set a
    display name + title, and change the password. The photo is stored inline on the account (a small
    data-URI, like client logos -- private registry JSON, never public)."""
    if not authed():
        return redirect(url_for("login", next="/profile"))
    email = current_user()
    account = current_account()

    if request.method == "GET":
        return render_template("profile.html", account=account, email=email,
                               role_label=_role_label(account), is_superadmin=is_superadmin(),
                               msg=request.args.get("msg"), flash_err=(request.args.get("err") == "1"),
                               **_brand_ctx())

    # POST -- update. Env/bootstrap logins have no stored account to edit.
    if account is None:
        return redirect(url_for("profile",
                                msg="This session is signed in via an environment secret, so there's "
                                    "no stored profile to edit.", err=1))
    new_pw = request.form.get("password", "")
    if new_pw:
        if len(new_pw) < 6:
            return redirect(url_for("profile", msg="New password must be at least 6 characters.", err=1))
        store.set_account_password(email, new_pw)

    photo_data = None
    if request.form.get("remove_photo") == "1":
        photo_data = ""          # clear the photo
    else:
        upload = request.files.get("photo")
        if upload is not None and upload.filename:
            mime = (upload.mimetype or "").lower()
            if mime not in _PROFILE_IMAGE_EXT:
                return redirect(url_for("profile", msg="Photo must be a PNG, JPG, GIF, or WEBP image.", err=1))
            data = upload.read(PROFILE_PHOTO_MAX_BYTES + 1)
            if len(data) > PROFILE_PHOTO_MAX_BYTES:
                return redirect(url_for("profile", msg="Photo is too large -- use an image under 512 KB.", err=1))
            import base64  # lazy: only the photo path needs it
            photo_data = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))

    store.set_account_profile(email, name=(request.form.get("name", "").strip() or None),
                              title=request.form.get("title", "").strip(), photo=photo_data)
    return redirect(url_for("profile", msg="Profile updated."))


@app.route("/r", methods=["GET"])
def shared_recap():
    """Public, capability-URL recap page for the Overview "Share recap" button.

    DELIBERATELY UNAUTHENTICATED so a client can forward the link to a colleague who has no login.
    This does NOT weaken the "never make the data JSON public" rule: the recap data rides ENTIRELY in
    the URL #fragment (ROAS / leads / revenue / wins, base64), which browsers never send to the
    server -- so this route reads no client data, touches no bucket, and stores nothing. It serves a
    static branded shell that decodes the fragment and renders it client-side. The only thing exposed
    is what the client themselves chose to put in the link.

    TODO(recap-pdf): add a PDF export of this recap -- e.g. a "Download PDF" button here that renders
    the same recap to PDF (server-side via a headless renderer, or client-side). Copy-link ships now.
    """
    return render_template("recap.html", workspace_name=WORKSPACE_NAME, **_brand_ctx())


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
    # dashboard background (mirrors assets/brand.json -- green CTA, charcoal text).
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
        'background:#4FA84A;color:#fff;text-decoration:none;font-size:12px;font-weight:700;'
        'box-shadow:0 4px 14px rgba(79,168,74,.34);">Feedback</a>'
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
# The old `/admin` and `/superadmin` pages (a client card list with inline admin controls, rendered
# from portal.html) are RETIRED: the operator console at /admin/atrium is now the single admin
# surface (client add/delete, account create/reset/reveal, activity, trash). Both paths redirect
# there so any stale bookmark or link still lands somewhere useful instead of the old page.
@app.route("/admin", methods=["GET", "POST"])
@app.route("/superadmin", methods=["GET", "POST"])
def admin_legacy_redirect():
    if not authed():
        return redirect(url_for("login", next="/admin/atrium"))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    return redirect(url_for("admin_atrium"))


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
ATRIUM_TABS = {"overview", "dashboard", "leadgen", "organic", "calendar", "conversations",
               "intel", "progress", "settings"}
# Team-only tabs: rendered ONLY for admins/super-admins (is_superadmin), never shown to clients. The
# Website Health tab monitors the client's live site + the marketing tags installed on it; the
# Watcher tab archives every video transcript from watched YouTube channels; the Assistant tab is
# retrieval-augmented chat over EVERY workspace source (watcher, intel, campaigns, metrics, ...);
# the Mail tab archives + AI-summarizes the client's email correspondence (mailroom.py).
# NOTE: "mail" is no longer its own tab -- the client's email archive is folded into the unified
# Communications tab (2026-07-15). The /w/<c>/admin/mail + /w/<c>/mail/thread routes stay (invoked
# from within Communications); an old /w/<c>/mail link is redirected to conversations below.
ATRIUM_TEAM_TABS = {"website-health", "watcher", "assistant"}


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


@app.template_filter("atrium_date")
def _atrium_date(iso):
    """Format a stored ISO-8601 date/timestamp as a date only: 'Jun 7, 2026' (no time)."""
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return iso
    return "%s %d, %d" % (dt.strftime("%b"), dt.day, dt.year)


def _atrium_json_gate(client):
    """Shared gate for Atrium POST actions: returns a JSON error Response, or None when allowed."""
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    if not can_open(client):
        return Response('{"error":"forbidden"}', status=403, mimetype="application/json")
    return None


def _atrium_admin_json_gate(client):
    """Gate for in-workspace ADMIN edit actions: super-admin only. JSON error Response, or None."""
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    if not is_superadmin():
        return Response('{"error":"forbidden"}', status=403, mimetype="application/json")
    return None


def _atrium_root_json_gate(client):
    """Gate for Website-Health EDITS: THE super admin only (admins are view-only). JSON error or None.

    Website Health is visible to every admin (is_superadmin) but only THE super admin (is_root_admin)
    may change the monitored URL, run a check, or edit notes -- so an admin "just sees it".
    """
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    if not is_root_admin():
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


def _admin_sender_name(user):
    """The attribution shown on an admin's inline comments/replies (defaults to 'AGORA').

    A shared mailbox like info@ is generic, so we fall back to the team name 'AGORA' rather than a
    personal first name; a personal login (e.g. maya@...) gets their first name.
    """
    if user and "@" in user:
        local = user.split("@")[0]
        if local.lower() not in atrium_view._GENERIC_MAILBOXES:
            return local.split(".")[0].split("_")[0].title()
    return "AGORA"


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
    if tab == "mail":
        # Mail was folded into Communications (2026-07-15) -- keep old /w/<c>/mail links working.
        tab = "conversations"
    if tab not in ATRIUM_TABS and tab not in ATRIUM_TEAM_TABS:
        tab = "overview"
    if tab in ATRIUM_TEAM_TABS and not is_superadmin():
        # Team-only tabs (Website Health) are never shown to clients -- bounce to Dashboard.
        tab = "dashboard"
    if tab == "overview":
        # Overview is hidden in the nav for the demo; land on Dashboard instead so the bare
        # workspace URL never opens on the hidden tab. (Revert with the nav list to restore it.)
        tab = "dashboard"
    user = current_user()
    view = atrium_view.build(ws, client, user, tab)
    # Admin-only "Preview as client": a real admin can append ?preview=client to SEE the exact
    # client-facing view (admin edit affordances hidden) WITHOUT changing their session/role. This is
    # not the old in-header session toggle (which could flip roles) -- it's a per-request preview the
    # client themselves never see, so the login-derived role boundary stays intact.
    admin_preview = is_superadmin() and request.args.get("preview") == "client"
    is_admin_view = is_superadmin() and not admin_preview
    # Mail (folded into Communications): the client's email archive index + digest. Team-only, so it
    # is only built for admins; it feeds BOTH the Communications team panel and the timeline's
    # non-client email cards. Bodies stay in per-thread objects, fetched on click (atrium_mail_thread).
    mailview = _mail_view(ws, client) if is_admin_view else None
    return render_template(
        "atrium.html",
        workspace_name=WORKSPACE_NAME,
        ws=ws,
        view=view,
        user=user,
        user_notify=workspace.get_notify(ws, user or ""),
        is_superadmin=is_admin_view,
        # Website Health is editable by THE super admin only; admins see it read-only.
        can_edit_health=(is_root_admin() and not admin_preview),
        # Watcher (team-only tab): per-channel video cards with transcript PREVIEWS only -- the
        # full transcripts stay in each channel's own archive object, fetched on click.
        watcher=(_watcher_view(ws, client) if is_admin_view else []),
        mail=mailview,
        # Communications: ONE unified, date-sorted timeline of every conversation (email/Upwork/
        # Slack/meeting/call). Clients get ONLY audience=="client" cards (team cards are filtered out
        # here, server-side); admins also get non-client email threads as team-only cards.
        communications=_communications_view(ws, client, is_admin_view, mailview),
        # Assistant (team-only): the model dropdown options + the saved choices ("" = automatic
        # model; depth quick|standard|deep) + the all-time spend tally that seeds the cost pill.
        assistant_models=intel_ai.available_models(),
        assistant_model=((ws.get("assistant") or {}).get("model", "")),
        assistant_depth=_assistant_depth(ws),
        assistant_usage=workspace.assistant_usage(ws),
        admin_preview=admin_preview,
        admin_name=_admin_sender_name(user),
        profile_photo=(current_account() or {}).get("photo", ""),
        # Progress tab: the client-safe task columns (server-side filtered -- internal fields and
        # client_facing:false tasks never reach this render; see _progress_tasks).
        progress_cols=_progress_tasks(ws),
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
    _audit(client, "approved content" if decision == "approved" else "requested changes",
           item.get("ref") or content_id)
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
    _audit(client, "sent a message", conv.get("subject") or "")
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
    prefs = {k: _bool_field(k) for k in
             ("master", "content", "changes", "replies", "summary", "status", "news")}
    freq = request.form.get("frequency", "instant")
    if freq in ("instant", "daily"):
        prefs["frequency"] = freq
    try:
        workspace.set_notify(client, user, prefs)
    except KeyError:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    return jsonify(ok=True)


@app.route("/w/<client>/logo", methods=["POST"])
def atrium_client_logo(client):
    """Let a workspace member set the client's own logo from INSIDE the workspace (no console needed).

    Client-facing twin of the team console's /admin/atrium/<c>/logo: any user who can open this
    workspace may replace its logo by clicking the side-panel crest. Stored INLINE in the workspace
    brand (brand.client_logo) as a self-contained <img> data: URI -- exactly how seeded logos are
    embedded, so it needs no new infra or separate object. Returns the new markup as JSON so the side
    panel can swap it in place (no reload)."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    upload = request.files.get("logo")
    if upload is None or not upload.filename:
        return Response('{"ok":false,"error":"no_file"}', status=400, mimetype="application/json")
    mime = (upload.mimetype or "").lower()
    if mime not in _ATRIUM_IMAGE_EXT:
        return Response('{"ok":false,"error":"bad_type"}', status=400, mimetype="application/json")
    data = upload.read(LOGO_MAX_BYTES + 1)
    if len(data) > LOGO_MAX_BYTES:
        return Response('{"ok":false,"error":"too_large"}', status=400, mimetype="application/json")
    # Embed as a data: URI inside an <img> (NOT inlined SVG markup): an <img> never executes script,
    # so an uploaded SVG cannot inject anything when the markup is later rendered with |safe.
    import base64  # lazy: only the logo path needs it
    b64 = base64.b64encode(data).decode("ascii")
    logo_markup = '<img src="data:%s;base64,%s" alt="logo">' % (mime, b64)
    try:
        workspace.set_client_logo(client, logo_markup)
    except KeyError:
        return Response('{"ok":false,"error":"no_workspace"}', status=404, mimetype="application/json")
    _audit(client, "changed logo")
    return jsonify(ok=True, logo=logo_markup)


@app.route("/w/<client>/comment", methods=["POST"])
def atrium_comment(client):
    """Append a client comment to a content piece (client-facing); notifies the AGORA team.

    A comment is either a plain note (kind="comment") or a "Request changes" comment (kind="changes"),
    which ALSO flips the piece's status to 'changes' — the request-changes decision now lives in the
    comment thread (rendered as a flagged light-red bubble) instead of a separate action button.
    """
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    body = request.form.get("body", "").strip()
    if not body:
        return Response('{"error":"empty"}', status=400, mimetype="application/json")
    kind = "changes" if request.form.get("kind", "").strip() == "changes" else "comment"
    try:
        item, comment = workspace.add_content_comment(
            client, content_id, "client", _client_sender_name(current_user()), body,
            kind=kind, set_status=("changes" if kind == "changes" else None),
        )
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    if kind == "changes":
        notify.client_decided(client, item, "changes", current_user())
    else:
        notify.client_commented(client, item, body, current_user())
    _audit(client, "requested changes" if kind == "changes" else "commented",
           item.get("ref") or content_id)
    return jsonify(ok=True, comment=comment, status=item.get("status"))


def _progress_tasks(ws):
    """The CLIENT-SAFE shape of a workspace's tasks for the Progress tab, grouped by stage.

    This is the server-side filter the spec demands (§6/§7): only client_facing tasks are included,
    and only their client-safe fields are shaped for the template -- lead/support/sub-task owners,
    priority, internal_notes, and the account manager NEVER reach the client's HTML."""
    today = datetime.date.today().isoformat()
    soon = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
    cols = [{"key": k, "name": lbl, "tasks": []} for k, lbl in TASK_CLIENT_STAGES]
    by_key = {c["key"]: c for c in cols}
    for t in (ws or {}).get("tasks") or []:
        if not t.get("client_facing"):
            continue
        t = workspace.normalize_task(dict(t))
        stage = t.get("stage") or "in_process"
        # The two-level breakdown, stripped to client-safe "phases": a name + its steps.
        # Owners (main-task AND sub-task assignees) never reach this shape.
        phases = []
        for m in t.get("maintasks") or []:
            steps = [{"text": s.get("text", ""), "done": bool(s.get("done"))}
                     for s in m.get("subs") or []]
            pdone = len([s for s in steps if s["done"]])
            phases.append({"name": m.get("text", ""), "steps": steps,
                           "done": pdone, "total": len(steps),
                           "all_done": bool(steps) and pdone == len(steps)})
        subs = [s for ph in phases for s in ph["steps"]]
        done = len([s for s in subs if s["done"]])
        comments = [{"id": c.get("id", ""), "sender": c.get("sender", ""),
                     "sender_name": c.get("sender_name", ""), "body": c.get("body", ""),
                     "kind": c.get("kind", "comment"), "resolved": bool(c.get("resolved")),
                     "created_at": c.get("created_at", "")}
                    for c in t.get("comments") or []]
        due = t.get("due_date") or ""
        _disc_pair = _discipline(t.get("labels"))
        view = {
            "id": t.get("id", ""), "title": t.get("title", ""),
            "stage": stage,
            "campaign": t.get("campaign", ""), "content_type": t.get("content_type", ""),
            # The discipline (auto label) is the client-safe categorization + shared color language.
            # Campaign-as-title merged the old campaign into `title`, so this chip carries the
            # discipline instead. It's the ONLY department-derived value the client sees (a generic
            # discipline word, never the internal slug/owners/charge). Text + tint from ONE label.
            "discipline": _disc_pair[0], "disc_class": _disc_pair[1],
            # On hold -> the client sees a plain "Paused" (never the internal reason).
            "on_hold": bool(t.get("on_hold")),
            "start_date": t.get("start_date", ""),
            "due_date": due,
            "due_soon": bool(due and today <= due <= soon and stage != "closed"),
            "client_note": t.get("client_note", ""),
            "deliverable_url": t.get("deliverable_url", ""),
            "phases": phases, "subs_done": done, "subs_total": len(subs),
            "pct": (int(round(100.0 * done / len(subs))) if subs else 0),
            "open_changes": len(workspace.task_open_changes(t)),
            "comments": comments, "comment_count": len(comments),
            "in_review": stage == "for_launch",
        }
        (by_key.get(stage) or cols[0])["tasks"].append(view)
    for col in cols:
        # Soonest launch first (undated work sinks to the bottom of its column).
        col["tasks"].sort(key=lambda v: v.get("due_date") or "9999-99-99")
    return cols


@app.route("/w/<client>/task-comment", methods=["POST"])
def atrium_task_comment(client):
    """Client comment / request-changes on a client-facing task -- the Progress tab's ONE write.

    Mirrors /w/<c>/comment on content: kind "changes" raises a change request (a flagged bubble the
    team must resolve); anything else is a plain comment. A task the team kept internal
    (client_facing false) is invisible here -- commenting on it 404s just like a missing one."""
    gate = _atrium_json_gate(client)
    if gate:
        return gate
    task_id = request.form.get("task_id", "").strip()
    body = request.form.get("body", "").strip()
    if not body:
        return Response('{"error":"empty"}', status=400, mimetype="application/json")
    kind = "changes" if request.form.get("kind", "").strip() == "changes" else "comment"
    ws = workspace.load_workspace(client)
    existing = workspace._find_task(ws or {}, task_id)
    if existing is None or (not existing.get("client_facing") and not is_superadmin()):
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    try:
        task, comment = workspace.add_task_comment(
            client, task_id, "client", _client_sender_name(current_user()), body, kind=kind)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    if kind == "changes":
        notify.client_task_changes(client, task, current_user())
    else:
        notify.client_task_commented(client, task, body, current_user())
    _audit(client, "requested task changes" if kind == "changes" else "commented on task",
           task.get("title") or task_id)
    return jsonify(ok=True, comment=comment,
                   open_changes=len(workspace.task_open_changes(task)))


@app.route("/w/<client>/resolve-comment", methods=["POST"])
def atrium_resolve_comment(client):
    """Mark a "Request changes" comment resolved (TEAM only); may return the piece to 'awaiting'.

    Resolving is a team action: only the Agora team decides a change request is addressed. Clients
    raise change requests (via /comment kind=changes) but cannot resolve them -- hence the admin gate."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    comment_id = request.form.get("comment_id", "").strip()
    try:
        _item, _comment, status = workspace.resolve_content_comment(client, content_id, comment_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    _audit(client, "resolved change request", content_id)
    return jsonify(ok=True, status=status)


@app.route("/w/<client>/state.json", methods=["GET"])
def atrium_state(client):
    """Live-state snapshot for in-page polling — per-content review status + comment threads.

    Read-only and intentionally tiny: the client/admin views poll this so each side sees the other's
    comments, change-requests, resolves, and approve/changes status WITHOUT a page reload. There is no
    websocket/pub-sub (single workspace JSON in GCS, Cloud Run multi-instance) — every viewer simply
    reads the same authoritative object, so a poll picks up whatever the other party just wrote. Gated
    exactly like the workspace itself (authed + can_open); the bucket/JSON stays private."""
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    if not can_open(client):
        return Response('{"error":"forbidden"}', status=403, mimetype="application/json")
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    content = {}
    for camp in ws.get("campaigns", []) or []:
        for p in camp.get("content", []) or []:
            cid = p.get("id")
            if not cid:
                continue
            comments = []
            for cm in p.get("comments", []) or []:
                comments.append({
                    "id": cm.get("id", ""),
                    "sender": cm.get("sender", ""),
                    "sender_name": cm.get("sender_name", ""),
                    "body": cm.get("body", ""),
                    "kind": cm.get("kind", "comment"),
                    "resolved": bool(cm.get("resolved")),
                    "when": _atrium_dt(cm.get("created_at", "")),
                })
            content[cid] = {"status": p.get("status", ""), "comments": comments}
    return jsonify(ok=True, content=content)


@app.route("/w/<client>/creative/<path:content_id>", methods=["GET"])
def atrium_creative(client, content_id):
    """Stream a content piece's uploaded creative (authed proxy; the bucket stays private).

    Mirrors the /data.json posture: the object is never public -- it is served only to a session
    that may open this client. Returns 404 when the piece has no uploaded creative.
    """
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response("Not found", status=404, mimetype="text/plain")
    _camp, item = workspace._find_content(ws, content_id)
    if item is None or not item.get("image_object"):
        return Response("Not found", status=404, mimetype="text/plain")
    mime = item.get("image_mime") or "application/octet-stream"
    size = workspace.creative_size(client, content_id)
    if size is None:
        return Response("Not found", status=404, mimetype="text/plain")

    range_header = request.headers.get("Range", "")
    if not range_header:
        # No range: stream the whole object (200) WITHOUT a Content-Length, so werkzeug uses chunked
        # transfer-encoding. Cloud Run caps fixed-length (Content-Length) responses at ~32 MiB but
        # streams chunked ones unbounded -- so a >32 MiB video downloads fine here. (Video playback
        # uses the 206 range path below; its window stays well under the cap.)
        resp = Response(workspace.stream_creative(client, content_id, 0, size - 1), mimetype=mime)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "private, max-age=60"
        return resp

    # Range request (video seeking): return a bounded 206 window so memory stays small.
    window = 8 * 1024 * 1024
    start, end = 0, size - 1
    spec = range_header.split("=", 1)[1].split(",")[0].strip() if "=" in range_header else ""
    lo, _, hi = spec.partition("-")
    try:
        if lo:
            start = int(lo)
            end = int(hi) if hi else size - 1
        elif hi:  # suffix range: last N bytes
            start = max(0, size - int(hi))
    except ValueError:
        start, end = 0, size - 1
    if start >= size:
        return Response(status=416, headers={"Content-Range": "bytes */%d" % size, "Accept-Ranges": "bytes"})
    end = min(end, size - 1, start + window - 1)
    resp = Response(workspace.stream_creative(client, content_id, start, end), status=206, mimetype=mime)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    resp.headers["Content-Length"] = str(end - start + 1)
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


# --- Atrium in-workspace admin editing (super-admin; same /w/<c> surface, JSON actions) ----------
# These power the inline edit affordances rendered into atrium.html for super-admins, so the team
# edits the REAL client workspace in place instead of the separate dark console below. Every action
# reuses the same workspace.py mutators; all are gated super-admin via _atrium_admin_json_gate.
ATRIUM_UPLOAD_MAX_BYTES = 8 * 1024 * 1024    # images: reject larger than 8 MB
ATRIUM_VIDEO_MAX_BYTES = 30 * 1024 * 1024    # videos: kept under Cloud Run's ~32 MiB request cap
ATRIUM_VIDEO_MAX_BYTES_LOCAL = 1024 * 1024 * 1024  # local dev (no Cloud Run cap): in-app accepts up to 1 GB
ATRIUM_FILE_MAX_BYTES = 30 * 1024 * 1024     # any other attachment (pdf, doc, zip…): under Cloud Run's ~32 MiB cap
# A client LOGO is inlined into the workspace JSON (brand.client_logo), which is rewritten in full on
# every edit -- so keep it tiny. Seeded logos sit around ~70 KB; 512 KB is a generous ceiling.
LOGO_MAX_BYTES = 512 * 1024
_ATRIUM_IMAGE_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
    "image/webp": "webp", "image/svg+xml": "svg",
}
_ATRIUM_VIDEO_EXT = {
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
}
# Creatives accept images AND short videos; the stored mime tells the template which to render.
_ATRIUM_MEDIA_EXT = dict(_ATRIUM_IMAGE_EXT)
_ATRIUM_MEDIA_EXT.update(_ATRIUM_VIDEO_EXT)


def _strategy_from_form():
    """Read the two strategy columns (Insight / Action) from the request form into a dict."""
    return {
        "what": request.form.get("what", "").strip(),
        "why": request.form.get("why", "").strip(),
    }


@app.route("/w/<client>/admin/strategy", methods=["POST"])
def atrium_admin_strategy(client):
    """Edit a campaign's name / eyebrow / strategy columns in place."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    campaign_id = request.form.get("campaign_id", "").strip()
    fields = {"strategy": _strategy_from_form()}
    if request.form.get("name") is not None:
        fields["name"] = request.form.get("name", "").strip()
    if request.form.get("eyebrow") is not None:
        fields["eyebrow"] = request.form.get("eyebrow", "").strip()
    try:
        camp = workspace.update_campaign(client, campaign_id, **fields)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    _audit(client, "edited strategy", camp.get("name") or campaign_id)
    return jsonify(ok=True, strategy=camp.get("strategy"), name=camp.get("name"),
                   eyebrow=camp.get("eyebrow"))


@app.route("/w/<client>/admin/strategy-doc", methods=["POST"])
def atrium_admin_strategy_doc(client):
    """Attach or clear the Google Doc URL backing a campaign's AI summary."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    try:
        camp = workspace.set_strategy_doc(client, request.form.get("campaign_id", "").strip(),
                                          request.form.get("doc_url", "").strip())
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    _audit(client, "set strategy doc", camp.get("name") or "")
    return jsonify(ok=True, strategy_doc=camp.get("strategy_doc", ""))


@app.route("/w/<client>/admin/generate-summary", methods=["POST"])
def atrium_admin_generate_summary(client):
    """Read the campaign's attached Google Doc and (re)write its Insight/Action strategy. Always degrades.

    This regenerates the two strategy sections from the doc for an EXISTING campaign -- the same
    thing the Add-campaign modal does at creation -- so an admin can refresh them after editing the
    doc (or after enabling AI). Degrades: an unreadable doc returns ok:false with share guidance.
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    campaign_id = request.form.get("campaign_id", "").strip()
    ws = workspace.load_workspace(client)
    camp = workspace._find_campaign(ws, campaign_id) if ws else None
    if camp is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    doc_url = (request.form.get("doc_url", "").strip() or camp.get("strategy_doc", ""))
    if request.form.get("doc_url") is not None:
        workspace.set_strategy_doc(client, campaign_id, doc_url)
    strategy, source = atrium_docs.generate_strategy(doc_url)
    if not strategy:
        return jsonify(ok=False, source=source,
                       message="Couldn't read that Google Doc. Open it → Share → General access → "
                               "“Anyone with the link” (Viewer), then try again.")
    workspace.update_campaign(client, campaign_id, strategy=strategy)
    _audit(client, "generated AI strategy", (camp or {}).get("name") or campaign_id)
    return jsonify(ok=True, strategy=strategy, source=source)


@app.route("/w/<client>/admin/summary", methods=["POST"])
def atrium_admin_summary(client):
    """Hand-edit a campaign's AI summary text."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    try:
        camp = workspace.update_campaign(client, request.form.get("campaign_id", "").strip(),
                                         ai_summary=request.form.get("ai_summary", "").strip())
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    _audit(client, "edited AI summary", camp.get("name") or "")
    return jsonify(ok=True, ai_summary=camp.get("ai_summary", ""))


@app.route("/w/<client>/admin/campaign", methods=["POST"])
def atrium_admin_add_campaign(client):
    """Add a campaign in place. With just a name + a Google Doc link, AI writes the strategy.

    If a doc link is supplied (and the strategy fields weren't typed by hand) we best-effort read
    the doc and let AI write the campaign's "Insight / Action / What to do next?"
    sections, so the campaign lands fully formed from the doc alone. The doc link is ALWAYS saved
    regardless of whether AI is wired, so the client still gets the "View the full breakdown" link
    on a default (no-Docs/no-AI) deploy -- the admin then fills the sections via "Edit strategy".
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    name = request.form.get("name", "").strip()
    if not name:
        return Response('{"error":"name_required"}', status=400, mimetype="application/json")
    channel = "paid" if request.form.get("channel") == "paid" else "organic"
    doc_url = request.form.get("strategy_doc", "").strip()
    strategy = _strategy_from_form()       # empty from the quick modal; kept for any hand-typed flow
    source = "manual" if any(strategy.values()) else "none"
    if doc_url and not any(strategy.values()):
        generated, source = atrium_docs.generate_strategy(doc_url)
        if generated:
            strategy = generated
        else:
            # A doc was supplied but couldn't be read -- tell the admin how to fix it rather than
            # silently creating an empty campaign. They can share the doc and retry, or drop the link.
            return jsonify(ok=False, source=source,
                           message="Couldn't read that Google Doc. Open it → Share → General access "
                                   "→ “Anyone with the link” (Viewer), then try again. Or "
                                   "remove the link to add the campaign and write the strategy by hand.")
    camp = workspace.add_campaign(client, channel, name, request.form.get("eyebrow", "").strip(),
                                  strategy=strategy, strategy_doc=doc_url)
    _audit(client, "added campaign", name)
    return jsonify(ok=True, id=camp.get("id"), source=source)


@app.route("/w/<client>/admin/delete-campaign", methods=["POST"])
def atrium_admin_delete_campaign(client):
    """Delete a campaign (and clean up its content's uploaded creatives)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    campaign_id = request.form.get("campaign_id", "").strip()
    try:
        removed = workspace.delete_campaign(client, campaign_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    _trash(client, "campaign", removed.get("name") or campaign_id, removed)
    _audit(client, "deleted campaign", removed.get("name") or campaign_id)
    for item in removed.get("content", []):
        if item.get("image_object"):
            workspace.delete_creative(client, item.get("id"))
    return jsonify(ok=True)


@app.route("/w/<client>/admin/content", methods=["POST"])
def atrium_admin_add_content(client):
    """Add a content piece to a campaign in place (status -> awaiting) and notify the client."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    campaign_id = request.form.get("campaign_id", "").strip()
    content = {
        "ref": request.form.get("ref", "").strip(),
        "type_tag": request.form.get("type_tag", "").strip(),
        "sub_tag": request.form.get("sub_tag", "").strip(),
        "platform": request.form.get("platform", "").strip(),
        "caption": request.form.get("caption", "").strip(),
        "social_caption": request.form.get("social_caption", "").strip(),
        "video_url": request.form.get("video_url", "").strip(),
        # An optional publish date mirrors the piece onto the Content Calendar as a linked event.
        "date": request.form.get("date", "").strip(),
    }
    if content["ref"]:
        content["id"] = content["ref"]
    try:
        item = workspace.add_content(client, campaign_id, content)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    notify.team_added_content(client, workspace.load_workspace(client), item)
    _audit(client, "added content", item.get("ref") or item.get("id") or "")
    return jsonify(ok=True, id=item.get("id"))


@app.route("/w/<client>/admin/edit-content", methods=["POST"])
def atrium_admin_edit_content(client):
    """Patch a content piece's editable fields (ref/type/sub/platform/caption) in place."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    fields = {}
    for key in ("ref", "type_tag", "sub_tag", "platform", "caption", "social_caption", "video_url", "date"):
        if request.form.get(key) is not None:
            fields[key] = request.form.get(key, "").strip()
    try:
        item = workspace.update_content(client, content_id, fields)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    _audit(client, "edited content", item.get("ref") or content_id)
    return jsonify(ok=True, content=item)


@app.route("/w/<client>/admin/move-content", methods=["POST"])
def atrium_admin_move_content(client):
    """Reassign a content piece to a different campaign in place.

    Returns the destination campaign's id + channel so the client can jump to it (a cross-channel
    move lands the piece under the other tab). 404s if the piece or target campaign is gone.
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    campaign_id = request.form.get("campaign_id", "").strip()
    try:
        camp, item = workspace.move_content(client, content_id, campaign_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    _audit(client, "moved content",
           "%s -> %s" % (item.get("ref") or content_id, camp.get("name") or campaign_id))
    return jsonify(ok=True, id=item.get("id"), campaign_id=camp.get("id"),
                   channel=camp.get("channel") or "organic")


@app.route("/w/<client>/admin/video-link", methods=["POST"])
def atrium_admin_video_link(client):
    """Attach (or clear) a video by URL on a content piece. An empty url removes the link.

    This is the 'provide a link' half of the Add-video control; the '.mp4 file' half reuses the
    existing creative upload (/admin/add-images), which already stores and renders video creatives.
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    url = request.form.get("url", "").strip()
    # Only accept http(s) links (or an empty string to clear); never store javascript:/data: URIs.
    if url and not (url.startswith("http://") or url.startswith("https://")):
        return jsonify(ok=False, message="Enter a valid http(s) video link."), 400
    try:
        item = workspace.update_content(client, content_id, {"video_url": url})
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True, content=item)


@app.route("/w/<client>/admin/delete-content", methods=["POST"])
def atrium_admin_delete_content(client):
    """Delete a content piece in place (and its uploaded creative object, if any)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    # Capture which campaign holds the piece BEFORE deleting, so a Trash restore knows where to put it.
    ws_pre = workspace.load_workspace(client)
    camp_pre, _pre = workspace._find_content(ws_pre, content_id) if ws_pre else (None, None)
    campaign_id = (camp_pre or {}).get("id", "")
    try:
        removed = workspace.delete_content(client, content_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    _trash(client, "content", removed.get("ref") or content_id, removed,
           extra={"campaign_id": campaign_id})
    _audit(client, "deleted content", removed.get("ref") or content_id)
    if removed.get("image_object"):
        workspace.delete_creative(client, content_id)
    return jsonify(ok=True)


@app.route("/w/<client>/admin/content-comment", methods=["POST"])
def atrium_admin_content_comment(client):
    """Post a team comment on a content piece (notifies opted-in client recipients)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    body = request.form.get("body", "").strip()
    if not body:
        return Response('{"error":"empty"}', status=400, mimetype="application/json")
    sender_name = request.form.get("sender_name", "").strip() or "AGORA"
    # The team posts a plain comment or a "Notify" (kind=notify) -- a flagged note to the client.
    # "Request changes" (kind=changes) is a CLIENT-only power, so it is never honored here.
    kind = "notify" if request.form.get("kind", "").strip() == "notify" else "comment"
    try:
        item, comment = workspace.add_content_comment(
            client, content_id, "agora", sender_name, body, kind=kind,
        )
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    notify.team_commented(client, workspace.load_workspace(client), item, body, sender_name)
    _audit(client, "notified client" if kind == "notify" else "commented on content",
           item.get("ref") or content_id)
    return jsonify(ok=True, comment=comment)


@app.route("/w/<client>/admin/delete-comment", methods=["POST"])
def atrium_admin_delete_comment(client):
    """Delete a single comment from a content piece's thread (team-only); may return it to 'awaiting'."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    comment_id = request.form.get("comment_id", "").strip()
    try:
        _item, status = workspace.delete_content_comment(client, content_id, comment_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    _audit(client, "deleted comment", content_id)
    return jsonify(ok=True, status=status)


@app.route("/w/<client>/admin/upload-creative", methods=["POST"])
def atrium_admin_upload_creative(client):
    """Upload an image or short video creative for a content piece (private object; image ≤8MB, video ≤30MB)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return Response('{"error":"no_file"}', status=400, mimetype="application/json")
    mime = (upload.mimetype or "").lower()
    if mime not in _ATRIUM_MEDIA_EXT:
        return Response('{"error":"unsupported_type"}', status=400, mimetype="application/json")
    video_cap = ATRIUM_VIDEO_MAX_BYTES_LOCAL if _LOCAL_BACKEND else ATRIUM_VIDEO_MAX_BYTES
    max_bytes = video_cap if mime in _ATRIUM_VIDEO_EXT else ATRIUM_UPLOAD_MAX_BYTES
    data = upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        return Response('{"error":"too_large"}', status=413, mimetype="application/json")
    ws = workspace.load_workspace(client)
    _camp, item = workspace._find_content(ws, content_id) if ws else (None, None)
    if item is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    object_name = workspace.write_creative(client, content_id, data, content_type=mime)
    workspace.set_content_image(client, content_id, object_name, mime)
    return jsonify(ok=True, url=url_for("atrium_creative", client=client, content_id=content_id))


@app.route("/w/<client>/admin/creative-upload-url", methods=["POST"])
def atrium_admin_creative_upload_url(client):
    """Mint a V4 signed PUT URL so the browser uploads a LARGE creative DIRECTLY to GCS, bypassing
    Cloud Run's ~32 MiB request cap. The client then calls /creative-confirm to record it."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    mime = (request.form.get("content_type", "") or "").lower()
    if mime not in _ATRIUM_MEDIA_EXT:
        return Response('{"error":"unsupported_type"}', status=400, mimetype="application/json")
    ws = workspace.load_workspace(client)
    _camp, item = workspace._find_content(ws, content_id) if ws else (None, None)
    if item is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    try:
        url, _obj = workspace.signed_upload_url(client, content_id, mime)
    except Exception as e:
        # Signing unavailable/misconfigured -> tell the client to fall back to the in-app upload.
        # app.logger isn't always wired to Cloud Run's log stream, so print the traceback to stderr
        # (which IS captured) and surface the error detail (this route is super-admin only).
        import sys
        import traceback as _tb
        sys.stderr.write("creative-upload-url signing failed for %s/%s\n" % (client, content_id))
        _tb.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return jsonify(ok=False, error="sign_unavailable", detail=str(e))
    if not url:
        return jsonify(ok=False, error="sign_unavailable", detail="signer_returned_no_url")
    return jsonify(ok=True, url=url, mime=mime)


@app.route("/w/<client>/admin/creative-confirm", methods=["POST"])
def atrium_admin_creative_confirm(client):
    """Record a creative that was uploaded straight to GCS via a signed URL. Verifies it landed."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    mime = (request.form.get("content_type", "") or "").lower()
    if mime not in _ATRIUM_MEDIA_EXT:
        return Response('{"error":"unsupported_type"}', status=400, mimetype="application/json")
    ws = workspace.load_workspace(client)
    _camp, item = workspace._find_content(ws, content_id) if ws else (None, None)
    if item is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    if workspace.creative_size(client, content_id) is None:
        return Response('{"error":"not_uploaded"}', status=400, mimetype="application/json")
    object_name = workspace.creative_object_name(client, content_id)
    workspace.set_content_image(client, content_id, object_name, mime)
    return jsonify(ok=True, url=url_for("atrium_creative", client=client, content_id=content_id))


@app.route("/w/<client>/admin/remove-creative", methods=["POST"])
def atrium_admin_remove_creative(client):
    """Remove a content piece's uploaded creative (object + workspace pointer)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    try:
        workspace.clear_content_image(client, content_id)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    workspace.delete_creative(client, content_id)
    return jsonify(ok=True)


# --- Multiple images per content piece: authed serve + admin add/remove -----------------------
@app.route("/w/<client>/creative/<path:content_id>/<image_id>", methods=["GET"])
def atrium_creative_image(client, content_id, image_id):
    """Serve ONE image of a content piece (authed proxy; the bucket stays private).

    Same posture as the single-creative route: never public, served only to a session that may open
    this client. Images are small (<=8MB) so we return them whole.
    """
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response("Not found", status=404, mimetype="text/plain")
    _camp, item = workspace._find_content(ws, content_id)
    img = next((im for im in item.get("images", []) if im.get("id") == image_id), None) if item else None
    if img is None:
        # Content ids are free-text titles, so one may literally contain "/" (e.g. "Engaged Lead /
        # Considering"). The router then splits such a LEGACY single-creative/video id into
        # (content_id, image_id) and lands here. Re-join and serve it via the single-creative route
        # before giving up, so a slashed-title creative still loads.
        whole = content_id + "/" + image_id
        _c2, w_item = workspace._find_content(ws, whole)
        if w_item is not None and w_item.get("image_object"):
            return atrium_creative(client, whole)
        return Response("Not found", status=404, mimetype="text/plain")
    data = workspace.read_content_image_bytes(client, content_id, image_id)
    if data is None:
        return Response("Not found", status=404, mimetype="text/plain")
    mime = img.get("mime") or "application/octet-stream"
    resp = Response(data, mimetype=mime)
    resp.headers["Cache-Control"] = "private, max-age=300"
    # Default = inline so a PDF previews in an <iframe>; ?dl=1 forces a download with the original
    # name (the doc lightbox's download button uses ?dl=1). Always carry the filename so a download
    # keeps the real name regardless of disposition.
    safe = (img.get("name") or ("file-" + image_id)).replace('"', "").replace("\r", "").replace("\n", "")
    disposition = "attachment" if request.args.get("dl") == "1" else "inline"
    resp.headers["Content-Disposition"] = '%s; filename="%s"' % (disposition, safe)
    return resp


@app.route("/w/<client>/docview/<path:content_id>/<image_id>", methods=["GET"])
def atrium_docview(client, content_id, image_id):
    """Render an attached Office document (docx/xlsx/pptx/csv/txt) to a scrollable HTML preview,
    served in an <iframe> by the content card + doc lightbox. Same private/authed posture as the
    creative serve route; PDFs are previewed natively (inline serve) and never reach this route."""
    if not authed():
        return redirect(url_for("login", next=request.full_path))
    if not can_open(client):
        return Response("Forbidden", status=403, mimetype="text/plain")
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response("Not found", status=404, mimetype="text/plain")
    _camp, item = workspace._find_content(ws, content_id)
    if item is None:
        return Response("Not found", status=404, mimetype="text/plain")
    img = next((im for im in item.get("images", []) if im.get("id") == image_id), None)
    if img is None:
        return Response("Not found", status=404, mimetype="text/plain")
    data = workspace.read_content_image_bytes(client, content_id, image_id)
    if data is None:
        return Response("Not found", status=404, mimetype="text/plain")
    import atrium_docview
    page = atrium_docview.render_doc_html(data, img.get("mime") or "", img.get("name") or "")
    resp = Response(page, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@app.route("/w/<client>/admin/add-images", methods=["POST"])
def atrium_admin_add_images(client):
    """Append one or more images to a content piece (the approval ticket's picture row). Field: 'files'."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    ws = workspace.load_workspace(client)
    _camp, item = workspace._find_content(ws, content_id) if ws else (None, None)
    if item is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    uploads = request.files.getlist("files")
    if not uploads and "file" in request.files:
        uploads = [request.files["file"]]
    added = []
    for upload in uploads:
        if not upload or not upload.filename:
            continue
        mime = (upload.mimetype or "").lower()
        # Any file type is accepted: images/video render inline, everything else as a download chip.
        # Size cap depends on kind (images are inlined small; videos/other files share the request cap).
        if mime in _ATRIUM_IMAGE_EXT:
            max_bytes = ATRIUM_UPLOAD_MAX_BYTES
        elif mime in _ATRIUM_VIDEO_EXT:
            max_bytes = ATRIUM_VIDEO_MAX_BYTES
        else:
            max_bytes = ATRIUM_FILE_MAX_BYTES
        data = upload.read(max_bytes + 1)
        if len(data) > max_bytes:
            continue
        name = os.path.basename(upload.filename or "")
        image_id = workspace._new_id("img")
        workspace.add_content_image(client, content_id, image_id, data, mime, name=name)
        added.append({
            "id": image_id, "mime": mime, "name": name,
            "url": url_for("atrium_creative_image", client=client, content_id=content_id, image_id=image_id),
        })
    if not added:
        return Response('{"error":"no_valid_files"}', status=400, mimetype="application/json")
    return jsonify(ok=True, added=added)


@app.route("/w/<client>/admin/remove-image", methods=["POST"])
def atrium_admin_remove_image(client):
    """Remove one image from a content piece (object + pointer)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    content_id = request.form.get("content_id", "").strip()
    image_id = request.form.get("image_id", "").strip()
    workspace.remove_content_image(client, content_id, image_id)
    return jsonify(ok=True)


@app.route("/w/<client>/admin/metrics", methods=["POST"])
def atrium_admin_metrics(client):
    """Edit headline counts (today + split) and the KPI metric values/trends in place."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")

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
    _audit(client, "updated metrics")
    return jsonify(ok=True)


@app.route("/w/<client>/admin/goal", methods=["POST"])
def atrium_admin_goal(client):
    """Set the per-client Monthly goal: label / format / three tiers / current / optional source metric."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")

    def _num(name):
        try:
            return float(request.form.get(name, "") or 0)
        except (TypeError, ValueError):
            return 0.0

    fmt = request.form.get("goal_format", "number").strip()
    workspace.set_goal(client, {
        "label": request.form.get("goal_label", "").strip() or "goal",
        "format": "currency" if fmt == "currency" else "number",
        "target": _num("goal_target"),
        "exceed": _num("goal_exceed"),
        "breakthrough": _num("goal_breakthrough"),
        "current": _num("goal_current"),
        "source_metric": request.form.get("goal_source_metric", "").strip(),
    })
    return jsonify(ok=True)


@app.route("/w/<client>/admin/reach", methods=["POST"])
def atrium_admin_reach(client):
    """Set the per-client Total reach headline (this vs last month) shown on the Overview card."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    workspace.set_reach(client,
                        request.form.get("reach_current", "").strip(),
                        request.form.get("reach_previous", "").strip())
    return jsonify(ok=True)


@app.route("/w/<client>/admin/communication", methods=["POST"])
def atrium_admin_communication(client):
    """Add, edit, or delete a communication entry in the unified Communications timeline. `op` is
    'add' | 'edit' | 'delete'. An entry carries a `channel` (email/upwork/slack/meeting/call/note),
    an `audience` (client|team), a title, a summary, an optional date, and optional `people`."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    op = request.form.get("op", "").strip()
    if op == "delete":
        workspace.delete_communication(client, request.form.get("item_id", "").strip())
        _audit(client, "deleted a communication")
        return jsonify(ok=True)
    if op == "add":
        item = workspace.add_communication(
            client,
            request.form.get("channel", "").strip(),
            request.form.get("title", "").strip(),
            request.form.get("summary", "").strip(),
            date=((request.form.get("date", "") or "").strip() or None),
            people=request.form.get("people", "").strip(),
            audience=request.form.get("audience", "").strip(),
        )
        _audit(client, "added a communication", item.get("title", ""))
        return jsonify(ok=True, id=item.get("id"))
    if op == "edit":
        fields = {}
        for key in ("channel", "audience", "title", "summary", "date", "people"):
            if request.form.get(key) is not None:
                fields[key] = request.form.get(key, "").strip()
        workspace.update_communication(client, request.form.get("item_id", "").strip(), fields)
        _audit(client, "edited a communication")
        return jsonify(ok=True)
    return Response('{"error":"bad_op"}', status=400, mimetype="application/json")


def _intel_client_context(ws):
    """A short plain-text digest of what the workspace already knows about a client, for the AI to
    draft the Research-Brain settings from ('Write with AI'). Best-effort: every field is optional,
    and the whole digest is capped so the call stays small."""
    lines = []
    url = ((ws.get("website_health") or {}).get("url") or "").strip()
    if url:
        lines.append("Their website: %s" % url)
    for camp in (ws.get("campaigns") or []):
        bits = [b for b in ((camp.get("name") or "").strip(), (camp.get("eyebrow") or "").strip()) if b]
        strat = camp.get("strategy")
        if isinstance(strat, dict):
            bits.extend(str(v).strip() for v in strat.values() if str(v or "").strip())
        summary = (camp.get("ai_summary") or "").strip()
        if summary:
            bits.append(summary)
        if bits:
            lines.append("Campaign (%s): %s" % (camp.get("channel") or "paid", " — ".join(bits)[:400]))
    industries = sorted({(ch.get("industry") or "").strip()
                         for ch in workspace.watcher_channels(ws)} - {""})
    if industries:
        lines.append("Industries of the creators/competitors the team watches: %s" % ", ".join(industries))
    topics = workspace.get_intel_topics(ws)
    if topics:
        lines.append("Current research keywords (improve on these): %s" % ", ".join(topics))
    return "\n".join(lines)[:4000]


@app.route("/w/<client>/admin/intel", methods=["POST"])
def atrium_admin_intel(client):
    """Add/edit/delete a Market Intelligence entry, OR configure the AI research brain (team-only).

    `op` is one of:
      * 'add' | 'edit' | 'delete' -- a curated briefing entry (`section` = business_research|media_buying).
      * 'ai_settings'             -- save the selected model + the two tunable per-section prompts.
      * 'topics'                  -- save the client's Business-Research research keywords.
      * 'suggest'                 -- AI-draft the keywords + both focus prompts from what the
                                     workspace knows about this client (returned, NOT saved --
                                     the panel fills the fields for the admin to review + Save).
      * 'refresh-now'             -- run the daily refresh for THIS client right now (test the brain).
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    op = request.form.get("op", "").strip()

    # --- AI research config (model + tunable prompts) ---------------------------------------------
    if op == "ai_settings":
        model = request.form.get("model", "").strip()
        if model and intel_ai.model_meta(model) is None:
            return jsonify(ok=False, message="Unknown model."), 400
        if model and not intel_ai.model_available(model):
            return jsonify(ok=False, message="That model's API key isn't configured on the server."), 400
        window = request.form.get("window", "").strip()
        if window and not intel_ai.valid_window(window):
            return jsonify(ok=False, message="Unknown date range."), 400
        fields = {
            "model": model,
            "business_prompt": request.form.get("business_prompt", ""),
            "media_prompt": request.form.get("media_prompt", ""),
        }
        if window:
            fields["window"] = window
        if request.form.get("count") is not None:
            fields["count"] = str(intel_ai.count_of({"count": request.form.get("count", "")}))
        if request.form.get("show_thinking") is not None:
            fields["show_thinking"] = "1" if request.form.get("show_thinking") in ("1", "true", "on", "True") else ""
        workspace.set_intel_ai(client, fields)
        _audit(client, "set intel AI settings", model or "(off)")
        return jsonify(ok=True)

    # --- Per-client Business-Research keywords ----------------------------------------------------
    if op == "topics":
        topics = workspace.set_intel_topics(client, request.form.get("topics", ""))
        _audit(client, "set intel topics", ", ".join(topics))
        return jsonify(ok=True, topics=topics)

    # --- AI-draft the keywords + both focus prompts from what we know about this client -----------
    if op == "suggest":
        ws = workspace.load_workspace(client) or {}
        try:
            fields, err = intel_ai.suggest_config(
                ws.get("display_name") or client,
                _intel_client_context(ws),
                model=workspace.get_intel_ai(ws).get("model"),
            )
        except Exception as exc:  # never 500 the console; report it
            return jsonify(ok=False, message="Drafting failed: %s" % str(exc)[:200]), 200
        if fields is None:
            return jsonify(ok=False, message="Couldn't draft the prompts: %s" % err), 200
        _audit(client, "AI-drafted intel keywords/prompts")
        return jsonify(ok=True, topics=fields.get("topics", ""),
                       business_prompt=fields.get("business_prompt", ""),
                       media_prompt=fields.get("media_prompt", ""))

    # --- Run the daily refresh now for this one client (so the team can test the brain) -----------
    if op == "refresh-now":
        try:
            counts = intel_refresh.refresh_client(client)
        except Exception as exc:  # never 500 the console; report it
            return jsonify(ok=False, message="Refresh failed: %s" % str(exc)[:200]), 200
        ws2 = workspace.load_workspace(client) or {}
        err = (ws2.get("intel_ai") or {}).get("last_error", "")
        _audit(client, "ran intel refresh", ("ai: %d items" % (counts.get("media_buying", 0)
               + counts.get("business_research", 0))) if counts.get("ai") else ("no fill: %s" % err))
        return jsonify(ok=True, ai=bool(counts.get("ai")), error=err, counts=counts)

    # --- Bulk action on selected entries: mass delete / mass favourite (star + pin) ---------------
    if op == "bulk":
        section = request.form.get("section", "").strip()
        if workspace._intel_key(section) is None:
            return Response('{"error":"bad_section"}', status=400, mimetype="application/json")
        action = request.form.get("action", "").strip()
        if action not in workspace.INTEL_BULK_ACTIONS:
            return jsonify(ok=False, message="Unknown bulk action."), 400
        # Accept either repeated `entry_ids` fields OR a single comma-joined value (or a mix), so a
        # front-end that coerces the id array to one comma string still deletes every selected entry.
        ids = []
        for raw in (request.form.getlist("entry_ids") or [request.form.get("entry_ids", "")]):
            ids.extend((raw or "").split(","))
        ids = [i.strip() for i in ids if i.strip()]
        if not ids:
            return jsonify(ok=False, message="Nothing selected."), 400
        workspace.bulk_intel(client, section, action, ids)
        _audit(client, "bulk intel %s" % action, "%d entr%s" % (len(ids), "y" if len(ids) == 1 else "ies"))
        return jsonify(ok=True, count=len(ids))

    # Both sections also auto-refresh DAILY (services/intel-refresh); the team ADDS/EDITS curated
    # entries here, which are preserved across the auto-refresh (only `auto` entries are swapped).
    section = request.form.get("section", "").strip()
    if workspace._intel_key(section) is None:
        return Response('{"error":"bad_section"}', status=400, mimetype="application/json")
    if op == "delete":
        workspace.delete_intel_entry(client, section, request.form.get("entry_id", "").strip())
        _audit(client, "deleted intel entry", section)
        return jsonify(ok=True)
    fields = {}
    for key in workspace._INTEL_FIELDS:
        if request.form.get(key) is not None:
            fields[key] = request.form.get(key, "").strip()
    if op == "add":
        if not (fields.get("body") or fields.get("title") or fields.get("heading")):
            return jsonify(ok=False, message="Add a heading, headline, or some text first."), 400
        item = workspace.add_intel_entry(client, section, fields)
        _audit(client, "added intel entry", fields.get("title") or section)
        return jsonify(ok=True, id=item.get("id"))
    if op == "edit":
        workspace.update_intel_entry(client, section, request.form.get("entry_id", "").strip(), fields)
        _audit(client, "edited intel entry", section)
        return jsonify(ok=True)
    return Response('{"error":"bad_op"}', status=400, mimetype="application/json")


@app.route("/w/<client>/admin/dashboard-url", methods=["POST"])
def atrium_admin_dashboard_url(client):
    """Set the per-client Looker Studio embed URL (https only; empty hides the dashboard), the report
    native height in px (default 800, clamped 200..5000) and native width (default 1200, clamped
    320..5000). The embed scales so the native width fills the container -- no dead strip on the right."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    url = (request.form.get("url", "") or "").strip()
    if url and not url.lower().startswith("https://"):
        return Response('{"error":"must_be_https"}', status=400, mimetype="application/json")
    try:
        height = int(request.form.get("height", "") or 800)
    except (TypeError, ValueError):
        height = 800
    try:
        width = int(request.form.get("width", "") or 1200)
    except (TypeError, ValueError):
        width = 1200
    workspace.set_dashboard_url(client, url, max(200, min(height, 5000)), max(320, min(width, 5000)))
    return jsonify(ok=True)


# --- Website Health (team-only tab: site monitoring + tag detection; THE super admin edits) --------
@app.route("/w/<client>/admin/website-health/save", methods=["POST"])
def atrium_admin_website_health_save(client):
    """Save the monitored website URL and/or team notes (Website Health tab; super-admin only).

    Each field is written only when present in the form, so saving notes never clobbers the URL and
    vice-versa.
    """
    gate = _atrium_root_json_gate(client)
    if gate:
        return gate
    if workspace.load_workspace(client) is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    if "url" in request.form:
        workspace.set_website_url(client, request.form.get("url", "").strip())
    if "notes" in request.form:
        workspace.set_website_notes(client, request.form.get("notes", ""))
    _audit(client, "saved website health settings")
    return jsonify(ok=True)


@app.route("/w/<client>/admin/website-health/check", methods=["POST"])
def atrium_admin_website_health_check(client):
    """Run a website health check (reachability + tag detection) and store the result (super-admin only).

    The URL comes from the form (so an unsaved-but-typed URL still checks AND is remembered) or falls
    back to the stored one. Degrades gracefully: a dead site / network error still returns ok:true with
    the failure recorded INSIDE the result (so the tab shows the problem); only a MISSING url returns
    ok:false with guidance. Detection scans the live page's HTML -- no GTM API, no new infra.
    """
    gate = _atrium_root_json_gate(client)
    if gate:
        return gate
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    url = request.form.get("url", "").strip() or (ws.get("website_health") or {}).get("url", "")
    if not url:
        return jsonify(ok=False, message="Enter a website URL first.")
    import atrium_health  # lazy: only this route needs it
    result = atrium_health.check_website(url)
    workspace.save_website_check(client, result)
    _audit(client, "ran website health check", url)
    return jsonify(ok=True, result=result)


# --- Watcher (team-only tab: YouTube channel transcript archive) ---------------------------------
_WATCHER_PREVIEW_CHARS = 260


def _watcher_view(ws, client):
    """The Watcher pane's render model: each registry entry + its video cards, transcript bodies
    trimmed to a short preview (the full text is served on demand by atrium_watcher_video).

    Adds the filter/sort fields the creator grid needs: platform/industry/kind (with defaults for
    channels added before those fields existed) and `latest` -- the newest video's estimated
    publish date (fallback: the day the channel was added) -- which drives the date sort."""
    out = []
    safe_queue = set(workspace.watcher_safe_pull_queue(ws))
    for ch in workspace.watcher_channels(ws):
        entry = dict(ch)
        entry.setdefault("platform", "youtube")
        entry.setdefault("industry", "")
        entry.setdefault("kind", "creator")
        entry["loose"] = bool(ch.get("loose"))  # the "Saved videos" pseudo-channel (single scrapes)
        entry["safe_queued"] = ch.get("id") in safe_queue
        cards = []
        latest = ""
        for v in workspace.read_watcher_videos(client, ch.get("id", "")):
            text = v.get("transcript") or ""
            published = v.get("published", "")
            if published > latest:
                latest = published
            cards.append({
                "id": v.get("id", ""),
                "title": v.get("title", ""),
                "url": v.get("url", ""),
                "has_transcript": bool(text),
                "preview": (text[:_WATCHER_PREVIEW_CHARS] + "…") if len(text) > _WATCHER_PREVIEW_CHARS else text,
                "words": len(text.split()) if text else 0,
                "error": v.get("error", ""),
                "published": published,
                "published_text": v.get("published_text", ""),
            })
        entry["videos"] = cards
        entry["latest"] = latest or (ch.get("added_at", "") or "")[:10]
        out.append(entry)
    return out


_WATCHER_KINDS = ("creator", "competitor")


def _watcher_autolabel(title, video_titles):
    """Ask the intel AI for a short industry label for a creator ('AI Automation', 'Fitness', ...).

    Judged from the channel name + its video titles (plenty of signal, no transcript needed).
    Returns (industry, error) -- ("", reason) when no AI provider is configured or the call fails,
    so adding a channel NEVER breaks on labeling; the chip just stays empty and hand-editable."""
    titles = [t for t in (video_titles or []) if t][:40]
    if not titles:
        return "", "no video titles to judge from"
    system = (
        "You classify content creators into ONE short industry/niche label (1-3 words, Title Case) "
        "for a marketing team's watchlist. Examples: \"AI Automation\", \"E-commerce\", \"Fitness\", "
        "\"Personal Finance\", \"Real Estate\", \"Digital Marketing\". "
        "Answer with JSON only: {\"industry\": \"<label>\"}"
    )
    user = "Creator: %s\nRecent video titles:\n%s" % (title, "\n".join("- " + t for t in titles))
    raw, err = intel_ai.classify_text(system, user, max_tokens=128)
    if err:
        return "", err
    m = re.search(r'"industry"\s*:\s*"([^"]{1,60})"', raw or "")
    if not m:
        return "", "AI returned no label"
    return m.group(1).strip(), ""


def _watcher_counts(client, channel_id, videos):
    """Refresh a registry entry's counts/last_fetch from its video list; returns pending count."""
    pending = sum(1 for v in videos if not v.get("transcript") and not v.get("error"))
    workspace.update_watcher_channel(client, channel_id, {
        "video_count": len(videos),
        "transcript_count": sum(1 for v in videos if v.get("transcript")),
        "failed_count": sum(1 for v in videos if v.get("error")),
        "last_fetch": workspace.now_iso(),
    })
    return pending


def _watcher_video_entry(v):
    """A fresh archive entry for one listed video (no transcript yet)."""
    import watcher
    published_text = v.get("published_text", "")
    return {"id": v["id"], "title": v.get("title", ""),
            "url": "https://www.youtube.com/watch?v=" + v["id"],
            "transcript": "", "language": "", "generated": False,
            "error": "", "permanent": False, "fetched_at": "",
            "published_text": published_text,
            "published": watcher.published_estimate(published_text)}


@app.route("/w/<client>/admin/watcher", methods=["POST"])
def atrium_admin_watcher(client):
    """Manage watched YouTube channels (team-only). `op` is one of:

    * add     -- resolve the pasted channel `url`, list EVERY video, auto-label the industry,
                 store the (transcript-less) archive; transcripts come from repeated `fetch` calls.
    * add_video - scrape a SINGLE pasted video `url`: resolve its title, fetch its transcript inline,
                 and save it under the per-client "Saved videos" pseudo-channel. The transcript is
                 returned in the response (shown immediately); a rate-limit is reported `blocked` and
                 the video is saved pending, so Fetch missing / Safe pull on the card can finish it.
    * fetch   -- fetch the next batch of MISSING transcripts only (the page JS loops this until
                 `remaining` hits 0, so each request stays short). A YouTube rate-limit stops the
                 batch and reports `blocked` WITHOUT marking any video failed, so the next fetch
                 resumes exactly where it stopped. `retry=1` first clears non-permanent errors.
    * safe_pull - queue the channel for the LOCAL safe scraper instead of fetching here (Cloud Run
                 IPs get blocked regardless of pacing). The operator machine's scheduled task
                 (safe_scrape_local.py --queue) picks the queue up within minutes and works through
                 it slowly; transcripts appear as they sync back.
    * refresh -- re-list the channel: add newly uploaded videos, refresh upload dates (existing
                 transcripts kept).
    * meta    -- hand-edit the classification (industry text and/or kind creator|competitor).
    * label   -- re-run the AI industry auto-label from the stored video titles.
    * delete  -- remove the channel and its whole transcript archive.
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    import watcher  # lazy: only this route (and the GET below) needs it
    op = request.form.get("op", "")

    if op == "add":
        info = watcher.resolve_channel(request.form.get("url", ""))
        if not info["ok"]:
            return jsonify(ok=False, message=info["error"])
        for ch in workspace.watcher_channels(ws):
            if ch.get("channel_id") == info["channel_id"]:
                return jsonify(ok=False, message="Already watching %s." % (ch.get("title") or "that channel"))
        listing = watcher.list_videos(info["channel_id"])
        if not listing["ok"]:
            return jsonify(ok=False, message=listing["error"])
        # Auto-label the industry from the channel name + video titles (best-effort; "" if no AI).
        industry, _label_err = _watcher_autolabel(info["title"], [v.get("title", "") for v in listing["videos"]])
        entry = workspace.add_watcher_channel(client, {
            "url": info["url"], "title": info["title"], "channel_id": info["channel_id"],
            "platform": "youtube", "industry": industry, "kind": "creator",
            "video_count": len(listing["videos"]),
        })
        workspace.write_watcher_videos(client, entry["id"],
                                       [_watcher_video_entry(v) for v in listing["videos"]])
        _audit(client, "added watcher channel", "%s (%d videos)" % (info["title"], len(listing["videos"])))
        return jsonify(ok=True, channel=entry["id"])

    if op == "add_video":
        info = watcher.resolve_video(request.form.get("url", ""))
        if not info["ok"]:
            return jsonify(ok=False, message=info["error"])
        channel = workspace.ensure_loose_channel(client)
        videos = workspace.read_watcher_videos(client, channel["id"])
        entry = next((v for v in videos if v.get("id") == info["video_id"]), None)
        already = entry is not None
        if entry is None:
            entry = {"id": info["video_id"], "title": info["title"], "url": info["url"],
                     "transcript": "", "language": "", "generated": False,
                     "error": "", "permanent": False, "fetched_at": "",
                     "published_text": "", "published": ""}
            videos.insert(0, entry)
        # Fetch inline. A rate-limit is a session condition (not a fact about the video): leave the
        # entry pending so Fetch missing / Safe pull can finish it later, exactly like a channel.
        result = watcher.fetch_transcript(info["video_id"])
        blocked = (not result["ok"]) and ("rate-limiting" in result["error"])
        if not blocked:
            entry["fetched_at"] = workspace.now_iso()
            if result["ok"]:
                entry.update(transcript=result["transcript"], language=result["language"],
                             generated=result["generated"], error="", permanent=False)
            else:
                entry.update(error=result["error"], permanent=bool(result["permanent"]))
        workspace.write_watcher_videos(client, channel["id"], videos)
        _watcher_counts(client, channel["id"], videos)
        _audit(client, "scraped single video", info["title"][:80])
        return jsonify(ok=True, channel=channel["id"], video_id=info["video_id"],
                       title=entry.get("title", ""), url=entry.get("url", ""),
                       transcript=entry.get("transcript", ""),
                       words=len((entry.get("transcript") or "").split()),
                       language=entry.get("language", ""), error=entry.get("error", ""),
                       blocked=blocked, already=already)

    channel_id = request.form.get("channel_id", "").strip()
    if workspace.find_watcher_channel(ws, channel_id) is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")

    if op == "fetch":
        videos = workspace.read_watcher_videos(client, channel_id)
        if _bool_field("retry"):
            for v in videos:
                if v.get("error") and not v.get("permanent"):
                    v["error"] = ""
        fetched, blocked = watcher.fetch_transcripts_batch(videos)
        workspace.write_watcher_videos(client, channel_id, videos)
        pending = _watcher_counts(client, channel_id, videos)
        if fetched:
            _audit(client, "fetched watcher transcripts", "%d videos" % fetched)
        return jsonify(ok=True, fetched=fetched, blocked=blocked, remaining=pending,
                       total=len(videos), done=len(videos) - pending)

    if op == "safe_pull":
        workspace.queue_watcher_safe_pull(client, channel_id)
        ch = workspace.find_watcher_channel(ws, channel_id)
        _audit(client, "queued watcher safe pull", ch.get("title", ""))
        return jsonify(ok=True)

    if op == "refresh":
        ch = workspace.find_watcher_channel(ws, channel_id)
        listing = watcher.list_videos(ch.get("channel_id", ""))
        if not listing["ok"]:
            return jsonify(ok=False, message=listing["error"])
        videos = workspace.read_watcher_videos(client, channel_id)
        by_id = {v.get("id"): v for v in videos}
        new = []
        for lv in listing["videos"]:
            known = by_id.get(lv["id"])
            if known is None:
                new.append(_watcher_video_entry(lv))
            elif lv.get("published_text"):
                # Backfill/refresh the upload age on videos we already hold (older archives
                # predate date capture, and relative ages drift as time passes).
                known["published_text"] = lv["published_text"]
                known["published"] = watcher.published_estimate(lv["published_text"])
        videos = new + videos  # the listing is newest-first; keep the archive that way too
        workspace.write_watcher_videos(client, channel_id, videos)
        _watcher_counts(client, channel_id, videos)
        _audit(client, "refreshed watcher channel", "%s: %d new" % (ch.get("title", ""), len(new)))
        return jsonify(ok=True, new=len(new))

    if op == "meta":
        # Hand-edit the classification: industry (free text) and/or kind (creator|competitor).
        fields = {}
        if "industry" in request.form:
            fields["industry"] = request.form.get("industry", "").strip()[:40]
        if "kind" in request.form:
            kind = request.form.get("kind", "").strip()
            if kind not in _WATCHER_KINDS:
                return jsonify(ok=False, message="Unknown type.")
            fields["kind"] = kind
        if not fields:
            return jsonify(ok=False, message="Nothing to change.")
        workspace.update_watcher_channel(client, channel_id, fields)
        _audit(client, "edited watcher channel labels",
               ", ".join("%s=%s" % (k, v) for k, v in fields.items()))
        return jsonify(ok=True)

    if op == "label":
        # Re-run the AI industry label from the stored video titles.
        ch = workspace.find_watcher_channel(ws, channel_id)
        titles = [v.get("title", "") for v in workspace.read_watcher_videos(client, channel_id)]
        industry, err = _watcher_autolabel(ch.get("title", ""), titles)
        if not industry:
            return jsonify(ok=False, message="Could not auto-label: %s." % (err or "no label"))
        workspace.update_watcher_channel(client, channel_id, {"industry": industry})
        _audit(client, "auto-labeled watcher channel", "%s -> %s" % (ch.get("title", ""), industry))
        return jsonify(ok=True, industry=industry)

    if op == "delete":
        workspace.clear_watcher_safe_pull(client, channel_id)
        removed = workspace.delete_watcher_channel(client, channel_id)
        if removed:
            _audit(client, "removed watcher channel", removed.get("title", ""))
        return jsonify(ok=True)

    return Response('{"error":"bad_op"}', status=400, mimetype="application/json")


# --- Assistant (team-only tab: RAG chat over EVERY workspace source) -----------------------------
def _assistant_archives(ws, client):
    """The Watcher registry entries paired with their loaded video lists (the index's input)."""
    return [(ch, workspace.read_watcher_videos(client, ch.get("id", "")))
            for ch in workspace.watcher_channels(ws)]


def _assistant_mail(ws, client, cap=150):
    """The Mail thread archives loaded for the Assistant index (newest first, capped)."""
    out = []
    for t in workspace.mail_threads(ws)[:cap]:
        full = workspace.read_mail_thread(client, t.get("id", ""))
        if full:
            out.append(full)
    return out


def _assistant_embedder():
    """The chunk embedder for the semantic leg (RETRIEVAL_DOCUMENT), or None when embeddings are off.
    Bound to the intel brain's Vertex plumbing (same SA token, GCP-billed, no API key)."""
    if not intel_ai.embeddings_configured():
        return None
    return lambda texts: intel_ai.embed_texts(texts, task_type="RETRIEVAL_DOCUMENT")


def _assistant_query_embedder(index):
    """The per-question embedder (RETRIEVAL_QUERY) -- only when embeddings are wired AND this index
    actually carries a semantic leg; else None so ask() stays pure BM25."""
    import assistant_ai
    if not (intel_ai.embeddings_configured() and assistant_ai.has_embeddings(index)):
        return None
    return lambda q: intel_ai.embed_query(q)


def _assistant_reranker():
    """The cross-encoder rerank seam (Vertex Ranking API), or None when reranking isn't enabled."""
    if not intel_ai.reranking_configured():
        return None
    return lambda query, records, top_n: intel_ai.rerank(query, records, top_n=top_n)


def _assistant_index(ws, client, force=False):
    """The client's knowledge index, rebuilt lazily whenever any source changed (or on `force`).

    When embeddings are configured the semantic leg is attached at build time (and back-filled onto
    an existing, still-current index the first time embeddings are enabled -- no need to rechunk)."""
    import assistant_ai
    archives = _assistant_archives(ws, client)
    fp = assistant_ai.fingerprint(ws, archives)
    want_emb = intel_ai.embeddings_configured()
    index = None if force else workspace.read_assistant_index(client)
    if index is not None and index.get("fingerprint") == fp:
        if not want_emb or assistant_ai.has_embeddings(index):
            return index
        # Data unchanged, embeddings newly enabled: add the semantic leg without rebuilding chunks.
        assistant_ai.embed_index(index, _assistant_embedder())
        workspace.write_assistant_index(client, index)
        return index
    chunks = assistant_ai.build_chunks(ws, archives,
                                       dash_data=assistant_ai.read_client_dash_data(client),
                                       mail_threads=_assistant_mail(ws, client))
    index = assistant_ai.build_index(chunks, fp=fp)
    if want_emb:
        assistant_ai.embed_index(index, _assistant_embedder())
    workspace.write_assistant_index(client, index)
    return index


def _assistant_model(ws):
    """The model the Assistant should use: its own saved choice when set AND available, else the
    intel brain's model, else the deploy default ("" when no provider is configured)."""
    own = ((ws.get("assistant") or {}).get("model") or "").strip()
    if own and intel_ai.model_available(own):
        return own
    return ((ws.get("intel_ai") or {}).get("model") or "").strip()


def _assistant_depth(ws):
    """The Assistant's saved answer depth, validated ('standard' when unset/unknown)."""
    import assistant_ai
    d = ((ws.get("assistant") or {}).get("depth") or "").strip()
    return d if d in assistant_ai.DEPTHS else assistant_ai.DEFAULT_DEPTH


@app.route("/w/<client>/admin/assistant", methods=["POST"])
def atrium_admin_assistant(client):
    """The Atrium Assistant (team-only). `op` is:

    * ask      -- answer `question` from the workspace knowledge index (question + optional
                  `history` JSON of recent turns + optional `date_from`/`date_to` ISO dates that
                  scope DATED sources like transcripts/intel to a range). The index rebuilds
                  lazily when stale, so answers always reflect the latest fetched data. The
                  response carries the call's token `usage` + the updated all-time `totals`
                  (the cost pill).
    * settings -- save whichever Assistant settings the form carries: `model` ("" = automatic)
                  and/or `depth` (quick|standard|deep -- the detail control; deep also thinks).
    * reindex  -- force-rebuild the index now; returns its size (the pane's Rebuild button).
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    import assistant_ai
    op = request.form.get("op", "ask")

    if op == "reindex":
        index = _assistant_index(ws, client, force=True)
        embedded = index.get("emb_count", 0)
        _audit(client, "rebuilt assistant index",
               "%d chunks%s" % (len(index.get("chunks") or []),
                                (", %d embedded" % embedded) if embedded else ""))
        return jsonify(ok=True, chunks=len(index.get("chunks") or []), embedded=embedded,
                       built_at=index.get("built_at", ""))

    if op == "settings":
        # Save whichever settings the form carries (the two dropdowns each post just their own
        # field, so an absent field must never reset the other setting).
        saved = {}
        if "model" in request.form:
            model = request.form.get("model", "").strip()
            if model and intel_ai.model_meta(model) is None:
                return jsonify(ok=False, message="Unknown model.")
            workspace.set_assistant_model(client, model)
            meta = intel_ai.model_meta(model)
            _audit(client, "set assistant model", (meta or {}).get("label") or "automatic")
            saved["model"] = model
        if "depth" in request.form:
            depth = request.form.get("depth", "").strip() or assistant_ai.DEFAULT_DEPTH
            if depth not in assistant_ai.DEPTHS:
                return jsonify(ok=False, message="Unknown depth.")
            workspace.set_assistant_depth(client, depth)
            _audit(client, "set assistant depth", depth)
            saved["depth"] = depth
        if not saved:
            return jsonify(ok=False, message="Nothing to save.")
        return jsonify(ok=True, **saved)

    if op == "ask":
        question = request.form.get("question", "").strip()
        if not question:
            return jsonify(ok=False, message="Ask a question first.")
        try:
            history = json.loads(request.form.get("history", "") or "[]")
        except ValueError:
            history = []
        index = _assistant_index(ws, client)
        usage = {}
        answer, sources, err = assistant_ai.ask(
            ws.get("display_name") or client, index, question,
            history=history if isinstance(history, list) else [],
            date_from=request.form.get("date_from", "").strip(),
            date_to=request.form.get("date_to", "").strip(),
            model=_assistant_model(ws), usage_out=usage, depth=_assistant_depth(ws),
            query_embedder=_assistant_query_embedder(index), reranker=_assistant_reranker())
        if err:
            return jsonify(ok=False, message=err, sources=sources)
        # Spend accounting: price this call and fold it into the client's all-time tally so the
        # cost pill (this answer + session + all-time) has real numbers. Best-effort -- a tally
        # failure must never eat an answer the model already produced.
        mid = usage.get("model", "")
        cost = intel_ai.cost_of(mid, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        usage["cost_usd"] = cost
        try:
            totals = workspace.add_assistant_usage(
                client, mid, usage.get("input_tokens", 0), usage.get("output_tokens", 0), cost)
        except Exception:
            totals = workspace.assistant_usage(ws)
        _audit(client, "asked the assistant", question[:80])
        return jsonify(ok=True, answer=answer, sources=sources, usage=usage, totals=totals)

    return Response('{"error":"bad_op"}', status=400, mimetype="application/json")


@app.route("/w/<client>/watcher/video/<channel_id>/<video_id>", methods=["GET"])
def atrium_watcher_video(client, channel_id, video_id):
    """One video's FULL transcript as JSON (team-only) -- the click-to-expand behind the cards."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    for v in workspace.read_watcher_videos(client, channel_id):
        if v.get("id") == video_id:
            return jsonify(ok=True, title=v.get("title", ""), url=v.get("url", ""),
                           transcript=v.get("transcript", ""), error=v.get("error", ""),
                           language=v.get("language", ""), fetched_at=v.get("fetched_at", ""),
                           published_text=v.get("published_text", ""))
    return Response('{"error":"not_found"}', status=404, mimetype="application/json")


# --- Mail (team-only tab: client email archive + AI digest; see mailroom.py) ----------------------
def _mail_view(ws, client):
    """The Mail pane's render model: contacts + digest + the display-ready thread index rows
    (subjects/participants/summaries only -- bodies are fetched on click)."""
    state = workspace.mail_state(ws)
    rows = []
    for t in state.get("threads") or []:
        rows.append({
            "id": t.get("id", ""),
            "subject": t.get("subject") or "(no subject)",
            "participants": ", ".join(t.get("participants") or []),
            "last_date": t.get("last_date", ""),
            "message_count": t.get("message_count", 0),
            "mailbox": t.get("mailbox", ""),
            "summary": t.get("summary", ""),
            "tier": t.get("tier") or "client",   # older entries (pre-triage) default to client
            "awaiting_reply": bool(t.get("awaiting_reply")),
            "avg_response_hours": t.get("avg_response_hours"),
        })
    # The responsiveness strip: computed numbers (mailroom.thread_stats per thread), the same
    # facts the digest's REPLIES section and the Assistant's snapshot are judged against.
    # Noise (newsletters/bulk) is excluded from the "awaiting reply" pressure -- you don't owe a
    # newsletter a reply -- so the strip reflects real correspondence only.
    real = [r for r in rows if r["tier"] != "noise"]
    waiting = [r for r in real if r["awaiting_reply"]]
    hours = [r["avg_response_hours"] for r in real
             if isinstance(r.get("avg_response_hours"), (int, float))]
    tier_counts = {tier: sum(1 for r in rows if r["tier"] == tier)
                   for tier in ("security", "client", "operations", "noise")}
    stats = {
        "total": len(real),
        "awaiting": len(waiting),
        "oldest_awaiting": min([r["last_date"] for r in waiting if r["last_date"]] or [""]),
        "avg_response_hours": (round(sum(hours) / len(hours), 1) if hours else None),
        "security": tier_counts["security"],
        "tier_counts": tier_counts,
    }
    return {
        "contacts": ", ".join(state.get("contacts") or []),
        "threads": rows,
        "stats": stats,
        "digest": state.get("digest") or {},
        "last_sync": state.get("last_sync", ""),
        "last_error": state.get("last_error", ""),
        "backlog": int(state.get("backlog") or 0),
        # Which connected mailboxes actually feed THIS client (assigned to it, or shared).
        "mailboxes": [m for m in workspace.public_mailboxes()
                      if m.get("client") == client or not m.get("client")],
        # Drives the pane's setup hints ("connect a mailbox first" vs "add contacts").
        "mailbox_count": len(workspace.mail_mailboxes()),
    }


def _communications_view(ws, client, is_admin, mailview=None):
    """The unified Communications timeline: every conversation as one date-sorted list of cards.

    A CLIENT sees only audience=="client" cards -- team cards are filtered out HERE, server-side, so
    they never reach the client HTML (the same no-leak posture as _progress_tasks). An ADMIN also
    gets the non-client email threads (operations/security/noise) projected from Mail as team-only
    cards; the client-tier email recaps already arrive via the mirror in the list below."""
    items = []
    for it in workspace.communications_list(ws):
        aud = "team" if (it.get("audience") == "team") else "client"
        if not is_admin and aud != "client":
            continue
        items.append({
            "id": it.get("id", ""),
            "channel": it.get("channel") or "note",
            "audience": aud,
            "title": it.get("title") or "",
            "summary": it.get("summary") or "",
            "date": it.get("date") or "",
            "people": it.get("people") or "",
            "thread_key": it.get("thread_key") or "",
            "origin": it.get("origin") or "manual",
            "client_visible": (aud == "client"),
            "readonly": False,
        })
    if is_admin and mailview:
        for r in (mailview.get("threads") or []):
            if (r.get("tier") or "client") == "client":
                continue  # client-tier recap is already mirrored into the list above
            items.append({
                "id": "mailrow_" + (r.get("id") or ""),
                "channel": "email",
                "audience": "team",
                "title": r.get("subject") or "(no subject)",
                "summary": r.get("summary") or "",
                "date": r.get("last_date") or "",
                "people": r.get("participants") or "",
                "thread_key": r.get("id") or "",
                "origin": "mail",
                "tier": r.get("tier") or "",
                "client_visible": False,
                "readonly": True,  # projected -- manage it from the email thread, not as a card
            })
    items.sort(key=lambda it: it.get("date") or "", reverse=True)
    return items


@app.route("/w/<client>/admin/mail", methods=["POST"])
def atrium_admin_mail(client):
    """The Mail tab's team actions. `op` is one of:

    * contacts -- save the client's contact emails/domains (the textarea; drives the Gmail query).
    * sync     -- pull + archive + summarize now across every connected mailbox (the same
                  mailroom.sync_client the hourly job runs; per-run caps keep it inside the
                  request window, and message-id dedup makes re-runs cheap).
    * digest   -- rebuild the rolling AI digest from the stored thread summaries only (no pull).
    * delete   -- remove one archived thread (index entry + its archive object).
    """
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    ws = workspace.load_workspace(client)
    if ws is None:
        return Response('{"error":"no_workspace"}', status=404, mimetype="application/json")
    import mailroom  # lazy: only the mail routes need it
    op = request.form.get("op", "")

    if op == "contacts":
        contacts = workspace.set_mail_contacts(client, request.form.get("contacts", ""))
        usable = mailroom.clean_contacts(contacts)
        _audit(client, "saved mail contacts", "%d contact(s)" % len(contacts))
        return jsonify(ok=True, contacts=contacts,
                       message=("" if usable or not contacts else
                                "None of those look like an email address or domain."))

    if op == "sync":
        # Save the contacts textarea first if it came along, so "Sync now" works even when the
        # operator typed addresses but didn't click Save first (a common footgun).
        if "contacts" in request.form:
            workspace.set_mail_contacts(client, request.form.get("contacts", ""))
            ws = workspace.load_workspace(client) or ws
        # Archive-only by default: pull + store every matching email FAST, no AI calls. The hourly
        # job (summarize=True) writes the summaries/digest/Communications recaps later. Pass
        # summarize=1 to force a summarizing sync from the button.
        summarize = _bool_field("summarize")
        result = mailroom.sync_client(client, ws=ws, summarize=summarize)
        _audit(client, "synced client mail",
               "%d new message(s), %d thread(s)" % (result["new_messages"], result["new_threads"]))
        fresh = workspace.load_workspace(client) or ws
        state = workspace.mail_state(fresh)
        return jsonify(ok=result["ok"], new_messages=result["new_messages"],
                       new_threads=result["new_threads"], summarized=result["summarized"],
                       backlog=result.get("backlog", 0),
                       errors=result["errors"], last_sync=state.get("last_sync", ""),
                       digest=(state.get("digest") or {}).get("body", ""))

    if op == "digest":
        # Refresh briefing = the ON-DEMAND AI pass: summarize the archived threads that still lack a
        # summary (Sync-now is archive-only), then rebuild the digest. Capped per click, so a big
        # backlog is drained over a few clicks (the JS reports how many remain).
        result = mailroom.refresh_briefing(client, ws=ws)
        if not result["ok"]:
            return jsonify(ok=False, message=result.get("error") or "Nothing to summarize yet.")
        _audit(client, "refreshed mail briefing", "%d summarized" % result["summarized"])
        return jsonify(ok=True, digest=result["digest"], summarized=result["summarized"],
                       remaining=result["remaining"])

    if op == "delete":
        key = request.form.get("thread_id", "").strip()
        removed = workspace.delete_mail_thread(client, key)
        if removed is None:
            return Response('{"error":"not_found"}', status=404, mimetype="application/json")
        try:
            # Also retract the thread's mirrored recap from the client's Communications feed
            # (a later sync may re-pull + re-mirror it if it still matches the contacts).
            workspace.delete_communication(client, "mail_" + key)
        except Exception:
            pass
        _audit(client, "deleted mail thread", removed.get("subject", ""))
        return jsonify(ok=True)

    return Response('{"error":"bad_op"}', status=400, mimetype="application/json")


@app.route("/w/<client>/mail/thread/<thread_id>", methods=["GET"])
def atrium_mail_thread(client, thread_id):
    """One thread's FULL message archive as JSON (team-only) -- the click-to-read behind the rows."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    t = workspace.read_mail_thread(client, thread_id)
    if t is None:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    return jsonify(ok=True, subject=t.get("subject", ""),
                   participants=t.get("participants") or [], mailbox=t.get("mailbox", ""),
                   summary=t.get("summary", ""), last_date=t.get("last_date", ""),
                   messages=t.get("messages") or [])


@app.route("/admin/mail", methods=["POST"])
def admin_mail():
    """Manage the agency's connected mailboxes (console -> Mailboxes). THE super admin only --
    these entries carry live credentials. `op`:

    * add    -- connect (or re-save) a mailbox: `email` + `kind` (dwd = our Workspace domain via
                delegation, nothing stored; imap = any other Google account via its app password).
                Form post; redirects back to the console pane with a flash.
    * delete -- disconnect a mailbox by `mailbox_id` (form post, same redirect).
    * test   -- prove a mailbox connects right now (fetch/JSON; the pane's Test button).
    """
    if not authed():
        return redirect(url_for("login", next="/admin/atrium"))
    if not is_root_admin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    import mailroom  # lazy
    op = request.form.get("op", "")

    def back(msg, err=False):
        from urllib.parse import quote  # lazy, matching the rest of the app
        return redirect("/admin/atrium?section=mailboxes&msg=%s%s"
                        % (quote(msg), "&err=1" if err else ""))

    if op == "add":
        email = request.form.get("email", "")
        kind = request.form.get("kind", "")
        if kind == "dwd" and not mailroom.dwd_configured():
            return back("Workspace delegation isn't set up yet -- run enable_atrium_mail.ps1 "
                        "and redeploy first, or connect it as IMAP.", err=True)
        assign = request.form.get("client", "").strip()   # "" = shared; else a client key
        try:
            entry = workspace.add_mailbox(email, kind,
                                          app_password=request.form.get("app_password", ""),
                                          client=assign)
        except ValueError as exc:
            return back(str(exc), err=True)
        scope = ("assigned to %s (whole inbox)" % assign) if assign else "shared (routed by contacts)"
        _audit("", "connected mailbox", "%s (%s, %s)" % (entry["email"], kind, scope))
        return back("Mailbox %s connected — %s. Use Test to prove the connection." % (entry["email"], scope))

    if op == "delete":
        removed = workspace.delete_mailbox(request.form.get("mailbox_id", ""))
        if removed is None:
            return back("That mailbox is already gone.", err=True)
        _audit("", "disconnected mailbox", removed.get("email", ""))
        return back("Mailbox %s disconnected." % removed.get("email", ""))

    if op == "test":
        mb = workspace.find_mailbox(request.form.get("mailbox_id", ""))
        if mb is None:
            return Response('{"error":"not_found"}', status=404, mimetype="application/json")
        ok, message = mailroom.test_mailbox(mb)
        return jsonify(ok=ok, message=message)

    return Response('{"error":"bad_op"}', status=400, mimetype="application/json")


@app.route("/w/<client>/admin/calendar", methods=["POST"])
def atrium_admin_calendar(client):
    """Add, delete, or mark-done a calendar event in place. `op` is 'add', 'delete', or 'status'."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    op = request.form.get("op")
    if op == "delete":
        try:
            index = int(request.form.get("index", "-1"))
        except (TypeError, ValueError):
            index = -1
        removed = workspace.delete_calendar_event(client, index)
        if removed is not None:
            label = removed.get("label") or "event"
            # Personal events are restorable; content-linked ones are owned by their piece (restore
            # the content instead), so only trash personal events -- but audit either way.
            if not removed.get("content_id"):
                _trash(client, "calendar", label, removed)
            _audit(client, "deleted calendar event", label)
        return jsonify(ok=True)
    if op == "status":
        # Mark an event done (or clear it). Empty status clears; anything else normalizes to "done".
        try:
            index = int(request.form.get("index", "-1"))
        except (TypeError, ValueError):
            index = -1
        raw = request.form.get("status", "").strip()
        status = "done" if raw else ""
        event = workspace.set_calendar_status(client, index, status)
        if event is None:
            return Response('{"error":"not_found"}', status=404, mimetype="application/json")
        _audit(client, "marked calendar event " + ("done" if status else "not done"),
               event.get("label") or "event")
        return jsonify(ok=True, event=event)
    if op == "edit":
        # Edit an existing event's date / label / kind (paid|organic|due|milestone) in place.
        try:
            index = int(request.form.get("index", "-1"))
        except (TypeError, ValueError):
            index = -1
        date = request.form.get("date", "").strip()
        if not date:
            return Response('{"error":"date_required"}', status=400, mimetype="application/json")
        kind = request.form.get("kind", "milestone").strip() or "milestone"
        event = workspace.edit_calendar_event(client, index, date,
                                              request.form.get("label", "").strip(), kind)
        if event is None:
            return Response('{"error":"not_found"}', status=404, mimetype="application/json")
        _audit(client, "edited calendar event", event.get("label") or "event")
        return jsonify(ok=True, event=event)
    date = request.form.get("date", "").strip()
    if not date:
        return Response('{"error":"date_required"}', status=400, mimetype="application/json")
    kind = request.form.get("kind", "milestone").strip() or "milestone"
    event = workspace.add_calendar_event(client, date, request.form.get("label", "").strip(), kind)
    _audit(client, "added calendar event", event.get("label") or "event")
    return jsonify(ok=True, event=event)


@app.route("/w/<client>/admin/reply", methods=["POST"])
def atrium_admin_reply_inline(client):
    """Reply to a conversation as the AGORA team from inside the workspace; notifies the client."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    conv_id = request.form.get("conversation_id", "").strip()
    body = request.form.get("body", "").strip()
    if not body:
        return Response('{"error":"empty"}', status=400, mimetype="application/json")
    sender_name = request.form.get("sender_name", "").strip() or "AGORA"
    new_status = "resolved" if _bool_field("resolve") else "awaiting_reply"
    try:
        conv, message = workspace.add_message(client, conv_id, "agora", sender_name, body,
                                              set_status=new_status)
    except KeyError:
        return Response('{"error":"not_found"}', status=404, mimetype="application/json")
    notify.team_replied(client, workspace.load_workspace(client), conv, sender_name)
    _audit(client, "replied", conv.get("subject") or "")
    return jsonify(ok=True, message=message, status=conv.get("status"))


# --- Task tracker: the internal delivery board (spec: TASK_TRACKER_INTEGRATION.md) ---------------
# Cross-client team board in the operator console (Delivery -> Task Board) + the client-facing
# read-only Progress tab in each workspace. One data source: ws["tasks"] per client (workspace.py).
# Departments come from the accounts roster; stage KEYS are canonical (workspace.TASK_STAGES) and
# the client sees friendlier labels (In progress / In review / Live / Completed) on their tab.
TASK_DEPARTMENTS = (("acquisition", "Acquisition"), ("lifecycle", "Lifecycle"),
                    ("data", "Data Analyst"), ("development", "Development"),
                    ("bidbrain", "Bidbrain"))
# The label is AUTO-derived from the department (the team = the discipline) -- there is no manual
# label picker on the form. Acquisition -> Paid Media, Lifecycle -> Organic, the rest -> Website.
TASK_DEPT_LABEL = {"acquisition": "Paid Media", "lifecycle": "Organic",
                   "data": "Website", "development": "Website", "bidbrain": "Website"}
# Within-column ordering: urgent work floats to the top, ties broken by the sooner launch date.
TASK_PRIORITY_RANK = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3}
# Discipline tint class (shared color language across the admin board AND the client Progress tab).
# Only the COLOR crosses to the client -- the department name itself stays internal.
TASK_DISC_CLASS = {"Paid Media": "lb-paid", "Organic": "lb-organic", "Website": "lb-web"}


def _discipline(labels):
    """(label, tint_class) for the FIRST recognized discipline, or ('', '') if none.

    Text and color come from the SAME label, so a legacy multi-label task can't show one
    discipline's word in another's color."""
    for lbl in labels or []:
        if lbl in TASK_DISC_CLASS:
            return lbl, TASK_DISC_CLASS[lbl]
    return "", ""
TASK_STAGE_META = (("in_process", "In Process"), ("for_launch", "For Launch"),
                   ("launched", "Launched"), ("closed", "Closed"))
TASK_STAGE_LABELS = dict(TASK_STAGE_META)
# The client-facing relabels (spec §3.1) -- keys never change, labels are friendlier.
TASK_CLIENT_STAGES = (("in_process", "In progress"), ("for_launch", "In review"),
                      ("launched", "Live"), ("closed", "Completed"))


# Canonical Atrium delivery team -- these people must ALWAYS be assignable on the board, on EVERY
# deploy, even before their login account is fully provisioned (active admin). Real accounts take
# precedence (matched by name OR email, case-insensitive) so their actual login id is used; anyone
# here without a matching live account still appears so work can be assigned to them. The emails are
# the ids an assignment stores -- keep them equal to each person's real login email so the two line
# up once the account exists. EDIT THIS LIST as the team changes.
ATRIUM_TEAM = (
    {"name": "Charles", "email": "charles@100.digital"},
    {"name": "Christian", "email": "christian@agoradatadriven.com"},
    {"name": "Ehjay", "email": "ehjay@agoradatadriven.com"},
    {"name": "Ian", "email": "ian@100.digital"},
    {"name": "Jerome", "email": "jerome@agoradatadriven.com"},
    {"name": "John", "email": "john@bidbrain.com"},
    {"name": "Justine", "email": "justine@agoradatadriven.com"},
    {"name": "Lance", "email": "lance@agoradatadriven.com"},
    {"name": "Nico", "email": "nico@agoradatadriven.com"},
    {"name": "Paulo", "email": "paulo@agoradatadriven.com"},
    {"name": "Samuel", "email": "samuel@agoradatadriven.com"},
    {"name": "Zhen", "email": "zhen@100.digital"},
)


def _team_roster():
    """The assignable team as [{id: email, name}] (spec §3.3), sorted by name.

    Two sources, merged so a person is never dropped:
      1. LIVE admin/superadmin accounts (`active` OR `pending` -- an invited-but-not-yet-activated
         teammate is still a real person you may assign work to; dropping them silently is what
         caused the "not all names show up" report). Client-role accounts are never included.
      2. The canonical ATRIUM_TEAM -- guarantees every delivery-team member is assignable on EVERY
         deploy even if their account isn't provisioned yet. A live account (matched by name OR
         email, case-insensitive) always wins, so its real login id is used; anyone in the list
         without a live account is added with the id above.

    Lead / support / sub-task owners reference these ids (emails)."""
    out, seen_ids, seen_names = [], set(), set()
    for a in store.list_accounts():
        if a.get("status") in ("active", "pending") and a.get("role") in ("admin", "superadmin"):
            email = a.get("email") or ""
            name = a.get("name") or (email.split("@")[0].split(".")[0].title() if email else "?")
            out.append({"id": email, "name": name})
            if email:
                seen_ids.add(email.lower())
            seen_names.add(name.strip().lower())
    for m in ATRIUM_TEAM:
        nm, em = m["name"].strip().lower(), m["email"].strip().lower()
        if nm in seen_names or em in seen_ids:
            continue  # a live account already covers this person -- keep the real id
        out.append({"id": m["email"], "name": m["name"]})
        seen_names.add(nm)
        seen_ids.add(em)
    out.sort(key=lambda p: p["name"].lower())
    return out


def _person_name(names, pid):
    """A person's display name from the roster map, degrading to the email's local part."""
    if not pid:
        return ""
    return names.get(pid) or pid.split("@")[0].split(".")[0].title()


# Avatar colors (the prototype look): each person gets a STABLE color derived from their id, so
# the same face is the same color on every card, chip, and owner select across the whole board.
_PERSON_COLORS = ("#4fa84a", "#0d9488", "#2f6ecc", "#5a54dd", "#c2410c",
                  "#0f766e", "#6a6aea", "#3f8b3b", "#b45309", "#14857a")


def _person_color(pid):
    """A deterministic avatar color for a person id (email); grey for unassigned."""
    if not pid:
        return "#8b8f8c"
    return _PERSON_COLORS[sum(ord(ch) for ch in pid) % len(_PERSON_COLORS)]


def _task_next_stage(stage):
    """The (key, label) after `stage` on the board, or (None, None) at the end."""
    keys = [k for k, _l in TASK_STAGE_META]
    try:
        i = keys.index(stage)
    except ValueError:
        return None, None
    if i >= len(keys) - 1:
        return None, None
    return TASK_STAGE_META[i + 1]


def _task_board(clients_tasks, roster):
    """Shape every client's tasks into the console's stage columns (render-ready dicts).

    `clients_tasks` is [(client_key, client_name, task_dict), ...] from the admin_atrium walk --
    the workspaces are already loaded there, so this adds NO extra bucket reads."""
    names = {p["id"]: p["name"] for p in roster}
    today = datetime.date.today()
    today_iso = today.isoformat()
    soon_iso = (today + datetime.timedelta(days=2)).isoformat()
    dept_label = dict(TASK_DEPARTMENTS)
    cols = [{"key": k, "name": lbl, "tasks": []} for k, lbl in TASK_STAGE_META]
    by_key = {c["key"]: c for c in cols}
    for ckey, cname, t in clients_tasks:
        t = workspace.normalize_task(dict(t))
        # Two-level breakdown: shape each main task (owner name + its own done count) and keep a
        # flattened view for the totals/progress bar.
        mains = []
        for m in t.get("maintasks") or []:
            msubs = [dict(s, owner_name=_person_name(names, s.get("assignee_id") or ""),
                          owner_color=_person_color(s.get("assignee_id") or ""))
                     for s in (m.get("subs") or [])]
            mdone = len([s for s in msubs if s.get("done")])
            mains.append(dict(m, subs=msubs,
                              owner_name=_person_name(names, m.get("assignee_id") or ""),
                              owner_color=_person_color(m.get("assignee_id") or ""),
                              subs_done=mdone, subs_total=len(msubs),
                              all_done=bool(msubs) and mdone == len(msubs)))
        subs = [s for m in mains for s in m["subs"]]
        done = len([s for s in subs if s.get("done")])
        due = t.get("due_date") or ""
        due_cls = ""
        if due and t.get("stage") != "closed":
            due_cls = "over" if due < today_iso else ("soon" if due <= soon_iso else "")
        lead = t.get("lead_id") or ""
        support = [s for s in (t.get("support_ids") or []) if s]
        nxt_key, nxt_label = _task_next_stage(t.get("stage") or "in_process")
        view = dict(t)
        view.update({
            "client_key": ckey, "client_name": cname,
            "maintasks": mains,
            "department_label": dept_label.get(t.get("department", ""), t.get("department", "") or "—"),
            "lead_name": _person_name(names, lead),
            "lead_color": _person_color(lead),
            "support_people": [{"id": s, "name": _person_name(names, s),
                                "color": _person_color(s)} for s in support],
            "subs_done": done, "subs_total": len(subs),
            "subs_unassigned": len([s for s in subs if not s.get("done") and not s.get("assignee_id")]),
            "open_changes": len(workspace.task_open_changes(t)),
            "due_cls": due_cls,
            "service_charge_label": _money_label(t.get("service_charge")),
            "disc_class": _discipline(t.get("labels"))[1] or "lb-other",
            "on_hold": bool(t.get("on_hold")), "hold_reason": t.get("hold_reason") or "",
            # For the person filter: one space-joined haystack of everyone on the task.
            "people": " ".join([lead] + support).strip(),
            "comment_count": len(t.get("comments") or []),
            "all_subs_done": bool(subs) and done == len(subs),
            "next_stage_key": nxt_key or "", "next_stage_label": nxt_label or "",
        })
        col = by_key.get(t.get("stage") or "in_process") or cols[0]
        col["tasks"].append(view)
    for col in cols:
        # Urgent on top; within a priority, ACTIVE work before on-hold; then sooner launch, then age.
        col["tasks"].sort(key=lambda v: (TASK_PRIORITY_RANK.get(v.get("priority"), 9),
                                         1 if v.get("on_hold") else 0,
                                         v.get("due_date") or "9999-99-99",
                                         v.get("created_at") or ""))
    return cols


def _money_label(val):
    """A display money string ("$4,200") from a stored service charge; "" for empty/zero/junk."""
    s = str(val if val is not None else "").strip().replace("$", "").replace(",", "")
    if not s:
        return ""
    try:
        n = float(s)
    except ValueError:
        return ""
    if not n:
        return ""
    if n == int(n):
        return "${:,}".format(int(n))
    return "${:,.2f}".format(n)


def _task_reply(msg, err=False, **extra):
    """Answer a task-route POST: console forms redirect back to the Tasks pane with a flash;
    anything else (fetch/API) gets JSON. Keeps the console on plain <form> posts (no JS state).

    Overlay forms also carry `back_task` ("<client>:<task_id>") + `back_tab` — the redirect
    forwards them as ?task=&tab= so the console script REOPENS the same detail overlay on the
    same tab after the reload (instead of dumping the user back on the bare board)."""
    if request.form.get("redirect") == "console":
        back = request.form.get("back_task", "").strip()
        tab = request.form.get("back_tab", "").strip()
        return redirect(url_for("admin_atrium", msg=msg, section="tasks",
                                err=(1 if err else None),
                                task=(back or None), tab=(tab or None)))
    if err:
        return jsonify(ok=False, error=msg)
    return jsonify(ok=True, **extra)


def _task_fields_from_form():
    """The editable task fields from the posted form (shared by op=add and op=edit).

    The form's one name field is LABELED "Campaign" (tasks are mostly campaigns) but stores as
    `title` -- the canonical display name everywhere. Labels are AUTO-derived from the department
    (TASK_DEPT_LABEL), so there is no label input to read. The support picker only renders on the
    Edit form; op=add posts none and the new task starts with no support people."""
    dept = request.form.get("department", "").strip()
    lbl = TASK_DEPT_LABEL.get(dept)
    fields = {
        "title": request.form.get("title", "").strip(),
        "department": dept,
        "lead_id": request.form.get("lead_id", "").strip(),
        "priority": request.form.get("priority", "Medium"),
        "labels": [lbl] if lbl else [],
        "client_facing": _bool_field("client_facing"),
        "client_note": request.form.get("client_note", "").strip(),
        "deliverable_url": request.form.get("deliverable_url", "").strip(),
        "internal_notes": request.form.get("internal_notes", "").strip(),
    }
    # Dates + charge are patched ONLY when the form actually carried them, so a partial/programmatic
    # POST can't silently wipe a launch date or charge (the real add/edit forms always include them).
    if "start_date" in request.form:
        fields["start_date"] = request.form.get("start_date", "").strip()
    if "due_date" in request.form:
        fields["due_date"] = request.form.get("due_date", "").strip()
    if "service_charge" in request.form:
        fields["service_charge"] = request.form.get("service_charge", "").strip().replace("$", "").replace(",", "")
    # Support people are assigned AFTER the service exists (Edit form / detail overlay) -- only
    # patch them when the form actually carried the field, so op=add never clears anything.
    if "has_support" in request.form:
        fields["support_ids"] = [s for s in request.form.getlist("support_ids") if s]
    return fields


@app.route("/w/<client>/admin/task", methods=["POST"])
def atrium_admin_task(client):
    """Create or edit a task on a client's board (op=add|edit; TEAM only)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    op = request.form.get("op", "add").strip()
    fields = _task_fields_from_form()
    actor = current_user() or ""
    if op == "edit":
        task_id = request.form.get("task_id", "").strip()
        try:
            task = workspace.update_task(client, task_id, fields, actor=actor)
        except KeyError:
            return _task_reply("That task no longer exists.", err=True)
        _audit(client, "edited task", task.get("title", ""))
        return _task_reply("Service updated.", task_id=task["id"])
    if not fields["title"]:
        return _task_reply("A service needs a campaign name.", err=True)
    # New services always start In Process; they're moved along the board from there.
    fields["stage"] = "in_process"
    try:
        task = workspace.add_task(client, fields, actor=actor)
    except KeyError:
        return _task_reply("No workspace exists for that client yet.", err=True)
    _audit(client, "added task", task["title"])
    return _task_reply("Service added.", task_id=task["id"])


@app.route("/w/<client>/admin/task/hold", methods=["POST"])
def atrium_admin_task_hold(client):
    """Put a service on hold or resume it (TEAM only). `on_hold` is a plain boolean; the reason is
    internal. A client-facing held task shows the client a plain 'Paused' (reason never crosses)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    task_id = request.form.get("task_id", "").strip()
    held = _bool_field("on_hold")
    reason = request.form.get("hold_reason", "").strip()
    try:
        task = workspace.set_task_hold(client, task_id, held, reason, actor=current_user() or "")
    except KeyError:
        return _task_reply("That task no longer exists.", err=True)
    _audit(client, "put task on hold" if held else "resumed task", task.get("title", ""))
    return _task_reply("Put on hold." if held else "Resumed.", on_hold=task["on_hold"])


@app.route("/w/<client>/admin/task/move", methods=["POST"])
def atrium_admin_task_move(client):
    """Move a task to another stage (TEAM only; the close-guard message passes through verbatim)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    task_id = request.form.get("task_id", "").strip()
    stage = request.form.get("stage", "").strip()
    try:
        task = workspace.move_task_stage(client, task_id, stage, actor=current_user() or "")
    except KeyError:
        return _task_reply("That task (or stage) no longer exists.", err=True)
    except ValueError as exc:
        return _task_reply(str(exc), err=True)
    label = TASK_STAGE_LABELS.get(stage, stage)
    _audit(client, "moved task to %s" % label, task.get("title", ""))
    return _task_reply("Moved to %s." % label, stage=task["stage"])


@app.route("/w/<client>/admin/task/delete", methods=["POST"])
def atrium_admin_task_delete(client):
    """Soft-delete a task -> the console Bin (restorable for 30 days; TEAM only)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    task_id = request.form.get("task_id", "").strip()
    try:
        removed = workspace.delete_task(client, task_id)
    except KeyError:
        return _task_reply("That task no longer exists.", err=True)
    _trash(client, "task", removed.get("title") or task_id, removed)
    _audit(client, "deleted task", removed.get("title", ""))
    return _task_reply("Task moved to the Bin (restorable for %d days)." % audit.TRASH_TTL_DAYS)


@app.route("/w/<client>/admin/task/subtask", methods=["POST"])
def atrium_admin_task_subtask(client):
    """Sub-task ops on a task (op=add|toggle|assign|delete; TEAM only)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    op = request.form.get("op", "").strip()
    task_id = request.form.get("task_id", "").strip()
    subtask_id = request.form.get("subtask_id", "").strip()
    try:
        if op == "add":
            text = request.form.get("text", "").strip()
            if not text:
                return _task_reply("A sub-task needs a description.", err=True)
            workspace.add_subtask(client, task_id, text,
                                  request.form.get("assignee_id", "").strip(),
                                  maintask_id=request.form.get("maintask_id", "").strip())
            msg = "Sub-task added."
        elif op == "toggle":
            workspace.set_subtask_done(client, task_id, subtask_id, _bool_field("done"))
            msg = "Sub-task updated."
        elif op == "assign":
            workspace.set_subtask_owner(client, task_id, subtask_id,
                                        request.form.get("assignee_id", "").strip())
            msg = "Sub-task owner updated."
        elif op == "delete":
            workspace.delete_subtask(client, task_id, subtask_id)
            msg = "Sub-task removed."
        else:
            return _task_reply("Unknown sub-task action.", err=True)
    except KeyError:
        return _task_reply("That task or sub-task no longer exists.", err=True)
    return _task_reply(msg)


@app.route("/w/<client>/admin/task/maintask", methods=["POST"])
def atrium_admin_task_maintask(client):
    """Main-task ops on a service (op=add|assign|delete; TEAM only).

    A main task is a named group of sub-tasks with its own owner -- the two-level work
    breakdown's parent row. Deleting one removes its sub-tasks with it."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    op = request.form.get("op", "").strip()
    task_id = request.form.get("task_id", "").strip()
    maintask_id = request.form.get("maintask_id", "").strip()
    try:
        if op == "add":
            text = request.form.get("text", "").strip()
            if not text:
                return _task_reply("A main task needs a name.", err=True)
            workspace.add_maintask(client, task_id, text,
                                   request.form.get("assignee_id", "").strip())
            msg = "Main task added."
        elif op == "assign":
            workspace.set_maintask_owner(client, task_id, maintask_id,
                                         request.form.get("assignee_id", "").strip())
            msg = "Main-task owner updated."
        elif op == "rename":
            text = request.form.get("text", "").strip()
            if not text:
                return _task_reply("A main task needs a name.", err=True)
            workspace.rename_maintask(client, task_id, maintask_id, text)
            msg = "Main task renamed."
        elif op == "delete":
            workspace.delete_maintask(client, task_id, maintask_id)
            msg = "Main task removed."
        else:
            return _task_reply("Unknown main-task action.", err=True)
    except KeyError:
        return _task_reply("That task or main task no longer exists.", err=True)
    return _task_reply(msg)


@app.route("/w/<client>/admin/task/comment", methods=["POST"])
def atrium_admin_task_comment(client):
    """Team comment on a task, or resolve a client change request (op=add|resolve; TEAM only)."""
    gate = _atrium_admin_json_gate(client)
    if gate:
        return gate
    op = request.form.get("op", "add").strip()
    task_id = request.form.get("task_id", "").strip()
    if op == "resolve":
        comment_id = request.form.get("comment_id", "").strip()
        try:
            task, _comment, open_left = workspace.resolve_task_comment(client, task_id, comment_id)
        except KeyError:
            return _task_reply("That task or comment no longer exists.", err=True)
        if task.get("client_facing"):
            notify.team_task_resolved(client, workspace.load_workspace(client), task)
        _audit(client, "resolved task change request", task.get("title", ""))
        return _task_reply("Change request resolved.", open_changes=open_left)
    body = request.form.get("body", "").strip()
    if not body:
        return _task_reply("Write a comment first.", err=True)
    sender_name = _admin_sender_name(current_user())
    try:
        task, comment = workspace.add_task_comment(client, task_id, "agora", sender_name, body)
    except KeyError:
        return _task_reply("That task no longer exists.", err=True)
    # A comment on a client-facing task reaches the client's Progress tab -> notify opted-in users.
    if task.get("client_facing"):
        notify.team_task_commented(client, workspace.load_workspace(client), task, body, sender_name)
    _audit(client, "commented on task", task.get("title", ""))
    return _task_reply("Comment posted.", comment=comment)


@app.route("/admin/atrium/tasks/export", methods=["GET"])
def admin_atrium_tasks_export():
    """Download the WHOLE Task Board (every client's tasks) as one JSON backup (super-admin only).

    Read-only: gathers `ws["tasks"]` from each client's workspace. Pairs with the Import route below
    (a restore); it's the server-side equivalent of the prototype's Export button."""
    if not authed():
        return redirect(url_for("login", next="/admin/atrium"))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    payload = {"version": 1, "exported_at": workspace.now_iso(), "clients": {}}
    for c in store.list_clients():
        key = c.get("key")
        if key == "template":
            continue
        ws = workspace.load_workspace(key)
        tasks = (ws or {}).get("tasks") or []
        if tasks:
            payload["clients"][key] = {"name": c.get("name") or key, "tasks": tasks}
    body = json.dumps(payload, indent=2)
    fname = "agora-task-board-%s.json" % datetime.date.today().isoformat()
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=%s" % fname})


@app.route("/admin/atrium/tasks/import", methods=["POST"])
def admin_atrium_tasks_import():
    """Restore tasks from an exported JSON backup (super-admin only). Non-destructive: upserts BY ID
    into each client that still exists (existing tasks updated, new ones added; nothing deleted)."""
    if not authed():
        return redirect(url_for("login", next="/admin/atrium"))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return _atrium_redirect_list("Choose a JSON backup to import.", section="tasks", err=True)
    try:
        data = json.loads(upload.read().decode("utf-8"))
        clients = (data or {}).get("clients") or {}
        if not isinstance(clients, dict):
            raise ValueError("bad shape")
    except (ValueError, UnicodeDecodeError):
        return _atrium_redirect_list("That file isn't a valid task-board backup.", section="tasks", err=True)
    known = {c.get("key") for c in store.list_clients()}
    added = updated = skipped = 0
    for key, block in clients.items():
        if key not in known or workspace.load_workspace(key) is None:
            skipped += 1
            continue
        tasks = (block or {}).get("tasks") if isinstance(block, dict) else block
        a, u = workspace.upsert_tasks(key, tasks or [])
        added += a
        updated += u
    _audit("", "imported task board", "added %d, updated %d" % (added, updated))
    note = "Imported: %d added, %d updated." % (added, updated)
    if skipped:
        note += " %d client(s) in the file no longer exist — skipped." % skipped
    return _atrium_redirect_list(note, section="tasks")


# --- Atrium team management (super-admin operator console) --------------------------------------
# The console is the LANDING PAGE only: a card grid to open / add / delete clients. There is no
# per-client management page anymore -- the team edits each workspace IN PLACE via /w/<c>/admin/*
# (see the in-workspace admin editing section above), and a card opens that workspace directly.
def _atrium_redirect_list(msg, section=None, err=False):
    """Redirect back to the console with a flash, optionally reopening a section pane.

    `err=True` marks the flash as an ERROR so the console styles it red (a rejected action must not
    look like a green success -- that was the "it silently didn't add" confusion)."""
    return redirect(url_for("admin_atrium", msg=msg, section=section, err=(1 if err else None)))


def _account_view(account, name_by_key):
    """Shape one account for the console table: resolve its client keys to a readable label."""
    keys = account.get("clients") or []
    if "*" in keys:
        label = "All clients"
    elif keys:
        label = ", ".join(name_by_key.get(k, k) for k in keys)
    else:
        label = "-"
    return {
        "email": account.get("email"),
        "name": account.get("name") or account.get("email"),
        "role": account.get("role") or "client",
        "status": account.get("status") or "active",
        "client_label": label,
        # The client's own workspace key, so the console can link straight to that client's site.
        "client_key": next((k for k in keys if k != "*"), ""),
        "created_at": account.get("created_at", ""),
    }


@app.route("/admin/atrium", methods=["GET"])
def admin_atrium():
    """The operator console: clients, access requests, accounts, account creation, and the profile."""
    if not authed():
        return redirect(url_for("login", next="/admin/atrium"))
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    clients = []
    name_by_key = {}
    board_tasks = []   # (client_key, client_name, task) for the Delivery -> Task Board pane
    for c in store.list_clients():
        key, name = c.get("key"), c.get("name")
        name_by_key[key] = name or key
        if key == "template":
            continue  # the worked-example pattern, not a real client -- never list it in the console
        ws = workspace.load_workspace(key)
        # Logo shown on the card: the client's own logo from its workspace, else an initials monogram
        # (so a brand-new / unseeded client still renders something on-brand rather than an empty box).
        logo = (ws.get("brand", {}).get("client_logo") if ws else None) or brand.monogram(name or key)
        # Attention chip: content pieces still awaiting the client's approval (mirrors the
        # status=="awaiting" filter in atrium.html). The workspace JSON is already in hand for the
        # logo above, so this is a free walk -- no extra bucket read per client.
        awaiting = 0
        for camp in (ws or {}).get("campaigns", []):
            for piece in camp.get("content", []) or []:
                if piece.get("status") == "awaiting":
                    awaiting += 1
        # Task Board: same already-loaded workspace, so collecting its tasks is another free walk.
        for t in (ws or {}).get("tasks") or []:
            board_tasks.append((key, name or key, t))
        clients.append({"key": key, "name": name,
                        "has_workspace": ws is not None, "logo": logo, "awaiting": awaiting})
    # Clients needing attention come first; the sort is stable so ties keep their registry order.
    clients.sort(key=lambda c: -c["awaiting"])
    task_roster = _team_roster()
    task_cols = _task_board(board_tasks, task_roster)

    all_accounts = store.list_accounts()
    pending = [a for a in all_accounts if a.get("status") == "pending"]
    active = [_account_view(a, name_by_key) for a in all_accounts if a.get("status") == "active"]
    client_accounts = [a for a in active if a["role"] == "client"]
    admin_accounts = [a for a in active if a["role"] in ("admin", "superadmin")]

    me = current_account()
    profile = {
        "email": current_user(),
        "name": (me or {}).get("name") or current_user(),
        "role": (me or {}).get("role") or ("superadmin" if is_root_admin() else "admin"),
        "has_account": me is not None,
    }

    def _short_when(ts):
        # "2026-06-25T09:12:30Z" -> "Jun 25, 09:12" (UTC); falls back to the raw ts on any surprise.
        try:
            dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            return dt.strftime("%b %d, %H:%M")
        except Exception:
            return ts or ""

    activity = []
    for a in audit.recent_activity(limit=300):
        a2 = dict(a)
        a2["client_name"] = name_by_key.get(a.get("client", ""), a.get("client", "")) or "-"
        a2["when"] = _short_when(a.get("ts", ""))
        activity.append(a2)
    trash = []
    for t in audit.trash_list():
        t2 = dict(t)
        t2["client_name"] = name_by_key.get(t.get("client", ""), t.get("client", "")) or "-"
        t2["when"] = _short_when(t.get("ts", ""))
        trash.append(t2)

    return render_template(
        "admin_atrium.html", clients=clients, pending=pending,
        awaiting_total=sum(c["awaiting"] for c in clients),
        client_accounts=client_accounts, admin_accounts=admin_accounts,
        profile=profile, is_root_admin=is_root_admin(), super_admin_email=SUPER_ADMIN_EMAIL,
        activity=activity, trash=trash, trash_ttl_days=audit.TRASH_TTL_DAYS,
        initial_section=(request.args.get("section") or "clients"),
        user=current_user(), workspace_name=WORKSPACE_NAME,
        skill_mastery_url=SKILL_MASTERY_URL, website_editor_url=WEBSITE_EDITOR_URL,
        sentinel_url=SENTINEL_URL,
        # Delivery -> Task Board: stage columns of every client's tasks + the pickers' vocabularies.
        task_cols=task_cols, task_roster=task_roster,
        task_departments=TASK_DEPARTMENTS,
        # Nav badge = every task on the board (matches the prototype's total count).
        task_open_total=sum(len(col["tasks"]) for col in task_cols),
        task_trash_count=len([t for t in trash if t.get("kind") == "task"]),
        # Mailboxes pane: the connected-mailbox list (passwords stripped) + whether Workspace
        # delegation is wired on this deploy (drives the pane's setup hints).
        mailboxes=workspace.public_mailboxes(),
        # Client options for the mailbox "assign to a client" dropdown (real clients only, not the
        # worked-example template); name_by_key already excludes nothing, so filter template here.
        mail_client_options=[{"key": c["key"], "name": c["name"] or c["key"]} for c in clients],
        mail_dwd=bool(os.environ.get("MAIL_DWD_SA", "").strip()),
        msg=request.args.get("msg"), flash_err=(request.args.get("err") == "1"), **_brand_ctx())


def _valid_client_key(k):
    """Strict slug for a client key: ascii lowercase a-z0-9 + hyphens, not starting with a hyphen."""
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return bool(k) and k[0] != "-" and all(ch in allowed for ch in k)


def _slugify_key(name):
    """Derive a client key from a display name: ascii lowercase, runs of other chars -> one hyphen."""
    out = []
    for ch in (name or "").lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


@app.route("/admin/atrium/sync-status", methods=["GET"])
def admin_atrium_sync_status():
    """Last-sync stamp for the console's Sync control (super-admin only)."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    st = sync_dash.read_state()
    return jsonify(ok=True, last_sync=st.get("last_sync"),
                   triggered=st.get("triggered", []), job_count=st.get("job_count"))


@app.route("/admin/atrium/sync-all", methods=["POST"])
def admin_atrium_sync_all():
    """'Sync all dashboards' — discover every <c>-export Cloud Run job and trigger a fresh Windsor
    pull for each (no scheduler; refresh is operator-driven). Returns immediately; dashboards rebuild
    over the next minute or two. New clients are picked up automatically (jobs are discovered)."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    triggered, failed, ts = sync_dash.trigger_all()
    _audit("", "synced all dashboards",
           "%d triggered%s" % (len(triggered), (", %d failed" % len(failed)) if failed else ""))
    return jsonify(ok=(not failed), triggered=triggered,
                   failed=[f["job"] for f in failed], last_sync=ts)


@app.route("/admin/atrium/new", methods=["POST"])
def admin_atrium_new():
    """Onboard a brand-new client from JUST a display name, then land on their blank workspace.

    The key auto-derives from the name and a strong portal password is auto-generated, so the only
    thing the team types is the name. Reuses onboard_client (the same starter_workspace the CLI uses)
    so the new client opens to a tidy, empty workspace -- a blank version of /w/<key>/ with no
    campaigns -- which the team then builds out in place. On success we redirect STRAIGHT to that
    workspace (super-admin can_open is "*"), matching "click create -> the empty workspace loads".
    """
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    name = request.form.get("name", "").strip()
    # Key + password are no longer asked for: derive the key from the name, auto-generate the password.
    key = (request.form.get("key", "").strip().lower() or _slugify_key(name))
    if not _valid_client_key(key):
        return redirect(url_for("admin_atrium",
                                msg="Please enter a display name we can build a client key from "
                                    "(letters and numbers)."))
    if workspace.workspace_exists(key) or store.get_client(key) is not None:
        return redirect(url_for("atrium", client=key))  # already exists -> just open it
    import onboard_client  # lazy: reuses brand_for() + starter_workspace()
    onboard_client.onboard(key, name or None)
    _audit(key, "created client", name or key)
    return redirect(url_for("atrium", client=key))


def _unique_client_key(base):
    """Derive a client key from `base` (a company name) that no existing client/workspace uses.

    Slugifies, then appends -2, -3, ... until free, so approving two sign-ups with the same company
    name never collides on a key (or silently re-points at an existing client)."""
    root = _slugify_key(base) or "client"
    key = root
    n = 2
    while store.get_client(key) is not None or workspace.workspace_exists(key):
        key = "%s-%d" % (root, n)
        n += 1
    return key


@app.route("/admin/accounts/approve", methods=["POST"])
def admin_account_approve():
    """Approve a pending sign-up: create the client + a blank workspace, then activate the account.

    After this the client can log in with the email + password they chose at sign-up (verify_portal_login
    matches the now-`active` account and returns its single client key)."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = request.form.get("email", "").strip()
    account = store.get_account(email)
    if account is None or account.get("status") != "pending":
        return _atrium_redirect_list("No pending request found for that email.", section="requests", err=True)
    company = account.get("requested_name") or account.get("name") or email.split("@")[0]
    key = _unique_client_key(company)
    import onboard_client  # lazy: reuses brand_for() + starter_workspace()
    onboard_client.onboard(key, company)            # creates the client + a blank Atrium workspace
    store.set_account_clients(email, [key])
    store.set_account_status(email, "active")
    return _atrium_redirect_list("Approved %s -- created client '%s' and activated their login." %
                                 (email, key), section="requests")


@app.route("/admin/accounts/reject", methods=["POST"])
def admin_account_reject():
    """Reject (delete) a pending sign-up request. No client is created."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = request.form.get("email", "").strip()
    removed = store.remove_account(email)
    if not removed:
        return _atrium_redirect_list("No request to reject for that email.", section="requests", err=True)
    return _atrium_redirect_list("Rejected and removed the access request for %s." % email,
                                 section="requests")


@app.route("/admin/accounts/create-client", methods=["POST"])
def admin_account_create_client():
    """Admin creates an ACTIVE client account directly (no request/approval step).

    Creates the client + a blank workspace and an active client account, so the client can log in
    immediately with the email + password set here. If no password is given, a strong one is
    generated and surfaced so the operator can read it back to the client."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    company = request.form.get("company", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "") or _gen_password()
    domain = email.split("@")[-1] if "@" in email else ""
    if not company:
        return _atrium_redirect_list("Please enter a company name.", section="create", err=True)
    if "@" not in email or "." not in domain:
        return _atrium_redirect_list("Please enter a valid email like name@company.com.",
                                     section="create", err=True)
    if store.get_account(email) is not None:
        return _atrium_redirect_list("An account already exists for %s." % email, section="create", err=True)
    key = _unique_client_key(company)
    import onboard_client  # lazy
    onboard_client.onboard(key, company)            # client + blank workspace
    store.add_account(email, password, name=company, role="client", clients=[key],
                      status="active", requested_name=company)
    _audit(key, "created client account", email)
    return _atrium_redirect_list(
        "Created client '%s'. The client signs in at /login with  %s / %s  to see their own site. "
        "Use 'Open site' below to view their workspace." % (key, email, password), section="accounts")


@app.route("/admin/accounts/create-admin", methods=["POST"])
def admin_account_create_admin():
    """THE super admin creates another ADMIN account (full client access, role 'admin')."""
    if not is_root_admin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "") or _gen_password()
    domain = email.split("@")[-1] if "@" in email else ""
    if "@" not in email or "." not in domain:
        return _atrium_redirect_list("Please enter a valid email like name@company.com.",
                                     section="create", err=True)
    if store.get_account(email) is not None:
        return _atrium_redirect_list("An account already exists for %s." % email, section="create", err=True)
    store.add_account(email, password, name=name or email.split("@")[0], role="admin",
                      clients=["*"], status="active")
    _audit("", "created admin account", email)
    return _atrium_redirect_list(
        "Created admin '%s'. Login -> %s / %s" % (name or email, email, password), section="accounts")


@app.route("/admin/accounts/grant-google", methods=["POST"])
def admin_account_grant_google():
    """Grant a Gmail (Google sign-in) access, assigned to a CLIENT or a ROLE.

    ONE action for two flows: creating a fresh passwordless Google account AND approving/activating a
    pending access request (upsert_google_account updates in place by email). `assign` is:
      * "new-client"       -> onboard a brand-new client (company from name/requested_name) + a client
                              account scoped to it.
      * "role-admin"       -> an admin account (all clients).            [super-admin only]
      * "role-superadmin"  -> another super admin (all clients).         [super-admin only]
      * "<existing key>"   -> a client account scoped to that existing client.
    The account is passwordless: the grantee signs in with Google (never the password form).
    """
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = (request.form.get("email", "") or "").strip().lower()
    assign = (request.form.get("assign", "") or "").strip()
    name = (request.form.get("name", "") or "").strip()
    domain = email.split("@")[-1] if "@" in email else ""
    if "@" not in email or "." not in domain:
        return _atrium_redirect_list("Please enter a valid Gmail like name@gmail.com.",
                                     section="create", err=True)
    # Fall back to the pending request's remembered name/company when the form left name blank.
    pending = store.get_account(email)
    display = name or (pending or {}).get("requested_name") or (pending or {}).get("name") \
        or email.split("@")[0]

    if assign in ("role-admin", "role-superadmin"):
        if not is_root_admin():
            return _atrium_redirect_list("Only the super admin can grant admin access.",
                                         section="requests", err=True)
        role = "superadmin" if assign == "role-superadmin" else "admin"
        store.upsert_google_account(email, name=display, role=role, clients=["*"], status="active")
        _audit("", "granted %s access (Google)" % role, email)
        return _atrium_redirect_list(
            "Granted %s access to %s. They sign in with Google." % (role, email), section="accounts")

    if assign == "new-client" or not assign:
        key = _unique_client_key(display)
        import onboard_client  # lazy: reuses brand_for() + starter_workspace()
        onboard_client.onboard(key, display)
        store.upsert_google_account(email, name=display, role="client", clients=[key], status="active")
        _audit(key, "granted client access (Google)", email)
        return _atrium_redirect_list(
            "Created client '%s' and granted %s access (Google sign-in)." % (key, email),
            section="accounts")

    # Otherwise `assign` is an existing client key.
    if store.get_client(assign) is None:
        return _atrium_redirect_list("Unknown client '%s'." % assign, section="requests", err=True)
    store.upsert_google_account(email, name=display, role="client", clients=[assign], status="active")
    _audit(assign, "granted client access (Google)", email)
    return _atrium_redirect_list(
        "Granted %s access to client '%s' (Google sign-in)." % (email, assign), section="accounts")


# --- Impersonation: THE super admin can 'act as' any user -----------------------------------------
@app.route("/admin/impersonate", methods=["POST"])
def admin_impersonate():
    """Let THE super admin (info@ / role superadmin) act as another user -- assume their role + client
    access, with the real identity preserved so it's reversible. Signing in as info@ therefore means
    'act as any user you want'. Only is_root_admin can START this; once acting-as, the session IS that
    user (is_root_admin becomes false), so the 'Act as' controls disappear until they stop."""
    if not is_root_admin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    target = (request.form.get("email", "") or "").strip().lower()
    acct = store.get_account(target)
    if acct is None or acct.get("status") != "active":
        return _atrium_redirect_list("No active account for %s to act as." % target,
                                     section="accounts", err=True)
    clients = acct.get("clients") or []
    granted = ["*"] if "*" in clients else list(clients)
    # Audit BEFORE we switch identity, so the action is attributed to the real super admin (not the
    # user being impersonated). Then remember who we really are (the escape hatch) + become them.
    _audit("", "started acting as", target)
    session["impersonator"] = current_user()
    _establish_session(target, granted)
    resp = redirect(_post_login_destination(granted, "/"))
    return _mint_sso_on(resp, granted, target)


@app.route("/admin/stop-impersonating", methods=["GET", "POST"])
def admin_stop_impersonating():
    """Return to THE super admin's own identity after acting as someone. Safe to hit even if the
    session got into a weird state -- if there's no real identity to restore, it just logs out."""
    real = session.pop("impersonator", None)
    if not real:
        return redirect(url_for("index"))
    was = current_user() or ""     # who we were acting as (capture before we restore ourselves)
    granted = _resolve_login_email(real)
    if not granted:
        session.clear()
        return redirect(url_for("login"))
    _establish_session(real, granted)
    _audit("", "stopped acting as", was)
    resp = redirect(url_for("admin_atrium"))
    return _mint_sso_on(resp, granted, real)


@app.route("/admin/accounts/set-password", methods=["POST"])
def admin_account_set_password():
    """Change an account's password to one the operator types (helpdesk: 'change password')."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    if len(password) < 6:
        return _atrium_redirect_list("New password must be at least 6 characters.", section="accounts", err=True)
    if not _can_manage_account(email) or not _can_manage_admin_target(email):
        return _atrium_redirect_list("You can't change that account's password.", section="accounts", err=True)
    try:
        store.set_account_password(email, password)
    except KeyError:
        return _atrium_redirect_list("No account found for %s." % email, section="accounts", err=True)
    return _atrium_redirect_list("Password changed for %s." % email, section="accounts")


@app.route("/admin/accounts/reset-password", methods=["POST"])
def admin_account_reset_password():
    """Reset an account's password to a freshly generated one and reveal it (helpdesk: 'reset')."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = request.form.get("email", "").strip()
    if not _can_manage_account(email) or not _can_manage_admin_target(email):
        return _atrium_redirect_list("You can't reset that account's password.", section="accounts", err=True)
    new_pw = _gen_password()
    try:
        store.set_account_password(email, new_pw)
    except KeyError:
        return _atrium_redirect_list("No account found for %s." % email, section="accounts", err=True)
    return _atrium_redirect_list("Reset password for %s -> %s" % (email, new_pw), section="accounts")


@app.route("/admin/accounts/delete", methods=["POST"])
def admin_account_delete():
    """Delete an account. The super admin can't be deleted, and you can't delete yourself."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    email = request.form.get("email", "").strip()
    if not _can_manage_account(email, allow_self=False):
        return _atrium_redirect_list("That account can't be deleted here.", section="accounts", err=True)
    # Deleting an admin account is a super-admin-only power.
    target = store.get_account(email)
    if target and target.get("role") in ("admin", "superadmin") and not is_root_admin():
        return _atrium_redirect_list("Only the super admin can remove admin accounts.", section="accounts", err=True)
    removed = store.remove_account(email)
    if not removed:
        return _atrium_redirect_list("No account found for %s." % email, section="accounts", err=True)
    _audit("", "deleted account", email)
    return _atrium_redirect_list("Deleted the account for %s." % email, section="accounts")


@app.route("/admin/profile/password", methods=["POST"])
def admin_profile_password():
    """The logged-in operator changes their OWN password (Profile pane)."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    password = request.form.get("password", "")
    if len(password) < 6:
        return _atrium_redirect_list("New password must be at least 6 characters.", section="profile", err=True)
    me = current_user()
    if store.get_account(me) is None:
        return _atrium_redirect_list(
            "Your session isn't backed by a stored account (env login), so there's no password to "
            "change here.", section="profile", err=True)
    store.set_account_password(me, password)
    return _atrium_redirect_list("Your password was updated.", section="profile")


def _can_manage_admin_target(email):
    """True unless the target is an ADMIN account and the caller isn't the super admin.

    Clients are manageable by any admin; admin accounts are managed only by THE super admin."""
    target = store.get_account(email)
    if target and target.get("role") in ("admin", "superadmin"):
        return is_root_admin()
    return True


def _can_manage_account(email, allow_self=True):
    """Guard for account mutations: the target must exist, must not be THE super admin, and (unless
    allow_self) must not be the logged-in user. Protects info@ from being locked out or removed."""
    email_norm = (email or "").strip().lower()
    if not email_norm:
        return False
    if email_norm == SUPER_ADMIN_EMAIL:
        return False  # the super admin is managed only from their own Profile pane
    target = store.get_account(email_norm)
    if target is None:
        return False
    if target.get("role") == "superadmin":
        return False
    if not allow_self and email_norm == (current_user() or "").strip().lower():
        return False
    return True


@app.route("/admin/atrium/<client>/logo", methods=["POST"])
def admin_atrium_logo(client):
    """Set a client's logo from an uploaded image (PNG/JPG/GIF/WEBP/SVG), shown on the workspace card
    and in their workspace header. Stored INLINE in the workspace brand (brand.client_logo) as a
    self-contained <img> data: URI -- exactly how seeded logos are embedded, so it needs no new infra
    and no separate object. We cap the upload small (logos are tiny) since the workspace JSON is
    rewritten in full on every edit."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    if store.get_client(client) is None:
        return _atrium_redirect_list("Unknown client '%s'." % client)
    upload = request.files.get("logo")
    if upload is None or not upload.filename:
        return _atrium_redirect_list("No logo file chosen.")
    mime = (upload.mimetype or "").lower()
    if mime not in _ATRIUM_IMAGE_EXT:
        return _atrium_redirect_list("Logo must be a PNG, JPG, GIF, WEBP or SVG image.")
    data = upload.read(LOGO_MAX_BYTES + 1)
    if len(data) > LOGO_MAX_BYTES:
        return _atrium_redirect_list("Logo is too large -- please use an image under %d KB."
                                     % (LOGO_MAX_BYTES // 1024))
    # Embed as a data: URI inside an <img> (NOT inlined SVG markup): an <img> never executes script,
    # so an uploaded SVG cannot inject anything when the markup is later rendered with |safe.
    import base64  # lazy: only the logo path needs it
    b64 = base64.b64encode(data).decode("ascii")
    logo_markup = '<img src="data:%s;base64,%s" alt="logo">' % (mime, b64)
    try:
        workspace.set_client_logo(client, logo_markup)
    except KeyError:
        return _atrium_redirect_list("'%s' has no workspace yet -- create one before adding a logo."
                                     % client)
    _audit(client, "uploaded client logo")
    return _atrium_redirect_list("Logo updated for '%s'." % client)


@app.route("/admin/atrium/<client>/rename", methods=["POST"])
def admin_atrium_rename(client):
    """Rename a client on the fly (display name only -- the key `<c>` and every derived resource
    stay untouched). Updates BOTH places a name lives: the registry entry (login page, console
    cards) and the workspace's display_name (the workspace header + assistant prompts)."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    entry = store.get_client(client)
    if entry is None:
        return _atrium_redirect_list("Unknown client '%s'." % client)
    name = " ".join(request.form.get("name", "").split()).strip()[:80]
    if not name:
        return _atrium_redirect_list("Enter the new name first.")
    old = entry.get("name") or client
    if name == old:
        return _atrium_redirect_list("'%s' is already the current name." % name)
    store.set_client_name(client, name)
    try:
        workspace.set_display_name(client, name)
    except KeyError:
        pass  # no workspace yet -- the registry rename is still worth keeping
    _audit(client, "renamed client", "%s -> %s" % (old, name))
    return _atrium_redirect_list("Renamed '%s' to '%s'." % (old, name))


@app.route("/admin/atrium/<client>/delete", methods=["POST"])
def admin_atrium_delete(client):
    """Delete a client: remove its registry entry (login + listing) AND its Atrium workspace object.

    Soft-delete: the registry entry and the workspace JSON are first stashed in the Trash (restorable
    by the super admin for 30 days). Gated super-admin and confirmed in the UI before the POST fires."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    # Snapshot BEFORE removal so a Trash restore can rebuild the login AND the whole workspace.
    client_entry = store.get_client(client)
    ws_snapshot = workspace.load_workspace(client)
    removed = store.remove_client(client)
    workspace.delete_workspace(client)
    if not removed:
        return _atrium_redirect_list("No client '%s' to delete." % client)
    name = (client_entry or {}).get("name") or client
    _trash(client, "client", name, client_entry or {"key": client},
           extra={"workspace": ws_snapshot})
    _audit(client, "deleted client", name)
    return _atrium_redirect_list("Deleted client '%s'. Restorable from Trash for 30 days." % client)


@app.route("/admin/atrium/restore", methods=["POST"])
def admin_atrium_restore():
    """Restore a soft-deleted item from the Trash (super-admin only).

    Re-inserts the stashed payload via the right workspace/store helper, then removes the Trash entry.
    Handles content, campaign, calendar event, task, and whole client."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    entry = audit.trash_get(request.form.get("entry_id", "").strip())
    if not entry:
        return _atrium_redirect_list("That item is no longer in the Trash.", section="trash", err=True)
    kind, client = entry.get("kind"), entry.get("client")
    payload, extra = (entry.get("payload") or {}), (entry.get("extra") or {})
    label = entry.get("label") or kind
    try:
        if kind == "content":
            workspace.insert_content(client, extra.get("campaign_id", ""), payload)
        elif kind == "campaign":
            workspace.insert_campaign(client, payload)
        elif kind == "calendar":
            workspace.insert_calendar_event(client, payload)
        elif kind == "task":
            workspace.insert_task(client, payload)
        elif kind == "client":
            store.restore_client(payload)
            if extra.get("workspace") is not None:
                workspace.save_workspace(client, extra["workspace"])
        else:
            return _atrium_redirect_list("Can't restore that item type.", section="trash", err=True)
    except KeyError:
        # A KeyError means the parent container is gone. Only CONTENT has a parent (its campaign);
        # for every other kind the workspace/client itself is missing -- so give a truthful reason.
        if kind == "content":
            reason = "Its campaign was deleted too (restore the campaign first)."
        elif kind == "client":
            reason = "Its registry entry could not be rebuilt."
        else:
            reason = "Its workspace no longer exists (restore or recreate the client first)."
        return _atrium_redirect_list(
            "Couldn't restore '%s'. %s" % (label, reason), section="trash", err=True)
    except Exception:
        return _atrium_redirect_list("Couldn't restore '%s'." % label, section="trash", err=True)
    audit.trash_remove(entry.get("id"))
    _audit(client, "restored %s" % kind, label)
    return _atrium_redirect_list("Restored '%s'." % label, section="trash")


@app.route("/admin/atrium/trash/empty", methods=["POST"])
def admin_atrium_trash_empty():
    """Permanently empty the Bin (super-admin only). IRREVERSIBLE -- nothing here can be restored
    afterwards, so the UI double-confirms before posting here."""
    if not is_superadmin():
        return Response("Forbidden", status=403, mimetype="text/plain")
    n = audit.trash_clear()
    _audit("", "emptied the bin", "%d item%s permanently deleted" % (n, "" if n == 1 else "s"))
    if n:
        return _atrium_redirect_list(
            "Emptied the Bin — %d item%s permanently deleted." % (n, "" if n == 1 else "s"),
            section="trash")
    return _atrium_redirect_list("The Bin was already empty.", section="trash")


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
