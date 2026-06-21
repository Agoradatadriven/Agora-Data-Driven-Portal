"""Seed a LOCAL portal for click-through testing (no GCP, no ADC).

Run by run_local.ps1 after pointing REGISTRY_LOCAL_DIR + WORKSPACE_LOCAL_DIR at a throwaway folder.
It builds a portal you can actually log into on your laptop:

  * riverdance -- the full demo workspace (seed_workspace) + a known password, so you can see a
    single-client login drop STRAIGHT onto the company overview (/w/riverdance/overview).
  * three more clients (Honey Tribe, Melo Yelo, RHE) onboarded via onboard_client, each with a
    starter workspace + known password, to compare against.

Every client here gets a DISTINCT password on purpose: portal login matches a password to a client
(the email is only a label), so each password must be unique. These are LOCAL dev passwords only --
never used in production. Reruns are safe (every step refuses to clobber existing data).
"""

import sys

import onboard_client
import seed_workspace
import store
import workspace

# (key, display name, LOCAL dev password). Riverdance reuses the rich demo workspace; the rest get a
# clean starter workspace from onboard_client.
DEMO_KEY = "riverdance"
DEMO_NAME = "Riverdance RV Resort"
DEMO_PW = "riverdance-demo"

OTHER_CLIENTS = [
    ("honeytribe", "Honey Tribe", "honeytribe-demo"),
    ("meloyelo", "Melo Yelo", "meloyelo-demo"),
    ("rhe", "RHE", "rhe-demo"),
]


def main():
    creds = []

    # Riverdance: rich demo workspace (refuses to clobber) + registry entry + a known password.
    if not workspace.workspace_exists(DEMO_KEY):
        seed_workspace.seed()  # writes workspace/riverdance.json and registers the client
    store.add_client(DEMO_KEY, DEMO_NAME)
    store.set_client_password(DEMO_KEY, DEMO_PW)
    creds.append((DEMO_KEY, DEMO_PW))

    # The other three via the one-step onboarding flow.
    for key, name, pw in OTHER_CLIENTS:
        onboard_client.onboard(key, name, pw)
        creds.append((key, pw))

    print("\n  Local portal seeded. Log in at http://localhost:8080/login")
    print("  Use ANY email (e.g. owner@example.com) + one of these passwords:\n")
    for key, pw in creds:
        only = " <- single-client: lands straight on the overview" if key == DEMO_KEY else ""
        print("    %-12s  password: %s%s" % (key, pw, only))
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
