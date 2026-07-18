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

    # --- Hybrid retrieval: the semantic leg surfaces a chunk BM25 misses; RRF fuses the legs ------
    # A tiny deterministic "embedder": text -> a concept-count vector, so a query and a document that
    # share MEANING but no keywords ("coming back" ~ "loyalty/churn/repeat") land on the same vector.
    _CONCEPTS = (
        ("retention", "loyalty", "repeat", "coming back", "churn", "retain"),
        ("price", "pricing", "retainer", "charge", "fee", "cost"),
        ("email", "inbox", "outreach", "cold"),
    )

    def _fake_embed(texts):
        vecs = []
        for t in texts:
            tl = (t or "").lower()
            v = [float(sum(w in tl for w in grp)) for grp in _CONCEPTS]
            vecs.append(v if any(v) else [0.01, 0.01, 0.01])
        return vecs, ""

    hchunks = [
        {"id": "h_loyal", "kind": "content", "title": "Loyalty program", "url": "", "date": "",
         "text": "our loyalty program keeps churn low and drives repeat purchases"},
        {"id": "h_cold", "kind": "content", "title": "Cold email", "url": "", "date": "",
         "text": "cold outreach emails to brand new prospects"},
        {"id": "h_price", "kind": "content", "title": "Pricing", "url": "", "date": "",
         "text": "we charge a monthly retainer fee for the service"},
    ]
    hidx = assistant_ai.build_index(hchunks)
    _check("BM25 alone misses the semantically-related chunk",
           all(c["id"] != "h_loyal"
               for c in assistant_ai.search(hidx, "how do we get customers coming back")))
    assistant_ai.embed_index(hidx, _fake_embed)
    _check("embed_index attaches a semantic leg",
           assistant_ai.has_embeddings(hidx) and hidx["emb_count"] == 3 and hidx["emb_dim"] == 3)
    _check("packed vectors round-trip",
           assistant_ai._unpack_vec(hidx["emb"]["h_cold"], 3) is not None)

    # --- Incremental embedding: a rebuild reuses unchanged vectors, embeds only what changed --------
    # (the "was fast, now unusable" fix -- a Watcher fetch must not re-embed the whole corpus).
    embed_calls = []

    def _counting_embed(texts):
        embed_calls.append(list(texts))
        return _fake_embed(texts)

    # One new chunk, one changed chunk, one unchanged -> exactly two embed calls, one reused vector.
    hchunks2 = [
        dict(hchunks[0]),                                        # h_loyal: unchanged -> reuse
        {"id": "h_cold", "kind": "content", "title": "Cold email", "url": "", "date": "",
         "text": "cold outreach emails to WARM prospects now"},  # h_cold: content changed -> re-embed
        {"id": "h_new", "kind": "content", "title": "Upsell", "url": "", "date": "",
         "text": "upsell existing customers into a higher tier"},  # brand new -> embed
    ]
    hidx2 = assistant_ai.build_index(hchunks2)
    reused_vec = hidx["emb"]["h_loyal"]
    assistant_ai.embed_index(hidx2, _counting_embed, prev=hidx)
    embedded_texts = [t for batch in embed_calls for t in batch]
    _check("incremental embed skips unchanged chunks, embeds only new/changed ones",
           hidx2["emb_count"] == 3 and len(embedded_texts) == 2)
    _check("the unchanged chunk's stored vector is carried over verbatim (no re-embed)",
           hidx2["emb"]["h_loyal"] == reused_vec
           and not any("loyalty program" in t.lower() for t in embedded_texts))
    _check("a changed chunk IS re-embedded (stale vector never reused)",
           any("warm prospects" in t.lower() for t in embedded_texts))
    _check("first-ever embed (no prev) still embeds everything",
           (lambda i: (assistant_ai.embed_index(i, _fake_embed), i["emb_count"])[1])(
               assistant_ai.build_index(hchunks)) == 3)
    answer, sources, err = assistant_ai.ask(
        "X", hidx, "how do we get customers coming back",
        caller=lambda system, user: ('{"answer": "Lean on loyalty."}', ""),
        query_embedder=lambda q: (_fake_embed([q])[0][0], ""))
    _check("hybrid retrieval surfaces the chunk BM25 missed",
           err == "" and any(s["title"] == "Loyalty program" for s in sources))

    # --- RRF: rank-only fusion (incompatible BM25/cosine scales never fight) ----------------------
    _check("RRF ranks the item strong in BOTH lists first",
           assistant_ai._rrf([[5, 3, 1], [3, 9, 1]])[0] == 3)
    _check("RRF de-dupes across lists", set(assistant_ai._rrf([[1, 2], [2, 3]])) == {1, 2, 3})

    # --- Metadata pre-filter: confident single-source only, never a cross-source/generic question -
    _check("single-source question infers its kind",
           assistant_ai._infer_kinds("how are we handling the client's email replies") == {"email"})
    _check("cross-source question stays unfiltered",
           assistant_ai._infer_kinds("compare our campaigns with what creators say in videos") is None)
    _check("generic question stays unfiltered",
           assistant_ai._infer_kinds("what should we focus on next quarter") is None)

    # --- Titles are searchable + a named creator survives a 'campaign' question (the 2026-07 bug) --
    # The creator/channel name lives only in a chunk's TITLE, never in the transcript body, so it
    # must be indexed for a name query to retrieve that creator's videos.
    _check("a video is retrievable by its creator NAME (title is indexed, not just the body)",
           any(h["kind"] == "video" for h in assistant_ai.search(index, "Carson Reed")))
    # "what would <creator> say about <campaign>" contains 'campaign' -> _infer_kinds alone would
    # scope to {content,campaign} and drop every transcript. The creator name must reopen video.
    _named_kinds = assistant_ai._infer_kinds("what would Carson Reed say about the summer campaign")
    _check("'campaign' question alone would exclude videos",
           _named_kinds is not None and "video" not in _named_kinds)
    _cross = assistant_ai.ask(
        "Riverdance", index, "what would Carson Reed say about the summer campaign",
        caller=lambda system, user: ('{"answer": "ok"}', ""))
    _check("a creator named beside 'campaign' still retrieves that creator's videos",
           _cross[2] == "" and any(s["kind"] == "video" and "Carson Reed" in s["title"]
                                   for s in _cross[1]))

    # --- Reranker seam: it gets {id,title,content} records and ITS order drives the cited sources --
    seen_rr = {}

    def _rerank_stub(query, records, top_n):
        seen_rr["keys"] = set(records[0].keys())
        picked = ([r for r in records if r["title"] == "Cold email"]
                  + [r for r in records if r["title"] != "Cold email"])
        return picked[:top_n or len(picked)], ""

    answer, sources, err = assistant_ai.ask(
        "X", hidx, "prospects loyalty retainer service",
        caller=lambda system, user: ('{"answer": "ok"}', ""), reranker=_rerank_stub)
    _check("reranker receives id/title/content records", {"id", "title", "content"} <= seen_rr["keys"])
    _check("rerank order drives the cited sources", sources and sources[0]["title"] == "Cold email")
    # A failing reranker must degrade to the fused order, never blow up the answer.
    answer, sources, err = assistant_ai.ask(
        "X", hidx, "prospects loyalty retainer service",
        caller=lambda system, user: ('{"answer": "ok"}', ""),
        reranker=lambda q, recs, n: (recs, "rerank error"))
    _check("failing reranker degrades gracefully", err == "" and answer == "ok" and sources)

    # --- Streaming ask: sources first, live thinking+answer deltas, steer injection ---------------
    seen_prompt = {}

    def _stream_caller(system, user):
        seen_prompt["system"] = system
        seen_prompt["user"] = user
        yield {"type": "thinking", "text": "weighing loyalty vs cold email"}
        yield {"type": "answer", "text": "Lean on "}
        yield {"type": "answer", "text": "loyalty."}
        yield {"type": "usage", "input_tokens": 12, "output_tokens": 5}

    evs = list(assistant_ai.ask_stream(
        "X", hidx, "how do we keep customers", steer="focus on retention only",
        query_embedder=lambda q: (_fake_embed([q])[0][0], ""), stream_caller=_stream_caller))
    types = [e["type"] for e in evs]
    _check("stream emits sources FIRST, then thinking, answer, usage",
           types[0] == "sources" and "thinking" in types and "answer" in types and "usage" in types)
    _check("stream answer deltas assemble the reply",
           "".join(e["text"] for e in evs if e["type"] == "answer") == "Lean on loyalty.")
    _check("streaming prompt is plain-markdown (no JSON envelope)",
           '"answer"' not in seen_prompt["system"] and "markdown" in seen_prompt["system"].lower())
    _check("the steer is injected into the answer prompt",
           "focus on retention only" in seen_prompt["user"])
    empty = list(assistant_ai.ask_stream("X", hidx, "zzqx nomatch qqq",
                                         stream_caller=_stream_caller))
    _check("stream with no hits -> a single error event",
           len(empty) == 1 and empty[0]["type"] == "error")

    # --- Plan checkpoint: returns the sub-questions + sources WITHOUT answering -------------------
    def _plan_caller(system, user):
        if "search queries" in system:
            return ('{"queries": ["loyalty retention", "cold email outreach"]}', "")
        return ("", "")

    queries, psources = assistant_ai.plan_stage(
        hidx, "keep customers vs win new ones", depth="deep",
        query_embedder=lambda q: (_fake_embed([q])[0][0], ""), plan_caller=_plan_caller)
    _check("plan_stage returns the planned sub-questions + sources",
           "keep customers vs win new ones" in queries and len(queries) >= 2 and psources)

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

    # --- The streaming route: SSE frames for the answer stage AND the plan checkpoint ------------
    real_stream = assistant_ai.ask_stream
    assistant_ai.ask_stream = lambda name, idx, q, **kw: iter([
        {"type": "sources", "sources": [{"title": "Carson Reed", "kind": "video", "date": "", "url": ""}]},
        {"type": "thinking", "text": "considering the transcripts"},
        {"type": "answer", "text": "Monthly retainers."},
        {"type": "usage", "input_tokens": 3, "output_tokens": 2},
    ])
    r = c.post("/w/%s/admin/assistant/stream" % CLIENT,
               data={"question": "how to price?", "stage": "answer"})
    sse = r.get_data(as_text=True)
    _check("stream route emits SSE content-type", r.mimetype == "text/event-stream")
    _check("stream route forwards sources/thinking/answer + a priced usage + done",
           "event: sources" in sse and "event: thinking" in sse and "event: answer" in sse
           and "event: usage" in sse and "cost_usd" in sse and "event: done" in sse)
    assistant_ai.ask_stream = real_stream

    real_plan = assistant_ai.plan_stage
    assistant_ai.plan_stage = lambda idx, q, *a, **kw: (
        ["price retainers", "cold email"], [{"title": "Carson Reed", "kind": "video", "date": "", "url": ""}])
    r = c.post("/w/%s/admin/assistant/stream" % CLIENT,
               data={"question": "compare", "stage": "plan"})
    sse = r.get_data(as_text=True)
    _check("plan stage returns plan + sources, no answer",
           "event: plan" in sse and "event: sources" in sse and "event: answer" not in sse)
    assistant_ai.plan_stage = real_plan

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
    _check("client streaming POST is forbidden",
           c.post("/w/%s/admin/assistant/stream" % CLIENT,
                  data={"question": "hi"}).status_code == 403)

    # --- Conversation history: server-side team-shared save/list/get/delete -----------------------
    # workspace layer: upsert by id, newest-first, turns/list caps.
    workspace.save_assistant_conversation(CLIENT, "cv1", "Pricing chat",
                                          [{"role": "user", "text": "how to price?"},
                                           {"role": "bot", "text": "Monthly retainers."}])
    workspace.save_assistant_conversation(CLIENT, "cv2", "Campaigns chat",
                                          [{"role": "user", "text": "what campaigns?"}])
    lst = workspace.list_assistant_conversations(CLIENT)
    _check("history list returns both, newest first, without turns",
           len(lst) == 2 and lst[0]["id"] == "cv2" and "turns" not in lst[0]
           and lst[0]["turn_count"] == 1)
    workspace.save_assistant_conversation(CLIENT, "cv1", "Pricing chat v2",
                                          [{"role": "user", "text": "how to price?"},
                                           {"role": "bot", "text": "Monthly retainers."},
                                           {"role": "user", "text": "and support?"}])
    _check("saving same id UPSERTS (no duplicate) + bumps it to newest",
           len(workspace.list_assistant_conversations(CLIENT)) == 2
           and workspace.list_assistant_conversations(CLIENT)[0]["id"] == "cv1")
    got = workspace.get_assistant_conversation(CLIENT, "cv1")
    _check("get returns the full turns", got and len(got["turns"]) == 3
           and got["title"] == "Pricing chat v2")
    workspace.delete_assistant_conversation(CLIENT, "cv2")
    _check("delete removes just that conversation",
           [x["id"] for x in workspace.list_assistant_conversations(CLIENT)] == ["cv1"])

    # route ops (as the team; client is still logged in as CLIENT here -> must be forbidden first).
    _check("client history POST is forbidden",
           c.post("/w/%s/admin/assistant" % CLIENT,
                  data={"op": "history_list"}).status_code == 403)
    with c.session_transaction() as s:
        s.clear(); s.update(SUPER)
    r = c.post("/w/%s/admin/assistant" % CLIENT,
               data={"op": "history_save", "conv_id": "cv3", "title": "Route saved",
                     "turns": '[{"role":"user","text":"hi"},{"role":"bot","text":"hello"}]'})
    _check("op=history_save persists + returns the list",
           r.get_json()["ok"] is True
           and any(x["id"] == "cv3" for x in r.get_json()["conversations"]))
    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "history_get", "conv_id": "cv3"})
    _check("op=history_get returns the turns",
           r.get_json()["ok"] is True and len(r.get_json()["conversation"]["turns"]) == 2)
    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "history_get", "conv_id": "nope"})
    _check("op=history_get on a missing id -> friendly not-ok", r.get_json()["ok"] is False)
    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "history_delete", "conv_id": "cv3"})
    _check("op=history_delete removes it",
           r.get_json()["ok"] is True
           and not any(x["id"] == "cv3" for x in r.get_json()["conversations"]))


if __name__ == "__main__":
    try:
        run()
        print("PASS")
    except AssertionError as exc:
        print("FAIL: %s" % exc)
        sys.exit(1)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
