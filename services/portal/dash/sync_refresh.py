"""Scheduled "sync all dashboards" -- trigger every <c>-export Cloud Run job on a timer.

Runs as a Cloud Run JOB (`sync-refresh`) on a Cloud Scheduler tick (every 6h by default), REUSING
the platform-dash image + runtime SA (mirrors mail_refresh.py / intel_refresh.py exactly). This
REPLACES the console's manual "Sync all dashboards" button: syncing is now automatic and
server-side, decoupled from the browser -- so a client refreshing the console can no longer trigger
paid Windsor/Meta API pulls (which naive sync-on-refresh would have done on every page load).

It simply calls sync_dash.trigger_all() -- the SAME code the button used -- which DISCOVERS every
Cloud Run job whose name ends in `-export`, POSTs each `:run`, then records the last-sync stamp in
the registry bucket (`sync_state.json`) so the console's "Last synced: Xh ago" line stays accurate.
New clients are picked up automatically (jobs are discovered, never hardcoded).

Gated + graceful: a logged no-op unless SYNC_AUTO_ENABLED=1; trigger_all never raises (it reports
failures in its return value). Off-cloud (no credentials) it degrades to the "no credentials" path.
Runs as the platform-dash web SA, which already holds the run.jobs.list / run.jobs.run permissions
the manual button used -- so no new IAM beyond the scheduler wiring.
"""

import os
import sys

import sync_dash


def _enabled():
    """True iff the scheduled sync is switched on. Fail-closed (default OFF), like intel/mail refresh."""
    return os.environ.get("SYNC_AUTO_ENABLED", "") in ("1", "true", "True")


def main():
    """Job entry point. No-op (logs why) unless SYNC_AUTO_ENABLED=1."""
    if not _enabled():
        print("[sync-refresh] disabled (set SYNC_AUTO_ENABLED=1 to run); nothing to do.")
        return
    print("[sync-refresh] triggering every <c>-export job ...")
    triggered, failed, ts = sync_dash.trigger_all()
    print("[sync-refresh] done @ %s -- %d triggered, %d failed" % (ts, len(triggered), len(failed)))
    if triggered:
        print("[sync-refresh] triggered: %s" % ", ".join(triggered))
    for f in failed:
        print("[sync-refresh] FAILED %s: %s" % (f.get("job"), f.get("error")), file=sys.stderr)


if __name__ == "__main__":
    main()
