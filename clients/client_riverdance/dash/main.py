"""Flask web service for the `template` client dashboard (Stage 3 of the data contract).

Serving model -- private bucket + Flask password gate, SSO additive:
  The per-client data JSON (`template.json`) lives in a PRIVATE GCS bucket
  (`agora-data-driven-template-dash`) and is NEVER public. This service renders a login,
  holds a signed session, and proxies the private object at `/data.json` ONLY to an
  authenticated session. A valid portal SSO cookie is trusted ADDITIVELY (see authed()):
  the dashboard's own password ALWAYS still works regardless of SSO.

The org forbids public Cloud Run, so this service is deployed with --no-invoker-iam-check
(never --allow-unauthenticated) and does its OWN password/SSO auth in-process.

Three-stage data contract (matched BY NAME): sql/*.sql view columns -> job/main.py `data`
dict keys -> the `data.*` keys this app's dashboard.html reads. dashboard.html is baked into
the image and read relative to __file__ so there is no filesystem dependency at runtime.
"""

import hmac
import os

from flask import (
    Flask,
    Response,
    redirect,
    render_template_string,
    request,
    session,
)
from google.cloud import storage

import platform_sso

# --- Configuration from the environment --------------------------------------------------
# SESSION_SECRET signs the Flask session cookie; it is mounted from Secret Manager
# (template-dash-session-key) at deploy time. A missing secret is a hard misconfig.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
# The dashboard's own password (mounted from template-dash-password). compare_digest below
# is constant-time, so never compare it with `==`.
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
# The PRIVATE bucket + object holding this client's exported data JSON.
GCS_BUCKET = os.environ.get("GCS_BUCKET", "agora-data-driven-riverdance-dash")
DATA_OBJECT = os.environ.get("DATA_OBJECT", "riverdance.json")

app = Flask(__name__)
app.secret_key = SESSION_SECRET

# Cookie hardening. SameSite=None + Secure is REQUIRED for the cross-subdomain portal
# flow (portal.agoradatadriven.com -> template.agoradatadriven.com): a Lax/Strict cookie
# would be dropped on the cross-site navigation. HttpOnly keeps it out of JS.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)
# Cap request bodies: the only POST is a tiny login form, so a small cap is plenty and
# rejects oversized/abusive bodies cheaply.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64 KiB

# dashboard.html is baked into the image; read it relative to THIS file so the working
# directory at runtime is irrelevant.
_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "dashboard.html"), "r", encoding="utf-8") as _fh:
    DASHBOARD_HTML = _fh.read()

# A single GCS client, reused across requests (it is thread-safe for reads).
_storage_client = storage.Client()


def authed():
    """OPEN ACCESS (no login): this dashboard is embedded via iframe inside Agora Atrium, which is
    itself gated by the portal login. Requiring a password here would just show a login screen inside
    the Atrium frame, so the standalone service serves the dashboard + /data.json to anyone with the
    URL. The URL is unguessable and only pasted into the (gated) Atrium workspace.

    NOTE: this intentionally makes the data reachable by anyone who has the exact URL. To re-gate it
    later, restore the session/SSO check below.
    """
    return True


# Self-contained login page, themed with the Agora CSS vars. No external/CDN assets.
LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agora Data Driven -- Sign in</title>
<style>
  :root {
    --ag-bg:#0b1020; --ag-surface:#141b33; --ag-ink:#eaf0ff; --ag-muted:#9aa7c7;
    --ag-accent:#5b8cff; --ag-accent-2:#27d3a2; --ag-danger:#ff5c7a; --ag-border:#26314f;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--ag-bg); color: var(--ag-ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  .card {
    background: var(--ag-surface); border: 1px solid var(--ag-border); border-radius: 14px;
    padding: 32px 28px; width: 100%; max-width: 380px;
    box-shadow: 0 18px 50px rgba(0,0,0,0.45);
  }
  .brand { font-size: 20px; font-weight: 700; letter-spacing: 0.2px; }
  .brand .dot { color: var(--ag-accent-2); }
  .sub { color: var(--ag-muted); font-size: 13px; margin: 6px 0 22px; }
  label { display: block; font-size: 13px; color: var(--ag-muted); margin-bottom: 6px; }
  input[type=password] {
    width: 100%; padding: 11px 12px; border-radius: 9px;
    border: 1px solid var(--ag-border); background: var(--ag-bg); color: var(--ag-ink);
    font-size: 15px; outline: none;
  }
  input[type=password]:focus { border-color: var(--ag-accent); }
  button {
    margin-top: 18px; width: 100%; padding: 11px 12px; border: 0; border-radius: 9px;
    background: var(--ag-accent); color: #fff; font-size: 15px; font-weight: 600; cursor: pointer;
  }
  button:hover { filter: brightness(1.06); }
  .err {
    margin: 0 0 16px; padding: 10px 12px; border-radius: 9px; font-size: 13px;
    background: rgba(255,92,122,0.12); border: 1px solid var(--ag-danger); color: var(--ag-danger);
  }
</style>
</head>
<body>
  <form class="card" method="POST" action="/login">
    <div class="brand">Agora Data Driven<span class="dot">.</span></div>
    <div class="sub">Sign in to view this dashboard.</div>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" autofocus required>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    if authed():
        # no-store: never let an intermediary/browser cache the authenticated page.
        return Response(DASHBOARD_HTML, mimetype="text/html",
                        headers={"Cache-Control": "no-store"})
    return render_template_string(LOGIN_HTML, error=None)


@app.route("/login", methods=["POST"])
def login():
    submitted = request.form.get("password", "")
    # Constant-time comparison -- never use `==` on a secret/password.
    if DASH_PASSWORD and hmac.compare_digest(submitted, DASH_PASSWORD):
        session["ok"] = True
        return redirect("/")
    return render_template_string(LOGIN_HTML, error="Incorrect password."), 401


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect("/")


@app.route("/data.json", methods=["GET"])
def data_json():
    # The auth-gated proxy of the PRIVATE data object: only an authenticated session (or
    # an additive portal SSO cookie) may read it. Unauthenticated -> 401, never the data.
    if not authed():
        return Response('{"error":"unauthorized"}', status=401, mimetype="application/json")
    blob = _storage_client.bucket(GCS_BUCKET).blob(DATA_OBJECT)
    payload = blob.download_as_bytes()
    # no-store: the data is private; never cache it anywhere.
    return Response(payload, mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


@app.route("/healthz", methods=["GET"])
def healthz():
    return Response("ok", mimetype="text/plain")


if __name__ == "__main__":
    # Local dev only; in Cloud Run gunicorn (see Dockerfile) serves main:app.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
