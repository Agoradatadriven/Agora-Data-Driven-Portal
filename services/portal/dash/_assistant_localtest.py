"""Off-cloud test for the Atrium Assistant (no GCS, no network, no LLM).

Covers the RAG pieces end-to-end: chunking every workspace source, the BM25 index + date-filtered
retrieval, lenient answer parsing, the ask() seam, the Flask route (lazy index rebuild + ok/error
paths), and the team-only gating.

Run: python _assistant_localtest.py        # prints PASS / FAIL, exits 0 / 1
"""

import os
import shutil
import sys
import tempfile
import types

# Stub google.cloud.storage BEFORE importing main (store/feedback construct a client at import).
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in this test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

_TMP = tempfile.mkdtemp(prefix="atrium_assistant_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"

import assistant_ai     # noqa: E402
import seed_workspace   # noqa: E402
import workspace        # noqa: E402
import main             # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}
CLIENT_LOGIN = {"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]}


def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def _fake_archives():
    ch = {"id": "wch_test01", "title": "Carson Reed", "transcript_count": 1,
          "last_fetch": "2026-07-12T00:00:00Z"}
    videos = [
        {"id": "v1", "title": "Pricing AI retainers", "url": "https://youtu.be/v1",
         "published": "2026-07-01",
         "transcript": ("charge monthly retainers for AI receptionists " * 80).strip()},
        {"id": "v2", "title": "Old cold outreach video", "url": "https://youtu.be/v2",
         "published": "2024-01-15",
         "transcript": "cold outreach emails work best when personalized to the prospect."},
    ]
    return [(ch, videos)]


def run():
    seed_workspace.seed(register_client=False)
    ws = workspace.load_workspace(CLIENT)
    archives = _fake_archives()

    # --- Chunking: every source lands, long transcripts split ------------------------------------
    chunks = assistant_ai.build_chunks(ws, archives,
                                       dash_data={"kpis": {"leads": 42}, "daily": [{"d": 1}]})
    kinds = {c["kind"] for c in chunks}
    _check("chunks cover videos, intel, campaigns, content, metrics, dashboard",
           {"video", "intel", "campaign", "content", "metrics", "dashboard"} <= kinds)
    _check("long transcript split into word chunks",
           sum(1 for c in chunks if c["id"].startswith("yt:wch_test01:v1")) >= 1
           and all(len(c["text"].split()) <= assistant_ai.CHUNK_WORDS for c in chunks
                   if c["kind"] == "video"))

    # --- Fingerprint: changes when a source changes -----------------------------------------------
    fp1 = assistant_ai.fingerprint(ws, archives)
    archives[0][0]["transcript_count"] = 2
    fp2 = assistant_ai.fingerprint(ws, archives)
    _check("fingerprint moves when watcher data moves", fp1 != fp2)

    # --- BM25 retrieval + the date filter ---------------------------------------------------------
    index = assistant_ai.build_index(chunks, fp=fp2)
    hits = assistant_ai.search(index, "how should I price AI retainers")
    _check("search ranks the pricing transcript first",
           hits and hits[0]["kind"] == "video" and "retainers" in hits[0]["text"])
    hits = assistant_ai.search(index, "cold outreach emails",
                               date_from="2026-01-01", date_to="")
    _check("date range excludes the 2024 video",
           all(c["id"] != "yt:wch_test01:v2:0" for c in hits))

    # --- Lenient answer parsing -------------------------------------------------------------------
    _check("parses plain JSON", assistant_ai._parse_answer('{"answer": "Charge monthly."}')
           == "Charge monthly.")
    _check("parses fenced JSON", assistant_ai._parse_answer('```json\n{"answer": "Yes."}\n```') == "Yes.")
    _check("falls back to raw text", assistant_ai._parse_answer("Just words.") == "Just words.")
    # Nearly-JSON salvage: the UI must NEVER be handed a raw JSON envelope (2026-07-13 failure:
    # junk between the answer string's closing quote and the brace made json.loads reject it all).
    broken = '{\n  "answer": "Reed says \\"charge monthly\\" [1].\\n\\nUse retainers."\n."\n}'
    _check("salvages junk inside the envelope",
           assistant_ai._parse_answer(broken) == 'Reed says "charge monthly" [1].\n\nUse retainers.')
    _check("salvages trailing junk after a valid object",
           assistant_ai._parse_answer('{"answer": "Done."} trailing noise') == "Done.")
    _check("tolerates raw newlines inside the answer string",
           assistant_ai._parse_answer('{"answer": "Line one.\nLine two."}') == "Line one.\nLine two.")
    _check("salvages output truncated at the token cap",
           assistant_ai._parse_answer('{"answer": "Cut off mid-sent') == "Cut off mid-sent")
    _check("a broken envelope never reaches the UI as raw JSON",
           not assistant_ai._parse_answer(broken).lstrip().startswith("{"))

    # --- ask() with a stubbed model ---------------------------------------------------------------
    answer, sources, err = assistant_ai.ask(
        "Riverdance", index, "how should I price AI retainers",
        caller=lambda system, user: ('{"answer": "Monthly retainers, per Carson."}', ""))
    _check("ask returns the answer + cited sources",
           answer == "Monthly retainers, per Carson." and err == ""
           and any("Carson Reed" in s["title"] for s in sources))
    answer, sources, err = assistant_ai.ask(
        "Riverdance", index, "how should I price AI retainers",
        caller=lambda system, user: ("", "no AI provider configured"))
    _check("model error surfaces as the error", err == "no AI provider configured" and answer == "")
    answer, sources, err = assistant_ai.ask("Riverdance", index, "zzqx unmatchable gibberish qqq")
    _check("no matching chunks -> friendly error", err != "" and "match" in err)

    # --- Depth: the detail control shapes prompts, and deep query-plans before answering ---------
    _check("depth styles the system prompt",
           "2-4 tight sentences" in assistant_ai._system_prompt("X", "quick")
           and "structured analysis" in assistant_ai._system_prompt("X", "deep")
           and "disagreement worth reporting" in assistant_ai._system_prompt("X"))
    _check("plan_queries parses the model's queries",
           assistant_ai.plan_queries("nick vs carson", [], lambda s, u: (
               '{"queries": ["nick saraev cold email", "carson reed paid ads"]}', ""))
           == ["nick saraev cold email", "carson reed paid ads"])
    _check("plan_queries degrades to [] on any failure",
           assistant_ai.plan_queries("q", [], lambda s, u: ("not json", "")) == []
           and assistant_ai.plan_queries("q", [], lambda s, u: ("", "model down")) == [])
    calls = []

    def deep_caller(system, user):
        calls.append(system)
        if "search queries" in system:
            return ('{"queries": ["pricing AI retainers", "cold outreach emails"]}', "")
        return ('{"answer": "Deep dive."}', "")

    answer, sources, err = assistant_ai.ask(
        "Riverdance", index, "how should I price AI retainers",
        depth="deep", caller=deep_caller)
    _check("deep ask plans queries, then answers with the deep prompt",
           answer == "Deep dive." and err == "" and len(calls) == 2
           and "structured analysis" in calls[1])
    _check("deep retrieval unions the planned queries' hits",
           any("cold outreach" in s["title"].lower() or s["kind"] == "video" for s in sources))

    # --- The route: lazy rebuild, reindex, ask (stubbed), gating ---------------------------------
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(SUPER)

    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "reindex"})
    data = r.get_json()
    _check("op=reindex builds + stores the index", data["ok"] is True and data["chunks"] > 0)
    _check("index object persisted", workspace.read_assistant_index(CLIENT) is not None)

    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "ask", "question": ""})
    _check("empty question refused", r.get_json()["ok"] is False)

    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "settings", "depth": "deep"})
    _check("op=settings saves the depth",
           r.get_json()["ok"] is True
           and (workspace.load_workspace(CLIENT).get("assistant") or {}).get("depth") == "deep")
    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "settings", "depth": "bogus"})
    _check("unknown depth refused", r.get_json()["ok"] is False)
    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "settings", "model": ""})
    _check("saving the model alone leaves the depth untouched",
           r.get_json()["ok"] is True
           and (workspace.load_workspace(CLIENT).get("assistant") or {}).get("depth") == "deep")

    real_ask = assistant_ai.ask
    assistant_ai.ask = lambda name, idx, q, **kw: ("Answer from the stub.", [
        {"title": "Campaign: Summer Lead-Gen Push", "kind": "campaign", "date": "", "url": ""}], "")
    r = c.post("/w/%s/admin/assistant" % CLIENT,
               data={"op": "ask", "question": "What campaigns are running?",
                     "history": '[{"role":"user","text":"hi"}]'})
    data = r.get_json()
    _check("op=ask returns answer + sources",
           data["ok"] is True and data["answer"] == "Answer from the stub."
           and data["sources"][0]["kind"] == "campaign")
    assistant_ai.ask = real_ask

    body = c.get("/w/%s/assistant" % CLIENT).get_data(as_text=True)
    _check("assistant pane renders for the team",
           'data-pane="assistant"' in body and 'id="ax-as-send"' in body)
    _check("detail (depth) selectors render in both surfaces, with the saved choice",
           'id="ax-as-depth"' in body and 'id="ax-asfab-depth"' in body
           and 'value="deep" selected' in body)

    with c.session_transaction() as s:
        s.clear()
        s.update(CLIENT_LOGIN)
    body = c.get("/w/%s/assistant" % CLIENT).get_data(as_text=True)
    _check("client hitting /assistant is bounced (no pane in the DOM)",
           'data-pane="assistant"' not in body)
    _check("client POST is forbidden",
           c.post("/w/%s/admin/assistant" % CLIENT,
                  data={"op": "ask", "question": "hi"}).status_code == 403)


if __name__ == "__main__":
    try:
        run()
        print("PASS")
    except AssertionError as exc:
        print("FAIL: %s" % exc)
        sys.exit(1)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
