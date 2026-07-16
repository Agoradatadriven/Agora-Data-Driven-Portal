"""Local smoke test for the Market Intelligence AI brain -- runs entirely off-cloud, no network.

Exercises intel_ai (model registry + availability gating, Vertex + DeepSeek transports, the
retrieve-then-curate mapping onto REAL articles, error surfacing, NO news-feed fallback), the bulk
favourite/delete data layer, and intel_refresh end-to-end with injected transports. Proves:
  * a fabricated/out-of-range article index from the model is DROPPED (never a hallucinated link),
  * link/source/date always come from the real candidate, never the model,
  * every failure returns a SHORT reason (curate -> (None, reason)); there is NO RSS fallback,
  * refresh_client fills ONLY via the model, records the reason on failure, and does NOT latch the
    12-month backfill flag when the run errored,
  * favourite stars + pins an entry so replace_auto_intel keeps it; favourites sort to the top.

    python _intel_ai_localtest.py        # prints PASS / FAIL and exits 0 / 1
"""

import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="intel_ai_localtest_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
for _k in ("DEEPSEEK_API_KEY", "VERTEX_GEMINI_ENABLED", "GEMINI_API_KEY",
           "VERTEX_ACCESS_TOKEN", "ASSISTANT_EMBED_ENABLED", "ASSISTANT_RERANK_ENABLED"):
    os.environ.pop(_k, None)

import atrium_view      # noqa: E402
import intel_ai         # noqa: E402
import intel_refresh    # noqa: E402
import workspace        # noqa: E402

CLIENT = "aitest"

_CANDS = [
    {"title": "Google Ads adds a new PMax control", "link": "https://sel.com/a1",
     "body": "Advertisers get finer budget caps.", "source": "Search Engine Land", "date": "2026-07-01"},
    {"title": "Meta overhauls Advantage+ targeting", "link": "https://sel.com/a2",
     "body": "New signals for shopping campaigns.", "source": "Marketing Dive", "date": "2026-06-28"},
    {"title": "A totally irrelevant celebrity story", "link": "https://gossip.com/a3",
     "body": "Nothing to do with marketing.", "source": "Gossip Daily", "date": "2026-06-30"},
]

_MODEL_JSON = json.dumps({"entries": [
    {"n": 1, "heading": "Platform Update", "title": "Google Ads adds a new PMax control",
     "summary": "Google Ads now lets you cap PMax budgets more tightly -- useful for controlling spend."},
    {"n": 2, "heading": "Platform Update", "title": "Meta overhauls Advantage+ targeting",
     "summary": "Meta changed Advantage+ shopping signals; revisit your audience setup."},
    {"n": 9, "heading": "Fake", "title": "Hallucinated item", "summary": "Should be dropped."},
]})


class _Resp(object):
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return json.loads(self._payload) if isinstance(self._payload, str) else self._payload


def _deepseek_fetcher(url, headers, payload, timeout):
    return _Resp({"choices": [{"message": {"content": _MODEL_JSON}}]})


def _vertex_fetcher(url, headers, payload, timeout):
    return _Resp({"candidates": [{"content": {"parts": [{"text": _MODEL_JSON}]}}]})


def _quota_fetcher(url, headers, payload, timeout):
    return _Resp({"error": {"message": "Your prepayment credits are depleted."}}, status=429)


def _bad_json_fetcher(url, headers, payload, timeout):
    return _Resp({"choices": [{"message": {"content": "sorry, I cannot help"}}]})


def _token():
    return "fake-vertex-token"


def _check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def run():
    print("[intel-ai-localtest] WORKSPACE_LOCAL_DIR = %s" % _TMP)

    # 1. Registry + gating: gemini gated on Vertex flag, deepseek on its key.
    _check("four models offered", len(intel_ai.MODELS) == 4)
    _check("no config -> nothing available", all(not m["available"] for m in intel_ai.available_models()))
    _check("no config -> default_model ''", intel_ai.default_model() == "")
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    _check("deepseek key -> deepseek available", intel_ai.model_available("deepseek-v4-pro"))
    _check("gemini still unavailable (no Vertex)", not intel_ai.model_available("gemini-2.5-pro"))
    os.environ["VERTEX_GEMINI_ENABLED"] = "1"
    _check("VERTEX_GEMINI_ENABLED -> gemini available", intel_ai.model_available("gemini-2.5-flash"))
    _check("default_model prefers first available (gemini flash)", intel_ai.default_model() == "gemini-2.5-flash")

    # 2. Prompts.
    _check("business default prompt present", "industry" in intel_ai.default_prompt("business_research").lower())

    # 3. curate via DeepSeek -> (entries, ""); drops fabricated #9; maps onto REAL articles.
    out, err = intel_ai.curate("media_buying", "Aitest Co", ["ppc"], _CANDS,
                               model="deepseek-v4-pro", fetcher=_deepseek_fetcher)
    _check("deepseek curate ok, no error", err == "" and out is not None and len(out) == 2)
    _check("real link kept", out[0]["link"] == "https://sel.com/a1")
    _check("real source kept", out[0]["source"] == "Search Engine Land")
    _check("model summary used as body", "cap PMax budgets" in out[0]["body"])
    _check("irrelevant #3 not selected", all(e["link"] != "https://gossip.com/a3" for e in out))

    # 4. curate via Vertex Gemini (token + fetcher injected) works the same.
    gout, gerr = intel_ai.curate("media_buying", "Aitest Co", ["ppc"], _CANDS,
                                 model="gemini-2.5-flash", fetcher=_vertex_fetcher, token_fetcher=_token)
    _check("vertex curate maps to real links", gerr == "" and gout and gout[0]["link"] == "https://sel.com/a1")

    # 5. Error surfacing (NO fallback -> (None, reason)).
    e1 = intel_ai.curate("media_buying", "X", [], _CANDS, model="", fetcher=_deepseek_fetcher)
    _check("no model -> (None, 'no model selected')", e1[0] is None and "no model" in e1[1])
    os.environ.pop("DEEPSEEK_API_KEY", None)
    e2 = intel_ai.curate("media_buying", "X", [], _CANDS, model="deepseek-v4-pro", fetcher=_deepseek_fetcher)
    _check("unconfigured provider -> (None, reason)", e2[0] is None and "configured" in e2[1].lower())
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    e3 = intel_ai.curate("media_buying", "X", [], [], model="deepseek-v4-pro", fetcher=_deepseek_fetcher)
    _check("no candidates -> (None, reason)", e3[0] is None and "source articles" in e3[1])
    e4 = intel_ai.curate("media_buying", "X", [], _CANDS, model="deepseek-v4-pro", fetcher=_quota_fetcher)
    _check("429 -> 'out of quota/credits'", e4[0] is None and "quota" in e4[1])
    e5 = intel_ai.curate("media_buying", "X", [], _CANDS, model="deepseek-v4-pro", fetcher=_bad_json_fetcher)
    _check("unparseable -> (None, reason)", e5[0] is None and e5[1])

    # 5b. suggest_config ("Write these for me"): drafts the three settings from client context.
    _SUGGEST_JSON = json.dumps({"topics": "RV rentals, campgrounds, roadtrip travellers",
                                "business_prompt": "Watch RV rental demand and campground policy.",
                                "media_prompt": "Focus on Google and Meta travel-ad changes."})

    def _suggest_grounded_fetcher(url, headers, payload, timeout):
        assert payload.get("tools"), "a Gemini suggest should ground on Google Search"
        return _Resp({"candidates": [{"content": {"parts": [{"text": _SUGGEST_JSON}]}}]})

    sg, serr = intel_ai.suggest_config("RV Co", "Their website: https://rv.example",
                                       model="gemini-2.5-flash",
                                       fetcher=_suggest_grounded_fetcher, token_fetcher=_token)
    _check("suggest via Gemini ok (grounded)", serr == "" and sg is not None)
    _check("suggest returns all three fields",
           "RV rentals" in sg["topics"] and sg["business_prompt"] and sg["media_prompt"])

    def _suggest_deepseek_fetcher(url, headers, payload, timeout):
        return _Resp({"choices": [{"message": {"content": _SUGGEST_JSON}}]})

    os.environ.pop("VERTEX_GEMINI_ENABLED", None)   # only DeepSeek left -> plain JSON-mode call
    sd, sderr = intel_ai.suggest_config("RV Co", "", model="", fetcher=_suggest_deepseek_fetcher)
    _check("suggest falls back to the default available model", sderr == "" and sd["topics"])
    os.environ["VERTEX_GEMINI_ENABLED"] = "1"
    sq, sqerr = intel_ai.suggest_config("RV Co", "", model="gemini-2.5-flash",
                                        fetcher=_quota_fetcher, token_fetcher=_token)
    _check("suggest surfaces a model failure", sq is None and "quota" in sqerr)
    os.environ.pop("DEEPSEEK_API_KEY", None)
    os.environ.pop("VERTEX_GEMINI_ENABLED", None)
    sn, snerr = intel_ai.suggest_config("RV Co", "")
    _check("suggest with no provider -> (None, reason)", sn is None and "configured" in snerr)
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    os.environ["VERTEX_GEMINI_ENABLED"] = "1"

    # Window + count config: defaults, validation, clamping.
    _check("window default is 3m", intel_ai.window_of({}) == "3m")
    _check("valid window honored", intel_ai.window_of({"window": "12m"}) == "12m")
    _check("invalid window -> default", intel_ai.window_of({"window": "99y"}) == "3m")
    _check("window_label maps value -> label", intel_ai.window_label("12m") == "Past 12 months")
    _check("count default 8", intel_ai.count_of({}) == 8)
    _check("count clamped to MAX", intel_ai.count_of({"count": "999"}) == intel_ai.MAX_COUNT)
    _check("count clamped to MIN", intel_ai.count_of({"count": "0"}) == intel_ai.MIN_COUNT)

    # 6. refresh_client end-to-end via GROUNDED research (Gemini + injected grounded fetcher + token).
    #    The model plans, "searches" (grounding), and returns entries with a per-item relevance line;
    #    the trace captures the search plan (queries) + grounded sources + reasoning.
    _GROUNDED = json.dumps({"entries": [
        {"heading": "Regulation", "title": "New freelancer tax rule for 2026",
         "summary": "The government introduced a new tax rule affecting freelancers.",
         "relevance": "The Contract Shop's freelancer customers will need updated contract clauses.",
         "source": "Legal Times", "url": "https://legaltimes.com/freelancer-tax", "date": "2026-07-01"},
        {"heading": "Market", "title": "Gig economy keeps growing",
         "summary": "Freelance workforce grew again this year.",
         "relevance": "A bigger freelance market means more demand for ready-made legal templates.",
         "source": "Biz Daily", "url": "https://bizdaily.com/gig-growth", "date": "2026-06-20"},
    ]})

    def _grounded_fetcher(url, headers, payload, timeout):
        include = payload["generationConfig"]["thinkingConfig"].get("includeThoughts")
        parts = []
        if include:
            parts.append({"text": "Planning: freelancer legal + gig-economy angles for this client.",
                          "thought": True})
        parts.append({"text": _GROUNDED})
        return _Resp({"candidates": [{
            "content": {"parts": parts},
            "groundingMetadata": {
                "webSearchQueries": ["freelancer contract law 2026", "gig economy regulation"],
                "groundingChunks": [
                    {"web": {"uri": "https://legaltimes.com/freelancer-tax", "title": "Legal Times"}},
                    {"web": {"uri": "https://bizdaily.com/gig-growth", "title": "Biz Daily"}}],
                "searchEntryPoint": {"renderedContent": "<div class='chips'>suggestions</div>"},
            }}]})

    workspace.save_workspace(CLIENT, {"display_name": "Aitest Co", "intel": {},
                                      "intel_topics": ["freelance contracts"],
                                      "intel_ai": {"model": "gemini-2.5-flash", "window": "6m",
                                                   "count": "5", "show_thinking": "1"}})
    workspace.add_intel_entry(CLIENT, "media_buying", {"title": "Hand-written note", "body": "keep me"})
    counts = intel_refresh.refresh_client(CLIENT, ai_fetcher=_grounded_fetcher, token_fetcher=_token)
    _check("grounded refresh used the AI (both sections)",
           counts["ai"] is True and counts["media_buying"] > 0 and counts["business_research"] > 0)
    ws = workspace.load_workspace(CLIENT)
    _check("no error recorded on success", ws["intel_ai"]["last_error"] == "")
    mb1 = ws["intel"]["media_buying"]
    n1 = len(mb1)
    _check("hand-written entry survives the refresh", any(e.get("title") == "Hand-written note" for e in mb1))
    br1 = ws["intel"]["business_research"]
    _check("business entries carry a relevance line", any((e.get("relevance") or "").strip() for e in br1))
    _check("real grounded source link kept", any((e.get("link") or "").startswith("http") for e in br1))
    tr = ws["intel_ai"]["last_trace"]["business_research"]
    _check("trace captured the search plan (queries)", len(tr.get("queries") or []) >= 1)
    _check("trace captured grounded sources", len(tr.get("sources") or []) >= 1)
    _check("trace captured reasoning (thoughts on)", tr.get("thinking", "").startswith("Planning"))

    # 6b. ADDITIVE + de-dup: a second identical run adds NOTHING new (grows, never wipes).
    intel_refresh.refresh_client(CLIENT, ai_fetcher=_grounded_fetcher, token_fetcher=_token)
    mb2 = workspace.load_workspace(CLIENT)["intel"]["media_buying"]
    _check("second identical run adds no duplicates", len(mb2) == n1)
    _check("hand-written entry still there after 2nd run",
           any(e.get("title") == "Hand-written note" for e in mb2))

    # 7. NO grounding on a non-Gemini model -> nothing added, clear reason (no fallback).
    workspace.save_workspace(CLIENT + "d", {"display_name": "DeepSeek Co", "intel": {},
                                            "intel_topics": ["x"],
                                            "intel_ai": {"model": "deepseek-v4-pro"}})
    cd = intel_refresh.refresh_client(CLIENT + "d", ai_fetcher=_grounded_fetcher, token_fetcher=_token)
    _check("non-Gemini model -> ai False, nothing added", cd["ai"] is False and cd["media_buying"] == 0)
    _check("non-Gemini reason recorded",
           "web research" in workspace.load_workspace(CLIENT + "d")["intel_ai"]["last_error"].lower())

    # 7b. A Vertex error (e.g. quota) -> nothing added, reason recorded.
    workspace.save_workspace(CLIENT + "q", {"display_name": "Quota Co", "intel": {},
                                            "intel_topics": ["x"],
                                            "intel_ai": {"model": "gemini-2.5-flash"}})
    cq = intel_refresh.refresh_client(CLIENT + "q", ai_fetcher=_quota_fetcher, token_fetcher=_token)
    _check("model failure -> ai False", cq["ai"] is False)
    _check("model failure -> NO entries written", cq["media_buying"] == 0)
    _check("failure reason recorded",
           "quota" in workspace.load_workspace(CLIENT + "q")["intel_ai"]["last_error"])

    # 8. No model selected -> refresh does nothing, records a helpful reason.
    workspace.save_workspace(CLIENT + "n", {"display_name": "NoModel Co", "intel": {}})
    cn = intel_refresh.refresh_client(CLIENT + "n", ai_fetcher=_grounded_fetcher, token_fetcher=_token)
    _check("no model -> zeros + ai False", cn == {"media_buying": 0, "business_research": 0, "ai": False})
    _check("no model -> reason recorded",
           "model" in workspace.load_workspace(CLIENT + "n")["intel_ai"]["last_error"].lower())

    # 9. Bulk favourite (star + pin) / unfavourite / delete + favourite survives a refresh + sorts top.
    workspace.save_workspace(CLIENT + "b", {"display_name": "Bulk Co", "intel": {}})
    a = workspace.add_intel_entry(CLIENT + "b", "media_buying", {"title": "Keep me", "date": "2026-01-01"})
    b = workspace.add_intel_entry(CLIENT + "b", "media_buying", {"title": "Delete me"})
    workspace.replace_auto_intel(CLIENT + "b", "media_buying", [{"title": "Auto item", "date": "2026-07-01"}])
    workspace.bulk_intel(CLIENT + "b", "media_buying", "favourite", [a["id"]])
    mb = workspace.load_workspace(CLIENT + "b")["intel"]["media_buying"]
    fav = [x for x in mb if x["id"] == a["id"]][0]
    _check("favourite sets star + drops auto (pin)", fav.get("favourite") is True and not fav.get("auto"))
    workspace.bulk_intel(CLIENT + "b", "media_buying", "delete", [b["id"]])
    ids = [x["id"] for x in workspace.load_workspace(CLIENT + "b")["intel"]["media_buying"]]
    _check("bulk delete removed the entry", b["id"] not in ids)
    # A refresh swaps auto entries but the favourited (pinned) one survives.
    workspace.replace_auto_intel(CLIENT + "b", "media_buying", [{"title": "Fresh auto", "date": "2026-08-01"}])
    titles = [x["title"] for x in workspace.load_workspace(CLIENT + "b")["intel"]["media_buying"]]
    _check("favourite survives auto refresh", "Keep me" in titles and "Fresh auto" in titles)
    # Favourites float to the top even though its date is older.
    secs = atrium_view.intel_sections(workspace.load_workspace(CLIENT + "b"))
    mb_sorted = [s for s in secs if s["key"] == "media_buying"][0]["entries"]
    _check("favourite sorts to the top", mb_sorted[0]["title"] == "Keep me")
    _check("unfavourite clears the star",
           (workspace.bulk_intel(CLIENT + "b", "media_buying", "unfavourite", [a["id"]]) and
            not [x for x in workspace.load_workspace(CLIENT + "b")["intel"]["media_buying"]
                 if x["id"] == a["id"]][0].get("favourite")))

    # 10. Assistant hybrid search: embeddings + reranking transport (injected fetcher + token).
    _emb_calls = []

    def _embed_fetcher(url, headers, payload, timeout):
        _emb_calls.append(payload)
        # A distinct 3-float vector per instance (real API returns 256; length is caller-agnostic).
        preds = [{"embeddings": {"values": [float(len(i["content"])), 1.0, 2.0]}}
                 for i in payload["instances"]]
        return _Resp({"predictions": preds})

    vecs, everr = intel_ai.embed_texts(["alpha", "beta gamma"], token_fetcher=_token,
                                       fetcher=_embed_fetcher)
    _check("embed_texts returns one vector per text", everr == "" and len(vecs) == 2 and vecs[0])
    _check("embed request carries task_type + content + outputDimensionality",
           _emb_calls[0]["instances"][0]["task_type"] == "RETRIEVAL_DOCUMENT"
           and "content" in _emb_calls[0]["instances"][0]
           and _emb_calls[0]["parameters"]["outputDimensionality"] == intel_ai.EMBED_DIM)
    qvec, qerr = intel_ai.embed_query("a question", token_fetcher=_token, fetcher=_embed_fetcher)
    _check("embed_query returns one vector with RETRIEVAL_QUERY",
           qerr == "" and qvec and _emb_calls[-1]["instances"][0]["task_type"] == "RETRIEVAL_QUERY")
    _emb_calls[:] = []
    many, merr = intel_ai.embed_texts(["t%d" % n for n in range(60)], token_fetcher=_token,
                                      fetcher=_embed_fetcher)
    _check("embed_texts batches over the count cap (>1 request, all embedded)",
           merr == "" and len(many) == 60 and all(v for v in many) and len(_emb_calls) >= 2)
    noc, ncerr = intel_ai.embed_texts(["x"], token_fetcher=lambda: "", fetcher=_embed_fetcher)
    _check("embed_texts with no creds -> (Nones, reason)", noc == [None] and "credentials" in ncerr)

    _rr_calls = []

    def _rerank_fetcher(url, headers, payload, timeout):
        _rr_calls.append((url, headers, payload))
        # Reverse the incoming order + score, to prove the API's order is what wins.
        out = [{"id": r["id"], "score": 1.0 - i * 0.1}
               for i, r in enumerate(reversed(payload["records"]))]
        return _Resp({"records": out})

    recs = [{"id": "0", "title": "A", "content": "first"},
            {"id": "1", "title": "B", "content": "second"},
            {"id": "2", "title": "C", "content": "third"}]
    ranked, rrerr = intel_ai.rerank("q", recs, top_n=2, token_fetcher=_token, fetcher=_rerank_fetcher)
    _check("rerank returns topN reordered records with scores",
           rrerr == "" and len(ranked) == 2 and ranked[0]["id"] == "2" and "score" in ranked[0])
    _check("rerank hits the Ranking API with a query-project header + model + topN",
           "rankingConfigs/default_ranking_config:rank" in _rr_calls[0][0]
           and _rr_calls[0][1].get("X-Goog-User-Project")
           and _rr_calls[0][2]["model"] == intel_ai.RERANK_MODEL
           and _rr_calls[0][2]["topN"] == 2)
    deg, degerr = intel_ai.rerank("q", recs, token_fetcher=_token, fetcher=_quota_fetcher)
    _check("rerank degrades to the input order on API error",
           degerr and [r["id"] for r in deg] == ["0", "1", "2"])
    dnc, dncerr = intel_ai.rerank("q", recs, token_fetcher=lambda: "")
    _check("rerank with no creds -> input unchanged + reason",
           dncerr and [r["id"] for r in dnc] == ["0", "1", "2"])

    # Gating helpers: both opt-in, both need Vertex auth wired.
    os.environ["VERTEX_GEMINI_ENABLED"] = "1"
    os.environ["ASSISTANT_EMBED_ENABLED"] = "1"
    _check("embeddings_configured on when opted in + Vertex wired", intel_ai.embeddings_configured())
    os.environ.pop("VERTEX_GEMINI_ENABLED", None)
    _check("embeddings_configured off without Vertex auth", not intel_ai.embeddings_configured())
    os.environ["VERTEX_GEMINI_ENABLED"] = "1"
    os.environ.pop("ASSISTANT_EMBED_ENABLED", None)
    _check("embeddings_configured off when the opt-in flag is absent",
           not intel_ai.embeddings_configured())
    _check("reranking_configured off by default", not intel_ai.reranking_configured())
    os.environ["ASSISTANT_RERANK_ENABLED"] = "1"
    _check("reranking_configured on when opted in", intel_ai.reranking_configured())
    os.environ.pop("ASSISTANT_RERANK_ENABLED", None)


def main():
    try:
        run()
    except AssertionError as exc:
        print("\n[FAIL] %s" % exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("\n[ERROR] %s" % exc)
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n[PASS] intel AI brain: Vertex/DeepSeek, real-link mapping, no-fallback errors, bulk/favourite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
