"""The Atrium Assistant (team-only tab): retrieval-augmented chat over EVERYTHING in a workspace.

Sources it reads (all already in the portal's hands, no new database):
  * Watcher transcript archives  -- every video's transcript, chunked ~1000 words
  * Market Intelligence          -- every briefing entry (both sections)
  * Campaigns + content          -- strategy, AI summary, every piece + its comments
  * Workspace metrics            -- the metrics/today/split snapshot the client sees
  * Content calendar + client conversations + website health notes
  * Client dashboard data        -- the per-client `<c>.json` KPI export (OPT-IN: needs the portal
                                    SA granted objectViewer on the client's dash bucket — run
                                    enable_assistant_dash_data.ps1; absent access is skipped).

How it works (deliberately no new infra -- mirrors the workspace-JSON posture):
  1. `build_chunks` flattens every source into small text chunks with a kind/title/date.
  2. `build_index` computes a classic BM25 index over them (pure Python, no dependencies) and the
     whole thing is stored as ONE private object per client (workspace/assistant/<c>/index.json).
     `fingerprint` detects when the underlying data changed, so the index rebuilds lazily.
  3. `ask` retrieves the top-scoring chunks for the question (optionally date-filtered), packs them
     into a grounded prompt, and answers with the SAME provider plumbing as the intel brain
     (`intel_ai._call`, default model -- Vertex Gemini when configured). The model is told to
     answer ONLY from the provided excerpts and name its sources; we also return the source list
     so the UI can show citation chips. The admin's DEPTH control ('quick'|'standard'|'deep')
     shapes the whole pipeline: deep first asks the model to PLAN extra search queries (so a
     comparative question retrieves each entity's actual positions, not just chunks containing the
     question's words), retrieves wider, turns provider thinking ON, and asks for a structured
     analysis; quick keeps it to a few sentences. Every depth is allowed to SYNTHESIZE across
     excerpts -- differing recommendations count as disagreement even when nobody names the other.

Pure + testable: chunking/indexing/search are dependency-free; `ask` accepts a `caller` injection
so tests run with no network. Every failure degrades to (\"\", sources, reason) -- never a raise.
"""

import json
import math
import re

# Small, boring stopword list -- enough to keep BM25 focused without a dependency.
_STOP = frozenset(
    "a an and are as at be but by for from has have how i if in into is it its me my not of on or "
    "our so that the their them they this to was we what when where which who why will with you "
    "your".split())

CHUNK_WORDS = 1000          # transcript chunk size (words)
TOP_K = 18                  # excerpts handed to the model per question
MAX_CONTEXT_CHARS = 90000   # hard cap on packed context (stays well inside Gemini's window)


def _tokens(text):
    """Lowercase word tokens minus stopwords (the BM25 vocabulary)."""
    return [t for t in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(t) > 1 and t not in _STOP]


# --- 1. Flatten the workspace into chunks ---------------------------------------------------------
def build_chunks(ws, archives, dash_data=None):
    """Every source in the workspace as a flat list of {id, kind, title, url, date, text} chunks.

    `archives` is [(channel_entry, videos), ...] -- the Watcher registry entries with their video
    lists (loaded by the caller so this stays I/O-free). `dash_data` is the optional client
    dashboard JSON (None when the bucket isn't readable)."""
    chunks = []

    def add(cid, kind, title, text, url="", date=""):
        text = (text or "").strip()
        if text:
            chunks.append({"id": cid, "kind": kind, "title": title, "url": url,
                           "date": date, "text": text})

    # Watcher transcripts, chunked so one video can yield several retrievable excerpts.
    for ch, videos in archives or []:
        cname = ch.get("title", "channel")
        for v in videos:
            words = (v.get("transcript") or "").split()
            for i in range(0, len(words), CHUNK_WORDS):
                part = " ".join(words[i:i + CHUNK_WORDS])
                add("yt:%s:%s:%d" % (ch.get("id", ""), v.get("id", ""), i // CHUNK_WORDS),
                    "video", "%s — %s" % (cname, v.get("title", "")),
                    part, url=v.get("url", ""), date=v.get("published", ""))

    # Market Intelligence briefing entries.
    intel = ws.get("intel") or {}
    for section, label in (("business_research", "Business Research"),
                           ("media_buying", "Media Buying News")):
        for e in intel.get(section) or []:
            add("intel:%s" % e.get("id", ""), "intel",
                "Market Intelligence (%s): %s" % (label, e.get("title", "")),
                " ".join(filter(None, [e.get("title", ""), e.get("body", ""),
                                       e.get("relevance", ""), "Source: " + (e.get("source") or "")])),
                url=e.get("link", ""), date=e.get("date", ""))

    # Campaigns: strategy + AI summary, then every content piece with its status and comments.
    for camp in ws.get("campaigns") or []:
        strategy = camp.get("strategy") or {}
        add("camp:%s" % camp.get("id", ""), "campaign",
            "Campaign: %s" % camp.get("name", ""),
            " ".join(filter(None, ["Channel: %s." % (camp.get("channel") or ""),
                                   json.dumps(strategy) if strategy else "",
                                   camp.get("ai_summary", "")])))
        for p in camp.get("content") or []:
            comments = "; ".join("%s: %s" % (c.get("sender_name") or c.get("sender", ""),
                                             c.get("body", ""))
                                 for c in p.get("comments") or [])
            label = " ".join(filter(None, [p.get("ref", ""), p.get("type_tag", "")]))
            add("content:%s" % p.get("id", ""), "content",
                "Content piece %s (%s)" % (label or p.get("id", ""), camp.get("name", "")),
                " ".join(filter(None, [p.get("sub_tag", ""), p.get("platform", ""),
                                       "status " + (p.get("status") or ""),
                                       p.get("caption", ""), p.get("client_note", ""),
                                       ("Comments: " + comments) if comments else ""])),
                date=p.get("date", ""))

    # The workspace metrics snapshot (what the client's overview shows).
    metrics = {k: ws.get(k) for k in ("metrics", "today", "split") if ws.get(k)}
    if metrics:
        add("metrics", "metrics", "Workspace metrics snapshot", json.dumps(metrics))
    series = ws.get("series")
    if series:
        add("series", "metrics", "Leads time series", json.dumps(series))

    # Calendar, conversations, website health.
    cal = ws.get("calendar") or []
    if cal:
        add("calendar", "calendar", "Content calendar",
            "; ".join("%s: %s (%s)" % (e.get("date", ""), e.get("label", ""),
                                       "done" if e.get("status") == "done" else "planned")
                      for e in cal))
    for conv in ws.get("conversations") or []:
        add("conv:%s" % conv.get("id", ""), "conversation",
            "Client conversation: %s" % conv.get("subject", ""),
            "; ".join("%s: %s" % (m.get("from", ""), m.get("body", ""))
                      for m in conv.get("messages") or []))
    wh = ws.get("website_health") or {}
    if wh.get("url") or wh.get("notes"):
        add("health", "website", "Website health",
            "Site: %s. Notes: %s. Last check: %s"
            % (wh.get("url", ""), wh.get("notes", ""), json.dumps(wh.get("last_check") or {})))

    # The client dashboard KPI export (opt-in source; None when unreadable).
    if dash_data:
        kpis = dash_data.get("kpis")
        if kpis:
            add("dash:kpis", "dashboard", "Dashboard KPIs", json.dumps(kpis))
        daily = dash_data.get("daily")
        if daily:
            add("dash:daily", "dashboard", "Dashboard daily performance",
                json.dumps(daily[-120:]))  # most recent ~4 months keeps the chunk sane

    return chunks


def fingerprint(ws, archives):
    """A cheap change-detector for the index: rebuild whenever any source moved."""
    import hashlib
    intel = ws.get("intel") or {}
    desc = {
        "watcher": [(ch.get("id"), ch.get("transcript_count"), ch.get("last_fetch"))
                    for ch, _v in archives or []],
        "intel": [len(intel.get("business_research") or []), len(intel.get("media_buying") or [])],
        "campaigns": [(c.get("id"), len(c.get("content") or [])) for c in ws.get("campaigns") or []],
        "metrics": ws.get("metrics"),
        "calendar": len(ws.get("calendar") or []),
        "conversations": [(c.get("id"), len(c.get("messages") or []))
                          for c in ws.get("conversations") or []],
    }
    return hashlib.md5(json.dumps(desc, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# --- 2. BM25 index --------------------------------------------------------------------------------
def build_index(chunks, fp=""):
    """A stored-JSON BM25 index: chunks + document frequencies + average length."""
    df = {}
    lengths = []
    for c in chunks:
        toks = set(_tokens(c["text"]))
        lengths.append(len(_tokens(c["text"])))
        for t in toks:
            df[t] = df.get(t, 0) + 1
    from workspace import now_iso
    return {"v": 1, "fingerprint": fp, "built_at": now_iso(), "chunks": chunks, "df": df,
            "avgdl": (sum(lengths) / len(lengths)) if lengths else 0.0}


def search(index, query, k=TOP_K, date_from="", date_to=""):
    """Top-k chunks for `query` by BM25. A date range filters DATED chunks (video/intel/content);
    undated chunks (metrics, campaigns, ...) always stay eligible."""
    chunks = index.get("chunks") or []
    df = index.get("df") or {}
    n_docs = len(chunks) or 1
    avgdl = index.get("avgdl") or 1.0
    q_terms = _tokens(query)
    if not q_terms:
        return []
    scored = []
    for c in chunks:
        date = c.get("date") or ""
        if date and ((date_from and date < date_from) or (date_to and date > date_to)):
            continue
        toks = _tokens(c["text"])
        if not toks:
            continue
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for t in q_terms:
            if t not in tf:
                continue
            idf = math.log((n_docs - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5) + 1.0)
            score += idf * (tf[t] * 2.5) / (tf[t] + 1.5 * (0.25 + 0.75 * len(toks) / avgdl))
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda sc: -sc[0])
    return [c for _s, c in scored[:k]]


# --- 3. Grounded answer ---------------------------------------------------------------------------
# The admin's detail control. Depth shapes retrieval width, provider thinking, and answer style;
# "deep" additionally query-plans (one extra model call) so comparative questions retrieve each
# entity's actual positions instead of just chunks containing the question's words.
DEPTHS = ("quick", "standard", "deep")
DEFAULT_DEPTH = "standard"

# Per-depth retrieval shape: (top-k per search query, overall excerpt cap).
_DEPTH_K = {"quick": (10, 10), "standard": (TOP_K, TOP_K), "deep": (12, 30)}

_DEPTH_STYLE = {
    "quick": ("Answer in 2-4 tight sentences: the direct answer and the headline numbers or names, "
              "nothing else."),
    "standard": ("Be direct and specific; a focused paragraph or two, with a short list when "
                 "comparing items."),
    "deep": ("Give a thorough, structured analysis. Synthesize ACROSS excerpts: compare positions, "
             "surface patterns and tensions, quote the key lines, and close with what it means for "
             "this client. Prefer short headed sections or bullet lists over one long wall of text."),
}


def _system_prompt(client_name, depth=DEFAULT_DEPTH):
    return (
        "You are the AGORA team's Atrium assistant for the client \"%s\". You answer questions "
        "using ONLY the numbered context excerpts provided — the client's campaigns, metrics, "
        "market intelligence, watched-creator video transcripts, and dashboard data. "
        "Quote numbers and names from the excerpts. When you use an excerpt, mention its source "
        "naturally (e.g. 'in Carson Reed's video ...', 'per the dashboard KPIs'). "
        "Comparative or analytical questions deserve real synthesis: when asked about "
        "disagreements, differences, or comparisons, contrast what each source emphasizes or "
        "recommends — two creators pushing different strategies (say, cold email vs paid ads) IS "
        "a disagreement worth reporting even if neither ever mentions the other. Make clear which "
        "part is stated in the excerpts and which part is your inference from them. If the "
        "excerpts truly contain nothing relevant, say so plainly — never invent facts. "
        % client_name
        + _DEPTH_STYLE.get(depth, _DEPTH_STYLE[DEFAULT_DEPTH])
        + " Answer with JSON only: {\"answer\": \"<your answer>\"}"
    )


_PLAN_SYSTEM = (
    "You write search queries for a keyword (BM25) index over a marketing client's workspace: "
    "watched-creator video transcripts, market intelligence, campaigns and content, metrics, and "
    "client conversations. Given the team's question, return JSON only: {\"queries\": [\"...\"]} "
    "— 2 to 5 short keyword queries that together cover every entity, topic, and angle the "
    "question needs. For comparative questions write one query per entity/stance (e.g. 'Nick "
    "Saraev lead generation advice' and 'Carson Reed client acquisition ads') plus one for the "
    "shared topic. Plain words only, no boolean syntax.")


def plan_queries(question, history, caller):
    """Deep mode's retrieval plan: extra search queries from the model. Never raises — any failure
    (model error, non-JSON, wrong shape) returns [] so deep degrades to single-query retrieval."""
    ctx = ""
    recent = [(t.get("text") or "")[:200] for t in (history or [])[-4:] if t.get("role") == "user"]
    if recent:
        ctx = "Earlier questions, for context: %s\n\n" % "; ".join(recent)
    try:
        raw, err = caller(_PLAN_SYSTEM, ctx + "Question: %s" % question)
        if err:
            return []
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.I)
        parsed = json.loads(raw, strict=False)
        qs = parsed.get("queries") if isinstance(parsed, dict) else None
        return [str(q).strip() for q in (qs or []) if str(q).strip()][:5]
    except Exception:
        return []


def _user_prompt(question, hits, history):
    lines = ["Context excerpts:"]
    used = 0
    for i, c in enumerate(hits, 1):
        body = c["text"]
        if used + len(body) > MAX_CONTEXT_CHARS:
            body = body[:max(0, MAX_CONTEXT_CHARS - used)]
        used += len(body)
        date = (" | " + c["date"]) if c.get("date") else ""
        lines.append("[%d] %s (%s%s)\n%s" % (i, c["title"], c["kind"], date, body))
        if used >= MAX_CONTEXT_CHARS:
            break
    if history:
        lines.append("\nRecent conversation:")
        for turn in history[-6:]:
            lines.append("%s: %s" % ("Team" if turn.get("role") == "user" else "Assistant",
                                     (turn.get("text") or "")[:600]))
    lines.append("\nQuestion: %s" % question)
    return "\n\n".join(lines)


def _scan_answer_string(raw):
    """Salvage the "answer" value out of a BROKEN JSON envelope, or "" if there is none.

    Walks the string literal character by character (honoring backslash escapes), so it survives
    garbage between the closing quote and the brace, a missing closing brace (output truncated at
    the token cap), and raw newlines inside the string. The collected literal is decoded with
    json.loads; if even that fails (e.g. truncated mid-escape) the common escapes are unescaped by
    hand — the goal is that the UI NEVER has to display a raw JSON blob."""
    m = re.search(r'"answer"\s*:\s*"', raw)
    if not m:
        return ""
    i, n, out = m.end(), len(raw), []
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            out.append(raw[i:i + 2])
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    literal = "".join(out)
    try:
        return str(json.loads('"%s"' % literal, strict=False)).strip()
    except ValueError:
        _esc = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
                "t": "\t"}
        return re.sub(r'\\(["\\/bfnrt])', lambda mm: _esc[mm.group(1)], literal).strip()


def _parse_answer(raw):
    """The model answers {"answer": ...}; parse leniently, salvage nearly-JSON, fall back to raw.

    The providers run in JSON mode yet still occasionally emit an envelope json.loads rejects —
    stray characters after the answer string, raw newlines/tabs inside it, or output cut at the
    token cap. Salvage progressively: strict parse (strict=False allows raw control chars in
    strings) → parse ignoring trailing junk → hand-scan the "answer" string literal."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.I)
    try:
        parsed = json.loads(raw, strict=False)
        if isinstance(parsed, dict) and parsed.get("answer"):
            return str(parsed["answer"])
    except ValueError:
        pass
    start = raw.find("{")
    if start >= 0:
        try:
            parsed, _end = json.JSONDecoder(strict=False).raw_decode(raw, start)
            if isinstance(parsed, dict) and parsed.get("answer"):
                return str(parsed["answer"])
        except ValueError:
            pass
        salvaged = _scan_answer_string(raw)
        if salvaged:
            return salvaged
    return raw


def ask(client_name, index, question, history=None, date_from="", date_to="", model=None,
        caller=None, usage_out=None, depth=DEFAULT_DEPTH):
    """Answer `question` from the workspace index. Returns (answer, sources, error).

    `depth` ('quick'|'standard'|'deep') is the admin's detail control: deep query-plans first (an
    extra model call), retrieves wider, turns provider thinking ON, and asks for a structured
    analysis; quick trims retrieval and asks for a few sentences. `sources` is the de-duplicated
    list of {title, kind, date, url} actually retrieved (shown as citation chips).
    `caller(system, user)` -> (text, error) is the LLM seam (used for BOTH the plan and the answer
    call in deep mode); the default uses the intel brain's provider plumbing with its default
    model. A `usage_out` dict is filled by the default caller with the SUMMED token counts of
    every call + the model id (the spend tally)."""
    depth = depth if depth in DEPTHS else DEFAULT_DEPTH
    if caller is None:
        import intel_ai
        mid = model or intel_ai.default_model()

        def _model_call(system, user, think):
            if not mid or not intel_ai.model_available(mid):
                return "", "no AI provider configured"
            u = {}
            text, err, _think = intel_ai._call(intel_ai.model_meta(mid), system, user,
                                               None, 8192, usage_out=u, think=think)
            if usage_out is not None:
                usage_out["model"] = mid
                for k in ("input_tokens", "output_tokens"):
                    usage_out[k] = usage_out.get(k, 0) + u.get(k, 0)
            return text, err

        def plan_call(system, user):
            return _model_call(system, user, False)   # planning is extraction — keep it fast

        def answer_call(system, user):
            return _model_call(system, user, depth == "deep")
    else:
        plan_call = answer_call = caller

    queries = [question]
    if depth == "deep":
        queries += [q for q in plan_queries(question, history or [], plan_call)
                    if q.lower() != question.lower()]
    k_each, cap = _DEPTH_K[depth]
    hits, seen_ids = [], set()
    for q in queries:
        for c in search(index, q, k=k_each, date_from=date_from, date_to=date_to):
            if c["id"] not in seen_ids:
                seen_ids.add(c["id"])
                hits.append(c)
    hits = hits[:cap]
    if not hits:
        return ("", [], "Nothing in this workspace matches that question — try rephrasing, or "
                        "fetch more data first.")
    sources, seen = [], set()
    for c in hits:
        key = c["title"]
        if key not in seen:
            seen.add(key)
            sources.append({"title": c["title"], "kind": c["kind"],
                            "date": c.get("date", ""), "url": c.get("url", "")})
    raw, err = answer_call(_system_prompt(client_name, depth),
                           _user_prompt(question, hits, history or []))
    if err:
        return "", sources, err
    answer = _parse_answer(raw)
    if not answer:
        return "", sources, "The model returned an empty answer — try again."
    return answer, sources, ""


# --- Optional source: the client's dashboard data export ------------------------------------------
def read_client_dash_data(client):
    """The per-client dashboard JSON (`<c>.json` in agora-data-driven-<c>-dash), or None.

    Opt-in: the portal SA needs objectViewer on that bucket (enable_assistant_dash_data.ps1).
    Any failure — no bucket (portal-only client), no permission, bad JSON — returns None."""
    try:
        from google.cloud import storage  # lazy
        blob = storage.Client().bucket("agora-data-driven-%s-dash" % client).blob("%s.json" % client)
        return json.loads(blob.download_as_bytes().decode("utf-8"))
    except Exception:
        return None
