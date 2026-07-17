"""Off-cloud test for Google sign-in: the pure OAuth helpers (google_oauth.py) + the passwordless
Google-account store functions (store.upsert_google_account / resolve_google_login). No Flask, no
network -- the token exchange is driven through an INJECTED fetcher, exactly like intel_feed's test.

Run:  python _google_oauth_localtest.py   (exit 0 = pass)
"""

import base64
import json
import os
import sys
import tempfile

FAILS = []


def check(label, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).rstrip(b"=").decode("ascii")


def make_id_token(payload):
    """A structurally-valid (unsigned) JWT: header.payload.sig -- we never verify the signature."""
    return "%s.%s.%s" % (_b64({"alg": "RS256", "typ": "JWT"}), _b64(payload), "sig")


def main():
    # Configure a client id/secret so is_configured() is True and aud checks have a value to match.
    os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client.apps.googleusercontent.com"
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-secret"
    os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "https://portal.agoradatadriven.com/auth/google/callback"

    import google_oauth as g

    CLIENT_ID = "test-client.apps.googleusercontent.com"
    NOW = 1_700_000_000
    FUTURE = NOW + 3600
    PAST = NOW - 10

    # --- config + auth url ---------------------------------------------------------------------
    check("is_configured true when id+secret set", g.is_configured() is True)
    check("redirect_uri honors the env override",
          g.redirect_uri() == "https://portal.agoradatadriven.com/auth/google/callback")
    url = g.auth_url("state-xyz", g.redirect_uri())
    check("auth_url carries client_id", "client_id=test-client" in url)
    check("auth_url carries state", "state=state-xyz" in url)
    check("auth_url requests openid email scope", "scope=openid" in url and "email" in url)
    check("auth_url points at Google", url.startswith(g.AUTH_ENDPOINT))

    # --- id_token decode -----------------------------------------------------------------------
    tok = make_id_token({"email": "a@b.com"})
    check("decode_id_token round-trips the payload", g.decode_id_token(tok).get("email") == "a@b.com")
    try:
        g.decode_id_token("not-a-jwt")
        check("malformed id_token raises", False)
    except ValueError:
        check("malformed id_token raises", True)

    # --- exchange_code: success path -----------------------------------------------------------
    good_payload = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "exp": FUTURE,
                    "email": "Owner@Gmail.com", "email_verified": True}

    def good_fetcher(_url, _data):
        return {"id_token": make_id_token(good_payload), "access_token": "at"}

    email, err = g.exchange_code("code123", g.redirect_uri(), fetcher=good_fetcher, now=NOW)
    check("exchange_code returns the verified email, lowercased", email == "owner@gmail.com")
    check("exchange_code success has no error", err is None)

    # --- exchange_code: rejection paths --------------------------------------------------------
    def wrong_aud(_u, _d):
        p = dict(good_payload); p["aud"] = "someone-else"
        return {"id_token": make_id_token(p)}
    e2, err2 = g.exchange_code("c", g.redirect_uri(), fetcher=wrong_aud, now=NOW)
    check("wrong audience rejected", e2 is None and err2 == "claims_invalid")

    def expired(_u, _d):
        p = dict(good_payload); p["exp"] = PAST
        return {"id_token": make_id_token(p)}
    e3, err3 = g.exchange_code("c", g.redirect_uri(), fetcher=expired, now=NOW)
    check("expired token rejected", e3 is None and err3 == "claims_invalid")

    def unverified(_u, _d):
        p = dict(good_payload); p["email_verified"] = False
        return {"id_token": make_id_token(p)}
    e4, err4 = g.exchange_code("c", g.redirect_uri(), fetcher=unverified, now=NOW)
    check("unverified email rejected", e4 is None and err4 == "email_unverified")

    def token_error(_u, _d):
        return {"error": "invalid_grant"}
    e5, err5 = g.exchange_code("c", g.redirect_uri(), fetcher=token_error, now=NOW)
    check("token endpoint error surfaced", e5 is None and err5 == "invalid_grant")

    def boom(_u, _d):
        raise RuntimeError("network down")
    e6, err6 = g.exchange_code("c", g.redirect_uri(), fetcher=boom, now=NOW)
    check("network failure -> token_exchange_failed", e6 is None and err6 == "token_exchange_failed")

    # --- store: passwordless Google accounts ---------------------------------------------------
    tmp = tempfile.mkdtemp(prefix="google_localtest_")
    os.environ["REGISTRY_LOCAL_DIR"] = tmp
    import store

    SA = "info@agoradatadriven.com"
    check("super admin resolves to '*'", store.resolve_google_login(SA, super_admin_email=SA) == ["*"])
    check("unknown email resolves to None",
          store.resolve_google_login("nobody@gmail.com", super_admin_email=SA) is None)

    # A pending request (from the request-access flow) cannot sign in yet.
    store.upsert_google_account("client1@gmail.com", name="Client One", role="client",
                                clients=[], status="pending", message="please add me")
    acct = store.get_account("client1@gmail.com")
    check("pending Google account is passwordless", acct is not None and "pw_hash" not in acct)
    check("pending Google account marked auth=google", acct.get("auth") == "google")
    check("pending request keeps the message", acct.get("message") == "please add me")
    check("pending Google account can't sign in yet",
          store.resolve_google_login("client1@gmail.com", super_admin_email=SA) is None)
    check("passwordless account can't be logged in via the password form",
          store.verify_portal_login("client1@gmail.com", "anything") == [])

    # Granting upserts IN PLACE (same email) -> active, scoped to a client.
    store.upsert_google_account("client1@gmail.com", role="client", clients=["acme"], status="active")
    check("grant activates the SAME account (no duplicate)",
          len([a for a in store.list_accounts() if a.get("email") == "client1@gmail.com"]) == 1)
    check("granted client resolves to its client key",
          store.resolve_google_login("client1@gmail.com", super_admin_email=SA) == ["acme"])

    # Grant a role: admin -> all clients.
    store.upsert_google_account("teammate@gmail.com", name="Teammate", role="admin",
                                clients=["*"], status="active")
    check("granted admin resolves to '*'",
          store.resolve_google_login("teammate@gmail.com", super_admin_email=SA) == ["*"])

    # --- sentinel_directory: graceful gating (no network, no crash) -----------------------------
    import sentinel_directory as sd

    os.environ.pop("SSO_SECRET", None)
    os.environ["SENTINEL_URL"] = "https://sentinel.agoradatadriven.com/login"
    check("no SSO_SECRET -> lookup returns None (disabled, never crashes login)",
          sd.lookup_user("anyone@agora.ph") is None)
    check("no SSO_SECRET -> is_active_user False", sd.is_active_user("anyone@agora.ph") is False)

    os.environ["SSO_SECRET"] = "test-shared-secret"
    os.environ.pop("SENTINEL_URL", None)
    os.environ.pop("SENTINEL_API_URL", None)
    check("no Sentinel URL -> lookup returns None (nothing to call)",
          sd.lookup_user("anyone@agora.ph") is None)

    # SENTINEL_URL (login page) is the fallback base; /login is stripped to reach the API root.
    os.environ["SENTINEL_URL"] = "https://sentinel.agoradatadriven.com/login"
    check("api base strips a trailing /login",
          sd._api_base() == "https://sentinel.agoradatadriven.com")
    os.environ["SENTINEL_API_URL"] = "https://sentinel.example.com/"
    check("explicit SENTINEL_API_URL wins and is normalized",
          sd._api_base() == "https://sentinel.example.com")

    # A successful call is parsed into is_active_user via an INJECTED requests.get (no network).
    class _Resp:
        status_code = 200

        def json(self):
            return {"found": True, "active": True, "name": "Staff", "role": "employee"}

    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Resp()

    _orig_get = sd.requests.get
    sd.requests.get = fake_get
    try:
        check("active Sentinel user -> is_active_user True", sd.is_active_user("Staff@Agora.ph") is True)
        check("lookup lowercases the email in the query",
              captured.get("params", {}).get("email") == "staff@agora.ph")
        check("lookup signs with the shared-secret HMAC headers",
              bool(captured.get("headers", {}).get("X-Academy-Sig"))
              and bool(captured.get("headers", {}).get("X-Academy-Ts")))
        check("lookup targets the internal endpoint on the API base",
              captured.get("url") == "https://sentinel.example.com/api/internal/user-lookup")

        # A non-200 (e.g. bad signature) degrades to "no answer", never an exception.
        class _Resp401:
            status_code = 401

            def json(self):
                return {}

        sd.requests.get = lambda *a, **k: _Resp401()
        check("non-200 -> is_active_user False (graceful)", sd.is_active_user("staff@agora.ph") is False)

        # A network error degrades to None, never breaking login.
        def boom_get(*a, **k):
            raise sd.requests.RequestException("network down")

        sd.requests.get = boom_get
        check("network error -> lookup None (login never breaks)", sd.lookup_user("staff@agora.ph") is None)
    finally:
        sd.requests.get = _orig_get

    if FAILS:
        print("\n[localtest] FAIL (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\n[google-oauth-localtest] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
