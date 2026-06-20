"""Seed data for the portal registry (platform.json).

This module holds the INITIAL contents of the registry that `seed_registry.py` writes into the
private GCS object `agora-data-driven-platform-dash/platform.json` -- the ONE source of truth for
the portal. There is no database; the registry is a single JSON document and these dicts are its
first snapshot.

What is here at standup:
  * The sole agency, `agora` ("Agora Data Driven"). Its client list starts EMPTY -- clients are
    added later through the portal admin/super-admin UI, not hand-edited here.
  * The `template` client entry, so the portal has at least one dashboard to link to from day one.
    Its dashboard is the Cloud Run service `template-dash`, served at template.agoradatadriven.com.

Naming derivation (kept consistent with the rest of the monorepo, derived from a client key `<c>`):
  * subdomain:    <c>.agoradatadriven.com
  * dash service: <c>-dash            (Cloud Run service)
  * password secret (upstream dash):  <c>-dash-password   (resolved by store.get_client_dash_password)

These are plain dicts on purpose: `seed_registry.py` consumes them as-is and `store.py` reads/writes
the same shape over GCS. Keep them serialisable (no objects, no callables).
"""

ROOT_DOMAIN = "agoradatadriven.com"

# The sole agency. Display name is separate from the key so the UI can rename without re-keying.
# `clients` starts EMPTY: client memberships are managed in the UI after standup, not seeded here.
AGENCIES = [
    {
        "key": "agora",
        "name": "Agora Data Driven",
        "clients": [],  # filled in later via the admin UI
    },
]

# Clients (dashboards) the portal knows about. We seed only `template` so the portal links to a
# real dashboard on day one. Additional clients are added through the UI (store.add_client), which
# appends entries of exactly this shape.
CLIENTS = [
    {
        "key": "template",
        "name": "Template",
        "subdomain": "template.agoradatadriven.com",
        # The upstream Cloud Run dashboard service this client maps to. The portal reverse-proxies
        # it under /d/template/ and logs into it server-side using template-dash-password.
        "dash_service": "template-dash",
        # Portal-login material for this client lives here once set in the UI (set_client_password):
        #   "pw_hash":  pbkdf2_hmac hex digest (what verify_portal_login checks)
        #   "pw_salt":  hex salt for that hash
        #   "pw_plain": RECOVERABLE plaintext kept beside the hash so the super-admin console can
        #               reveal it (see store.py for the deliberate trade-off comment).
        # Left unset at seed time; the bootstrap/super-admin password path covers first login.
    },
]


def initial_registry():
    """Return the first full registry snapshot as a plain dict (what seed_registry.py writes).

    `version` lets future migrations detect/upgrade the on-disk shape; bump it when the schema of
    platform.json changes.
    """
    return {
        "version": 1,
        "root_domain": ROOT_DOMAIN,
        "agencies": [dict(a) for a in AGENCIES],
        "clients": [dict(c) for c in CLIENTS],
        # CRM: future top-level collections (contacts, notes, tasks, deals) grow alongside these.
    }
