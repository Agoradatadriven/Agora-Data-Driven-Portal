"""Route-level smoke test for the auth foundation (Google sign-in, request-access, grant, impersonate,
legacy-redirect). Mirrors _atrium_smoketest.py: stubs google.cloud.storage, points the registry +
workspace at a temp dir, and drives Flask's test client. The Google token exchange is monkeypatched
(no network); everything else is the real app.

Run:  python _auth_smoketest.py   (exit 0 = pass)
"""

import os
import sys
import tempfile
import types

# 1. Stub google.cloud.storage BEFORE importing main.
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in smoke test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

# 2. Env: local backend + a signed session + Google OAuth configured so the button/routes are live.
_TMP = tempfile.mkdtemp(prefix="auth_smoke_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "cid.apps.googleusercontent.com"
os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "csecret"
os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "https://portal.agoradatadriven.com/auth/google/callback"

import main   # noqa: E402
import store  # noqa: E402

SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}
FAILS = []


def _check(label, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def _google_state(c):
    """Drive /auth/google/login so the session carries a fresh oauth_state, and return it."""
    r = c.get("/auth/google/login?next=/")
    assert r.status_code == 302 and "accounts.google.com" in r.headers["Location"], r.headers.get("Location")
    with c.session_transaction() as s:
        return s.get("oauth_state")


def run():
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()

    # --- Login page shows the Google button when configured ------------------------------------
    body = c.get("/login").get_data(as_text=True)
    _check("login page shows 'Sign in with Google'", "Sign in with Google" in body)
    _check("login page links the Google route", "/auth/google/login" in body)

    # --- /auth/google/login redirects to Google with a state ----------------------------------
    r = c.get("/auth/google/login?next=/")
    loc = r.headers.get("Location", "")
    _check("google login -> 302 to Google", r.status_code == 302 and loc.startswith(main.google_oauth.AUTH_ENDPOINT))
    _check("google login carries client_id + state", "client_id=cid" in loc and "state=" in loc)

    # --- Callback for an UNKNOWN email -> request-access page ----------------------------------
    main.google_oauth.exchange_code = lambda code, redirect, **k: ("stranger@gmail.com", None)
    state = _google_state(c)
    body = c.get("/auth/google/callback?state=%s&code=x" % state).get_data(as_text=True)
    _check("unknown email -> request access page", "Request access" in body and "Send request" in body)
    _check("request access page shows the email", "stranger@gmail.com" in body)

    # --- Request access files a pending request that shows in the console ----------------------
    c.post("/auth/request-access", data={"email": "stranger@gmail.com", "message": "let me in"})
    pend = store.get_account("stranger@gmail.com")
    _check("request-access created a pending account", pend is not None and pend.get("status") == "pending")
    _check("pending account is passwordless (Google)", "pw_hash" not in (pend or {}))

    # --- Callback for a KNOWN active account -> logged in --------------------------------------
    store.upsert_google_account("owner@gmail.com", name="Owner", role="client",
                                clients=["riverdance"], status="active")
    main.google_oauth.exchange_code = lambda code, redirect, **k: ("owner@gmail.com", None)
    state = _google_state(c)
    r = c.get("/auth/google/callback?state=%s&code=x" % state)
    _check("known email -> 302 (signed in)", r.status_code == 302)
    with c.session_transaction() as s:
        _check("session established for the Google user", s.get("user") == "owner@gmail.com")
        _check("session granted the account's client", s.get("clients") == ["riverdance"])

    # --- Sentinel is the source of truth: an active Sentinel user (no portal account) signs in ---
    # The portal defers to Sentinel for a verified email it doesn't already know locally, so adding
    # someone in Sentinel (People -> Add Employee) is all it takes to enable their Google login.
    _sentinel_active = {"staff@agora.ph"}
    main.sentinel_directory.is_active_user = lambda e: (e or "").strip().lower() in _sentinel_active
    main.google_oauth.exchange_code = lambda code, redirect, **k: ("staff@agora.ph", None)
    state = _google_state(c)
    r = c.get("/auth/google/callback?state=%s&code=x" % state)
    _check("active Sentinel user (no portal account) -> 302 (signed in)", r.status_code == 302)
    with c.session_transaction() as s:
        _check("session established for the Sentinel user", s.get("user") == "staff@agora.ph")
        _check("Sentinel staff granted no client dashboards ([])", s.get("clients") == [])
    _check("no portal account was created for the Sentinel user",
           store.get_account("staff@agora.ph") is None)

    # --- A user NOT active in Sentinel (and not in the portal) is still routed to request-access --
    main.google_oauth.exchange_code = lambda code, redirect, **k: ("outsider@agora.ph", None)
    state = _google_state(c)
    body = c.get("/auth/google/callback?state=%s&code=x" % state).get_data(as_text=True)
    _check("non-Sentinel, non-portal email -> request access page",
           "Request access" in body and "outsider@agora.ph" in body)
    # Restore the default (no Sentinel) for the remaining tests.
    main.sentinel_directory.is_active_user = lambda e: False

    # --- Callback with a BAD state is rejected ------------------------------------------------
    _google_state(c)  # set a real state, then send a wrong one
    r = c.get("/auth/google/callback?state=WRONG&code=x")
    _check("bad state rejected (400)", r.status_code == 400)

    # --- As super admin: legacy /admin + /superadmin redirect to the console ------------------
    with c.session_transaction() as s:
        s.clear(); s.update(SUPER)
    _check("/admin -> 302 /admin/atrium",
           c.get("/admin").headers.get("Location", "").endswith("/admin/atrium"))
    _check("/superadmin -> 302 /admin/atrium",
           c.get("/superadmin").headers.get("Location", "").endswith("/admin/atrium"))

    # --- Console renders the app suite (the "Switch app" dropdown) + the grant form ------------
    # (The old Home-hub suite cards were replaced by a Switch-app dropdown; "Skill Mastery" is now
    # reached from inside Sentinel/Academy rather than a top-level card.)
    body = c.get("/admin/atrium").get_data(as_text=True)
    _check("console renders the app suite",
           "Atrium Admin" in body and "Website Editor" in body and "Sentinel" in body
           and "Switch app" in body)
    _check("console shows Grant-Google form", "Grant Google access to a Gmail" in body)
    _check("console shows the pending request", "stranger@gmail.com" in body)

    # --- Grant a Gmail to an existing client via the console ----------------------------------
    store.add_client("acme", "Acme Co")
    r = c.post("/admin/accounts/grant-google",
               data={"email": "newperson@gmail.com", "assign": "acme", "name": "New Person"})
    _check("grant-google -> 302 back to console", r.status_code == 302)
    _check("grant created an active Google account scoped to the client",
           store.resolve_google_login("newperson@gmail.com", super_admin_email=main.SUPER_ADMIN_EMAIL) == ["acme"])

    # --- Grant activates a PENDING request in place (no duplicate) ----------------------------
    c.post("/admin/accounts/grant-google", data={"email": "stranger@gmail.com", "assign": "acme"})
    strangers = [a for a in store.list_accounts() if a.get("email") == "stranger@gmail.com"]
    _check("granting a pending request activates it in place (one row)", len(strangers) == 1)
    _check("granted request is now active", strangers and strangers[0].get("status") == "active")

    # --- Impersonation: super admin acts as the client account, banner appears, then stops ----
    r = c.post("/admin/impersonate", data={"email": "owner@gmail.com"})
    _check("impersonate -> 302", r.status_code == 302)
    with c.session_transaction() as s:
        _check("session now the target user", s.get("user") == "owner@gmail.com")
        _check("impersonator remembered", s.get("impersonator") == "info@agoradatadriven.com")
    # Any HTML page now carries the injected 'Stop acting as' banner.
    body = c.get("/").get_data(as_text=True)
    _check("impersonation banner injected on pages", "Stop acting as" in body and "Acting as" in body)
    r = c.post("/admin/stop-impersonating")
    _check("stop-impersonating -> 302", r.status_code == 302)
    with c.session_transaction() as s:
        _check("identity restored to the super admin", s.get("user") == "info@agoradatadriven.com")
        _check("impersonator cleared", s.get("impersonator") is None)

    # --- A regular client cannot impersonate --------------------------------------------------
    with c.session_transaction() as s:
        s.clear(); s.update({"ok": True, "user": "owner@gmail.com", "clients": ["riverdance"]})
    _check("non-root cannot impersonate (403)",
           c.post("/admin/impersonate", data={"email": "acme"}).status_code == 403)

    if FAILS:
        print("\n[auth-smoketest] FAIL (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\n[auth-smoketest] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(run())
