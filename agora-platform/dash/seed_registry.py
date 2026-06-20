"""Seed the portal registry (platform.json) once, from config.py.

Run this ONCE during portal standup to write the initial registry into the private GCS object
agora-data-driven-platform-dash/platform.json. It is idempotent in the safe direction: if the
registry already holds any agencies or clients, it REFUSES to overwrite and exits without writing,
so re-running it can never clobber data added later through the UI.

Usage (from the repo .venv, with ADC configured):
    python seed_registry.py

To force a re-seed of a genuinely empty/broken registry, delete the platform.json object first;
this script intentionally has no --force flag (overwriting live registry data is never the goal).
"""

import sys

import config
import store


def main():
    existing = store.load_registry()
    has_clients = bool(existing.get("clients"))
    has_agencies = bool(existing.get("agencies"))

    if has_clients or has_agencies:
        # Refuse to clobber: the registry already has real data (seeded before, or grown via the UI).
        print(
            "[seed_registry] platform.json already has %d agencies and %d clients -- refusing to "
            "overwrite. Delete the object first if you truly want to re-seed."
            % (len(existing.get("agencies", [])), len(existing.get("clients", [])))
        )
        return 1

    registry = config.initial_registry()
    store.save_registry(registry)
    print(
        "[seed_registry] seeded platform.json with %d agencies and %d clients."
        % (len(registry.get("agencies", [])), len(registry.get("clients", [])))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
