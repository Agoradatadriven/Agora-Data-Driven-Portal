"""Ask Sentinel whether a verified email is an active user.

The portal is the ONE app that runs Google OAuth, but Sentinel is where staff are added/assigned
(People -> Add Employee). This module lets the portal DEFER to Sentinel on a verified email it
doesn't already know locally: added-in-Sentinel therefore means can-sign-in-with-Google, and
deactivated-in-Sentinel means blocked -- with no copy of the user duplicated into the portal
registry (Sentinel stays the single source of truth).

Transport reuses the exact HMAC pattern the mastery engine already uses against Sentinel's
`/api/internal/*` endpoints: an HMAC-SHA256 signature over `"user-lookup:{ts}"` with the shared
secret both apps mount (Secret Manager `platform-sso-key`). No new secret, no CORS, no browser
credentials, a 5-minute replay window on Sentinel's side.

Everything here is best-effort and gated: if the secret or Sentinel URL is unset, or Sentinel is
unreachable / slow / returns non-200, `lookup_user` returns None so the caller simply falls through
to its existing behavior. A default or local deploy is unaffected; a Sentinel outage can never break
portal login.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import requests

# The shared HMAC secret (same one used to sign `ag_sso`). Read at call time so tests/deploys that
# set it after import still work.
_TIMEOUT_SECONDS = 3
_PURPOSE = "user-lookup"


def _secret() -> str:
    return (os.environ.get("SSO_SECRET", "") or "").strip()


def _api_base() -> str:
    """Base URL for Sentinel's API (no trailing slash).

    Prefers an explicit SENTINEL_API_URL; otherwise derives from SENTINEL_URL (which points at the
    login page, e.g. https://sentinel.agoradatadriven.com/login) by stripping a trailing /login.
    """
    base = (os.environ.get("SENTINEL_API_URL", "") or "").strip()
    if not base:
        base = (os.environ.get("SENTINEL_URL", "") or "").strip()
        if base.endswith("/login"):
            base = base[: -len("/login")]
    return base.rstrip("/")


def lookup_user(email):
    """Return Sentinel's record for `email` as a dict, or None.

    dict shape (on a successful call): {"found": bool, "active": bool, "name": str, "role": str}.
    None means "couldn't ask / not configured / error" -- the caller should treat it as "no answer",
    NOT as "denied". Callers key their allow decision off dict["active"] being True.
    """
    norm = (email or "").strip().lower()
    if not norm:
        return None
    secret = _secret()
    base = _api_base()
    if not secret or not base:
        return None
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{_PURPOSE}:{ts}".encode(), hashlib.sha256).hexdigest()
    try:
        resp = requests.get(
            f"{base}/api/internal/user-lookup",
            params={"email": norm},
            headers={"X-Academy-Ts": ts, "X-Academy-Sig": sig},
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def is_active_user(email) -> bool:
    """True iff Sentinel reports `email` as an active user. False on any not-found / error / outage."""
    data = lookup_user(email)
    return bool(data and data.get("found") and data.get("active"))
