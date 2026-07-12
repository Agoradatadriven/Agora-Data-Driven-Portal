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
     so the UI can show citation chips.

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
def _system_prompt(client_name):
    return (
        "You are the AGORA team's Atrium assistant for the client \"%s\". You answer questions "
        "using ONLY the numbered context excerpts provided — the client's campaigns, metrics, "
        "market intelligence, watched-creator video transcripts, and dashboard data. "
        "Be direct and specific; quote numbers and names from the excerpts. When you use an "
        "excerpt, mention its source naturally (e.g. 'in Carson Reed's video ...', 'per the "
        "dashboard KPIs'). If the excerpts don't contain the answer, say so plainly — never "
        "invent facts. Answer with JSON only: {\"answer\": \"<your answer>\"}" % client_name
    )


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


def _parse_answer(raw):
    """The model answers {"answer": ...}; parse leniently, fall back to the raw text."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.I)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("answer"):
            return str(parsed["answer"])
    except ValueError:
        pass
    return raw


def ask(client_name, index, question, history=None, date_from="", date_to="", model=None,
        caller=None):
    """Answer `question` from the workspace index. Returns (answer, sources, error).

    `sources` is the de-duplicated list of {title, kind, date, url} actually retrieved (shown as
    citation chips). `caller(system, user)` -> (text, error) is the LLM seam; the default uses the
    intel brain's provider plumbing with its default model."""
    hits = search(index, question, date_from=date_from, date_to=date_to)
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
    if caller is None:
        import intel_ai

        def caller(system, user):
            mid = model or intel_ai.default_model()
            if not mid or not intel_ai.model_available(mid):
                return "", "no AI provider configured"
            text, err, _think = intel_ai._call(intel_ai.model_meta(mid), system, user,
                                               None, 8192)
            return text, err
    raw, err = caller(_system_prompt(client_name), _user_prompt(question, hits, history or []))
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
