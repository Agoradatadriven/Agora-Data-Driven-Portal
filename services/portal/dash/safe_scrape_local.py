"""Safe, slow, resumable transcript scrape for EVERY watched channel of EVERY client.

Runs on Ian's machine (residential IP) — never on Cloud Run, which YouTube blocks instantly.
Prod is the checkpoint: each run downloads the live registries + archives, fetches only MISSING
transcripts (skips stored ones and permanent "no transcript exists" videos), and syncs progress
back every few transcripts — so the Watcher tab fills up live, a crash loses almost nothing,
and rerunning always resumes.

Usage:
    python safe_scrape_local.py                      # all clients with a watcher archive
    python safe_scrape_local.py honey-tribe ...      # only these clients, in this order
    python safe_scrape_local.py --queue              # serve the Watcher tab's Safe-pull queue
                                                     # (exit immediately when nothing is queued)

A PID lock in %TEMP% makes every mode single-instance, so the scheduled queue agent
(install_safe_pull_task.ps1 -> safe_pull_agent.vbs, every 5 minutes) and a manual full sweep
can never fetch on top of each other.

Politeness: 12-20s jittered pause between fetches; on a YouTube rate-limit the whole session
cools down on a 5 -> 10 -> 20 -> 40 -> 60 min ladder (reset after any success) and NEVER marks
a video failed. Honors WATCHER_PROXY_URL if set. Stop anytime; rerun anytime.

Launch detached (survives closing the session that started it):
    Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
      CommandLine = 'cmd /c ""<python>" -u "<this script>" > "%TEMP%\\watcher_safe_scrape\\run.log" 2>&1"' }
"""

import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time

DASH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DASH)
import watcher  # noqa: E402
from workspace import now_iso  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "watcher_safe_scrape")
os.makedirs(TMP, exist_ok=True)
BUCKET = "gs://agora-data-driven-platform-dash"
LOCK = os.path.join(TMP, "scrape.lock")

# The platform-dash bucket is owned by the agora-data-driven project and is NOT visible to
# ian@100.digital. gcloud's ambient active account flips between Agora tasks, so relying on it
# silently crash-loops the scheduled queue agent the moment it isn't info@ (it dies at list_clients
# on a permission error, before ever touching the queue). Pin every gcloud call to the deploy
# account instead; override with WATCHER_GCLOUD_ACCOUNT if ever needed.
GCLOUD_ACCOUNT = os.environ.get("WATCHER_GCLOUD_ACCOUNT", "info@agoradatadriven.com")

PAUSE_MIN, PAUSE_MAX = 12, 20
COOLDOWNS = [300, 600, 1200, 2400, 3600]
SYNC_EVERY = 5


def log(msg):
    print("[%s] %s" % (time.strftime("%m-%d %H:%M:%S"), msg), flush=True)


def _pid_alive(pid):
    """True when `pid` is a live process (Windows; never signals — os.kill(pid, 0) TERMINATES
    on Windows, so this goes through OpenProcess/GetExitCodeProcess instead)."""
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def acquire_lock():
    """Single-instance guard: refuse to start while another safe scrape is alive.

    The lock file holds the owner's PID; a stale file (dead PID, crash leftovers) is simply
    taken over, so no manual cleanup is ever needed."""
    try:
        with io.open(LOCK, encoding="utf-8") as fh:
            other = int(fh.read().strip() or 0)
        if other and other != os.getpid() and _pid_alive(other):
            return False
    except (OSError, ValueError):
        pass
    with open(LOCK, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def run_gcloud(args):
    acct = (" --account=%s" % GCLOUD_ACCOUNT) if GCLOUD_ACCOUNT else ""
    proc = subprocess.run("gcloud " + args + acct, shell=True, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("gcloud failed: %s\n%s" % (args, proc.stderr[-400:]))
    return proc.stdout


def list_clients():
    """Client slugs that have a watcher archive folder in the prod bucket."""
    out = run_gcloud('storage ls "%s/workspace/watcher/"' % BUCKET)
    slugs = []
    for line in out.splitlines():
        m = re.search(r"/workspace/watcher/([^/]+)/$", line.strip())
        if m:
            slugs.append(m.group(1))
    return slugs


def gcs_download_json(gs, name):
    path = os.path.join(TMP, name)
    run_gcloud('storage cp "%s" "%s"' % (gs, path))
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def gcs_upload_json(obj, gs, name):
    path = os.path.join(TMP, name)
    with open(path, "wb") as fh:
        fh.write(json.dumps(obj, indent=2, sort_keys=True).encode("utf-8"))
    run_gcloud('storage cp "%s" "%s"' % (path, gs))


def sync(client, channel, videos):
    """Upload the archive + refresh this channel's registry counts (fresh ws each time)."""
    arch_gs = "%s/workspace/watcher/%s/%s.json" % (BUCKET, client, channel["id"])
    gcs_upload_json({"videos": videos}, arch_gs, "arch_%s.json" % channel["id"])
    ws_gs = "%s/workspace/%s.json" % (BUCKET, client)
    ws = gcs_download_json(ws_gs, "ws_%s.json" % client)
    for ch in (ws.get("watcher") or {}).get("channels", []):
        if ch.get("id") == channel["id"]:
            ch["video_count"] = len(videos)
            ch["transcript_count"] = sum(1 for v in videos if v.get("transcript"))
            ch["failed_count"] = sum(1 for v in videos if v.get("error"))
            ch["last_fetch"] = now_iso()
    gcs_upload_json(ws, ws_gs, "ws_up_%s.json" % client)
    log("  SYNCED %s: %d/%d transcripts live"
        % (channel.get("title", "?"), sum(1 for v in videos if v.get("transcript")), len(videos)))


def scrape_channel(client, channel):
    arch_gs = "%s/workspace/watcher/%s/%s.json" % (BUCKET, client, channel["id"])
    videos = gcs_download_json(arch_gs, "in_%s.json" % channel["id"]).get("videos") or []
    pending = [v for v in videos if not v.get("transcript") and not v.get("error")]
    log("%s: %d videos, %d transcripts, %d pending"
        % (channel.get("title", "?"), len(videos),
           sum(1 for v in videos if v.get("transcript")), len(pending)))
    if not pending:
        return 0
    fetched_here, new_since_sync, cool_step = 0, 0, 0
    for v in videos:
        if v.get("transcript") or v.get("error"):
            continue
        while True:
            result = watcher.fetch_transcript(v["id"])
            if not result["ok"] and "rate-limiting" in result["error"]:
                wait = COOLDOWNS[min(cool_step, len(COOLDOWNS) - 1)]
                cool_step += 1
                log("  throttled -- cooling down %d min (video: %s)" % (wait // 60, v["title"][:48]))
                if new_since_sync:
                    sync(client, channel, videos)
                    new_since_sync = 0
                time.sleep(wait)
                continue
            break
        v["fetched_at"] = now_iso()
        if result["ok"]:
            cool_step = 0
            v.update(transcript=result["transcript"], language=result["language"],
                     generated=result["generated"], error="", permanent=False)
            fetched_here += 1
            new_since_sync += 1
            log("  ok    %s (%d words)" % (v["title"][:56], len(result["transcript"].split())))
        else:
            v.update(error=result["error"], permanent=bool(result["permanent"]))
            log("  skip  %s -- %s" % (v["title"][:56], result["error"]))
        if new_since_sync >= SYNC_EVERY:
            sync(client, channel, videos)
            new_since_sync = 0
        time.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))
    sync(client, channel, videos)
    return fetched_here


def clear_queue_entry(client, channel_id):
    """Drop one id from the client's safe-pull queue (fresh ws download, so counts synced by
    other writes in between are kept)."""
    ws_gs = "%s/workspace/%s.json" % (BUCKET, client)
    ws = gcs_download_json(ws_gs, "ws_%s.json" % client)
    w = ws.setdefault("watcher", {})
    w["safe_pull"] = [c for c in (w.get("safe_pull") or []) if c != channel_id]
    gcs_upload_json(ws, ws_gs, "ws_up_%s.json" % client)


def run_queue():
    """Serve the Watcher tab's Safe-pull queue: scrape ONLY queued channels, clearing each entry
    when its channel completes. A channel that errors stays queued for the next tick; a queued id
    whose channel was deleted is just cleared. Exits immediately when nothing is queued."""
    total = 0
    for client in sorted(list_clients()):
        ws = gcs_download_json("%s/workspace/%s.json" % (BUCKET, client), "ws0_%s.json" % client)
        w = ws.get("watcher") or {}
        queue = list(w.get("safe_pull") or [])
        if not queue:
            continue
        channels = {ch.get("id"): ch for ch in (w.get("channels") or [])}
        log("=== %s: %d safe pull(s) queued ===" % (client, len(queue)))
        for cid in queue:
            ch = channels.get(cid)
            if ch is not None:
                try:
                    total += scrape_channel(client, ch)
                except Exception as exc:
                    log("  ERROR on %s: %s -- leaving it queued" % (ch.get("title", "?"), exc))
                    continue
            clear_queue_entry(client, cid)
    log("QUEUE DONE -- %d new transcripts this run" % total)


def run_all(clients):
    """The full sweep: every pending video of every channel of the given (or all) clients."""
    clients = clients or sorted(list_clients())
    total = 0
    for client in clients:
        ws = gcs_download_json("%s/workspace/%s.json" % (BUCKET, client), "ws0_%s.json" % client)
        channels = (ws.get("watcher") or {}).get("channels") or []
        log("=== %s: %d channels ===" % (client, len(channels)))
        for ch in channels:
            try:
                total += scrape_channel(client, ch)
            except Exception as exc:  # one bad channel must not sink the sweep
                log("  ERROR on %s: %s -- moving on" % (ch.get("title", "?"), exc))
    log("ALL DONE -- %d new transcripts this run across %d clients" % (total, len(clients)))


def main():
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    queue_mode = "--queue" in args
    clients = [a for a in args if not a.startswith("--")]
    if not acquire_lock():
        log("another safe scrape is already running -- exiting")
        return
    proxy = os.environ.get("WATCHER_PROXY_URL", "")
    log("scrape starting (mode: %s; proxy: %s)"
        % ("queue" if queue_mode else "full", proxy or "none -- direct, polite pacing"))
    try:
        if queue_mode:
            run_queue()
        else:
            run_all(clients)
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


if __name__ == "__main__":
    main()
