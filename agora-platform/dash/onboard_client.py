"""One-step client onboarding for the portal / Agora Atrium.

Standing up a new client used to mean three separate moves through two consoles: add the client on
/admin, set a portal password on /superadmin, and seed a workspace with seed_workspace.py. This does
all three in ONE call (and pulls in the per-client logo if one is present), so a new client is fully
live -- a login that works AND a workspace to land on -- after a single command.

    python onboard_client.py <key> "<Display Name>" [password]

`<key>` is the short, lowercase client key (e.g. honeytribe) used everywhere in the monorepo (see
the derivation rule in CLAUDE.md). If no password is given a strong one is generated and printed --
that is the client's PORTAL password (any email + this password logs them in; the email is just a
display label, see store.verify_portal_login). Reruns are safe: add_client and the workspace seed
both refuse to clobber existing data, and a rerun simply (re)sets the password.

Backend selection is inherited from the env, exactly like the rest of the data layer:
  * Default          -> Google Cloud Storage (needs ADC) -- real onboarding on the deployed portal.
  * REGISTRY_LOCAL_DIR + WORKSPACE_LOCAL_DIR set -> local files, for laptop testing (run_local.ps1).
"""

import secrets
import sys

import seed_workspace  # reuse brand_for(): inlines Creatives/clients/<key>.svg, else a monogram
import store
import workspace

# A clean starter workspace: valid in every field the Atrium overview reads, but empty of real data
# so a brand-new client opens to a tidy "nothing here yet" overview rather than someone else's demo.
# The team then fills it in via the in-workspace admin editing (/w/<c>/admin/*).
_STARTER_METRICS = [
    {"icon": "users", "label": "New leads", "value": "0", "trend": "", "trend_up": True},
    {"icon": "calendar", "label": "Bookings", "value": "0", "trend": "", "trend_up": True},
    {"icon": "dollar", "label": "Revenue", "value": "$0", "trend": "", "trend_up": True},
    {"icon": "home", "label": "Occupancy", "value": "0%", "trend": "", "trend_up": True},
    {"icon": "tag", "label": "Cost / lead", "value": "$0", "trend": "", "trend_up": True},
    {"icon": "trending", "label": "ROAS", "value": "0x", "trend": "", "trend_up": True},
]


def starter_workspace(key, display_name):
    """Return a valid, empty Atrium workspace dict for a brand-new client (pure -- no I/O)."""
    return {
        "version": 1,
        "client": key,
        "display_name": display_name,
        "tagline": "Client workspace",
        "brand": seed_workspace.brand_for(key, display_name),
        "metrics": [dict(m) for m in _STARTER_METRICS],
        "today": {"leads": 0, "visitors": 0, "bookings": 0},
        "split": {"paid": 0, "organic": 0},
        "series": [],
        "activity": [],
        "campaigns": [],
        "calendar": [],
        "conversations": [],
        "notify": {},
    }


def _generate_password():
    """A readable, strong portal password (URL-safe, no ambiguous run-together words)."""
    return secrets.token_urlsafe(9)


def onboard(key, display_name=None, password=None, seed=True):
    """Register a client, set its portal password, and seed a starter workspace. Returns the password.

    Idempotent in the safe direction: an existing registry entry and an existing workspace are left
    intact (never clobbered); only the password is (re)set so a rerun can recover a lost login.
    """
    name = display_name or key.title()

    # 1. Registry entry (idempotent: add_client never clobbers an existing key).
    store.add_client(key, name)

    # 2. Portal login: set a password (generated if not supplied).
    pw = password or _generate_password()
    store.set_client_password(key, pw)

    # 3. Workspace to land on -- only if absent, so we never overwrite real content on a rerun.
    if seed and not workspace.workspace_exists(key):
        workspace.save_workspace(key, starter_workspace(key, name))

    return pw


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    key = argv[0].strip().lower()
    display_name = argv[1] if len(argv) > 1 else None
    password = argv[2] if len(argv) > 2 else None
    pw = onboard(key, display_name, password)
    seeded = "seeded" if workspace.workspace_exists(key) else "no workspace"
    print("[onboard] '%s' ready -- portal password: %s  (%s)" % (key, pw, seeded))
    print("[onboard] the client logs in with ANY email + that password.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
