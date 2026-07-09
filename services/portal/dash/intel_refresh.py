"""Daily Market Intelligence refresh -- an AI brain researches the live web into every client's tab.

Runs as a Cloud Run JOB (`intel-refresh`) on a daily Cloud Scheduler tick, REUSING the platform-dash
image + runtime SA. No new service/bucket/SA: it writes the SAME `workspace/<c>.json` objects the app
already does. The research itself is done by Vertex Gemini with **live Google Search grounding** (see
intel_ai.research) -- billed to the GCP project via the runtime SA's token, no API key.

RESEARCH METHOD = GROUNDED WEB RESEARCH (see intel_ai.py). For each client, per section, the selected
Gemini model PLANS the angles that matter to THIS client, SEARCHES the whole web live (grounding),
reads real pages, and CURATES the strongest items -- each with a real source URL and a "why this
matters to the client" line. This is the same engine as Gemini chat, so the output is broad and
on-topic, not a Google-News-RSS re-rank.

  * Business Research is per-client and keyed on the client's own `intel_topics` (as SEEDS the model
    expands into real research angles). NO keywords -> the section stays empty with a clear reason.
    There is deliberately NO fallback: good, relevant research or nothing.
  * Media Buying News is universal (ad-platform updates apply to every media buyer), so it runs for
    every client regardless of keywords.
  * Grounding is a Gemini capability -- a non-Gemini model (e.g. DeepSeek) can't search the live web,
    so the run reports that and adds nothing.

Each run is ADDITIVE: `workspace.add_auto_intel` de-dupes and APPENDS new stories (hand-added/edited/
favourited entries are always preserved). Gated + graceful like feedback_ai: a logged no-op unless
INTEL_AUTO_ENABLED=1; a client with no workspace/model is logged and skipped, never fatal. Off-cloud
testable via WORKSPACE_LOCAL_DIR + REGISTRY_LOCAL_DIR; `refresh_client` takes an injectable
`ai_fetcher` (the LLM POST) and `token_fetcher` (the Vertex token) so the pipeline runs with no
network in tests.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import intel_ai
import store
import workspace

# Heading defaults that make an auto entry read like the hand-written ones.
_BUSINESS_HEADING = "Industry News"
_MEDIA_HEADING = "Platform Update"

# How much of the model's reasoning / raw output / sources we keep in the (rewritten-in-full)
# workspace JSON, so the "show reasoning" panel stays useful without bloating the object.
_TRACE_THINK_MAX = 6000
_TRACE_RAW_MAX = 4000
_TRACE_SOURCES_MAX = 40


def _enabled():
    """True iff the daily auto-refresh is switched on. Fail-closed (default OFF), like feedback_ai."""
    return os.environ.get("INTEL_AUTO_ENABLED", "") in ("1", "true", "True")


def _empty_trace():
    """A zeroed diagnostics dict (shape the panel expects) for a section that never ran."""
    return {"queries": [], "sources": [], "suggestions": "", "thinking": "", "raw": "",
            "candidate_count": 0, "added": 0, "seconds": 0}


def _research_section(client, ws, section, model, heading, count, prompt, recency,
                      capture, ai_fetcher, token_fetcher):
    """GROUNDED research for one section. Returns (entries, err, trace) -- NO workspace write.

    Runs intel_ai.research (Gemini + live Google Search) and packages the diagnostics (the searches
    the model ran, the sources it grounded on, its reasoning + raw output, and timing) into `trace`.
    Does NOT touch the workspace so the two sections can research CONCURRENTLY without racing the
    read-modify-write workspace JSON -- the caller writes the results, one section at a time."""
    t0 = time.time()
    trace = _empty_trace()
    entries, err = intel_ai.research(
        section,
        ws.get("display_name") or client,
        workspace.get_intel_topics(ws),
        prompt=prompt,
        model=model,
        limit=count,
        heading_default=heading,
        recency=recency,
        capture_thinking=capture,
        trace=trace,
        fetcher=ai_fetcher,
        token_fetcher=token_fetcher,
    )
    trace["thinking"] = (trace.get("thinking") or "")[:_TRACE_THINK_MAX]
    trace["raw"] = (trace.get("raw") or "")[:_TRACE_RAW_MAX]
    trace["sources"] = (trace.get("sources") or [])[:_TRACE_SOURCES_MAX]
    trace["candidate_count"] = len(trace["sources"])   # how many live sources it grounded on
    trace["seconds"] = round(time.time() - t0, 1)
    if entries:
        return entries, "", trace
    return None, err or "the model returned nothing", trace


def refresh_client(client, ws=None, ai_fetcher=None, token_fetcher=None, fetcher=None):
    """Research fresh intelligence into both sections for one client and ADD it to the existing lists.

    `ws` may be passed to avoid a reload; `ai_fetcher` (the LLM POST) and `token_fetcher` (the Vertex
    token) are the transport injection seams for tests. `fetcher` is accepted-but-ignored (legacy RSS
    seam). Returns zeros if the client has no workspace, no model, or a non-Gemini model (grounded web
    research needs Gemini -- there is NO fallback). Each run ADDS new, de-duped stories, so history
    accumulates over time; the article target + look-back are admin-configured (intel_ai.count_of /
    window_of)."""
    if ws is None:
        ws = workspace.load_workspace(client)
    if ws is None:
        return {"media_buying": 0, "business_research": 0, "ai": False}

    cfg = workspace.get_intel_ai(ws)
    model = cfg.get("model") or ""
    if not model:
        workspace.mark_intel_run(client, "", error="No AI model selected — pick one in AI Research Brain.")
        return {"media_buying": 0, "business_research": 0, "ai": False}
    if not intel_ai.model_available(model):
        workspace.mark_intel_run(client, "", error="%s isn't available on the server (check its API access)." % model)
        return {"media_buying": 0, "business_research": 0, "ai": False}
    if not intel_ai.model_supports_grounding(model):
        label = (intel_ai.model_meta(model) or {}).get("label", model)
        workspace.mark_intel_run(client, "", error="%s can't do live web research — pick a Gemini model." % label)
        return {"media_buying": 0, "business_research": 0, "ai": False}

    count = intel_ai.count_of(cfg)
    recency = intel_ai.window_label(intel_ai.window_of(cfg)).lower()
    capture = str(cfg.get("show_thinking") or "").strip() in ("1", "true", "True")

    # Media Buying is universal; Business Research runs only when the client has keywords (NO
    # fallback -- with none it stays empty and says why, never off-topic filler).
    topics = workspace.get_intel_topics(ws)
    specs = [("media_buying", _MEDIA_HEADING, cfg.get("media_prompt"))]
    results = {}
    if topics:
        specs.append(("business_research", _BUSINESS_HEADING, cfg.get("business_prompt")))
    else:
        t = _empty_trace()
        results["business_research"] = (
            None, "no research keywords set — add this client's industry keywords above", t)

    # RESEARCH both sections CONCURRENTLY (the slow part -- two grounded LLM calls overlap instead of
    # running back-to-back, roughly halving wall time). WRITES happen afterwards in THIS thread, one
    # section at a time (the workspace JSON is a read-modify-write; concurrent writers clobber).
    try:
        with ThreadPoolExecutor(max_workers=len(specs)) as ex:
            futures = {
                sec: ex.submit(_research_section, client, ws, sec, model, heading, count, prompt,
                               recency, capture, ai_fetcher, token_fetcher)
                for (sec, heading, prompt) in specs
            }
            for sec, fut in futures.items():
                results[sec] = fut.result()
    except Exception as exc:
        workspace.mark_intel_run(client, model, error=str(exc)[:200])
        raise

    counts, errs, traces, used_ai = {}, [], {}, False
    for sec in ("media_buying", "business_research"):
        entries, err, trace = results.get(sec, (None, "did not run", _empty_trace()))
        if entries:
            workspace.add_auto_intel(client, sec, entries)
            counts[sec] = len(entries)
            trace["added"] = len(entries)
            used_ai = True
        else:
            counts[sec] = 0
            trace["added"] = 0
            if err:
                errs.append(err)
        traces[sec] = trace

    err = "; ".join(dict.fromkeys(errs))        # surface each distinct reason a section couldn't fill
    workspace.mark_intel_run(client, model if used_ai else "", error=err, traces=traces)
    return {"media_buying": counts["media_buying"],
            "business_research": counts["business_research"], "ai": used_ai}


def refresh_all(ai_fetcher=None, token_fetcher=None):
    """Refresh every registered client (skipping the worked-example `template`). Returns a summary."""
    summary = {}
    for c in store.list_clients():
        key = c.get("key")
        if not key or key == "template":
            continue
        try:
            counts = refresh_client(key, ai_fetcher=ai_fetcher, token_fetcher=token_fetcher)
        except Exception as exc:  # one bad client must not sink the whole run
            print("[intel-refresh] %s FAILED: %s" % (key, exc), file=sys.stderr)
            continue
        summary[key] = counts
        print("[intel-refresh] %s -> media_buying=%d business_research=%d ai=%s"
              % (key, counts["media_buying"], counts["business_research"], counts["ai"]))
    return summary


def main():
    """Job entry point. No-op (logs why) unless INTEL_AUTO_ENABLED=1."""
    if not _enabled():
        print("[intel-refresh] disabled (set INTEL_AUTO_ENABLED=1 to run); nothing to do.")
        return
    brain = intel_ai.default_model() or "(no model configured -> nothing runs)"
    print("[intel-refresh] starting daily grounded research (default brain: %s)" % brain)
    summary = refresh_all()
    print("[intel-refresh] done -- %d client(s) refreshed" % len(summary))


if __name__ == "__main__":
    main()
