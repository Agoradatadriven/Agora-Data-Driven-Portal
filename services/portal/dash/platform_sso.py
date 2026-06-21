"""Portal single-sign-on cookie -- shared signer + verifier (pure stdlib, no network).

This ONE module is vendored byte-identically into:
  * services/portal/dash/  -- the portal MINTS the cookie after a successful portal login.
  * clients/client_<c>/dash/ -- each dashboard VERIFIES the cookie to trust a portal login,
    additively (the dashboard's own password ALWAYS still works).

How it works:
  The portal signs a small JSON payload with HMAC-SHA256 using the shared secret stored in Secret
  Manager as `platform-sso-key`. The cookie is scoped to `.agoradatadriven.com` (note the leading
  dot) so it is presented to every `<c>.agoradatadriven.com` dashboard. A dashboard accepts the
  cookie iff the signature verifies, it has not expired, and its own CLIENT_KEY appears in the
  payload's allowed-client list (or the payload grants "*", i.e. super-admin / all clients).

Critical deployment caveat (see tools/enable_platform_sso.ps1):
  The cookie only reaches a dashboard that is served on a `*.agoradatadriven.com` host. On a raw
  `*.run.app` host the `.agoradatadriven.com` cookie is never sent, so SSO is silently inert and
  the dashboard's own password gate is the only path in. That is fail-safe by design.

Everything here is fail-CLOSED: any malformed / unsigned / expired / wrong-audience cookie yields
False, and any unexpected error is swallowed into False so SSO can NEVER weaken the password gate.
"""

import base64
import hashlib
import hmac
import json
import os
import time

COOKIE_NAME = "ag_sso"                 # the SSO cookie name (Agora portal)
COOKIE_DOMAIN = ".agoradatadriven.com"  # leading dot -> shared across all subdomains
DEFAULT_TTL_SECONDS = 60 * 60 * 12      # 12h portal session


def _b64e(raw_bytes):
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")


def _b64d(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(secret, payload_b64):
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256)
    return _b64e(mac.digest())


def mint_sso_cookie(secret, clients, subject="", ttl_seconds=DEFAULT_TTL_SECONDS, now=None):
    """Return a signed cookie value granting access to `clients`.

    `clients` is a list of client keys the bearer may open, or ["*"] for all (super-admin).
    `subject` is an opaque identifier for the logged-in portal user (e.g. their email).
    The portal calls this; dashboards never do.
    """
    issued = int(now if now is not None else time.time())
    payload = {
        "sub": subject,
        "clients": list(clients),
        "iat": issued,
        "exp": issued + int(ttl_seconds),
    }
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return "%s.%s" % (payload_b64, _sign(secret, payload_b64))


def _verify(secret, raw, now=None):
    """Return the payload dict if `raw` is a valid, unexpired cookie, else None. Fail-closed."""
    if not secret or not raw or "." not in raw:
        return None
    try:
        payload_b64, sig = raw.split(".", 1)
        expected = _sign(secret, payload_b64)
        # Constant-time comparison -- never use `==` on a MAC.
        if not hmac.compare_digest(expected, sig):
            return None
        payload = json.loads(_b64d(payload_b64))
        current = int(now if now is not None else time.time())
        if int(payload.get("exp", 0)) < current:
            return None
        return payload
    except Exception:
        # Any parse/decode error -> reject. SSO must never raise into the auth path.
        return None


def sso_allows(request, secret=None, client_key=None):
    """True iff the inbound request carries a valid portal SSO cookie that covers this dashboard.

    Reads `SSO_SECRET` and `CLIENT_KEY` from the environment when not passed explicitly. Designed
    to be OR-ed into a dashboard's `authed()` so a portal login is trusted additively. Returns
    False on ANY problem (missing env, missing cookie, bad signature, expired, wrong audience).
    """
    try:
        secret = secret if secret is not None else os.environ.get("SSO_SECRET", "")
        client_key = client_key if client_key is not None else os.environ.get("CLIENT_KEY", "")
        if not secret or not client_key:
            return False
        raw = request.cookies.get(COOKIE_NAME)
        payload = _verify(secret, raw)
        if not payload:
            return False
        allowed = payload.get("clients") or []
        return "*" in allowed or client_key in allowed
    except Exception:
        return False
