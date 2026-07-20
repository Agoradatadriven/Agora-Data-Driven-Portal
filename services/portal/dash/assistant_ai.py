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
     `embed_index` OPTIONALLY augments it with a SEMANTIC leg: every chunk is embedded once (Vertex
     text-embedding-005, via an injected embedder) and the unit vectors are packed compactly into
     the same index object -- so retrieval is HYBRID (keyword + meaning) when embeddings are wired,
     and pure BM25 (unchanged) when they are not.
  3. `ask` runs HYBRID retrieval and answers with the SAME provider plumbing as the intel brain
     (`intel_ai._call`, default model -- Vertex Gemini when configured):
       * metadata PRE-FILTER -- an unambiguous single-source question ("how are we doing on email?")
         scopes retrieval to that kind before scoring; an optional date range scopes dated sources.
       * per query, a BM25 ranking AND (when embedded) a cosine-similarity ranking;
       * those rankings are fused with RECIPROCAL RANK FUSION (rank-only, so incompatible BM25 and
         cosine scales never fight) into one candidate pool;
       * the pool is (optionally) RE-RANKED by a cross-encoder (Vertex Ranking API, via an injected
         reranker) -- retrieve wide, then keep the truly-relevant few;
       * the survivors are packed into a grounded prompt; the model answers ONLY from the excerpts
         and names its sources (returned so the UI can show citation chips).
     The admin's DEPTH control ('quick'|'standard'|'deep') shapes it: deep first asks the model to
     PLAN extra search queries (so a comparative question retrieves each entity's actual positions),
     retrieves wider, turns provider thinking ON, and asks for a structured analysis; quick keeps it
     to a few sentences. Every depth is allowed to SYNTHESIZE across excerpts -- differing
     recommendations count as disagreement even when nobody names the other.

Pure + testable: chunking/indexing/BM25/RRF/fusion are dependency-free; the semantic leg, query
embedding, and rerank are all INJECTED (an `embedder`, `query_embedder`, `reranker`), and `ask`
also accepts a `caller` injection, so tests run with no network and a default deploy with no
embeddings behaves exactly like the old BM25-only path. Every failure degrades to
(\"\", sources, reason) -- never a raise.
"""

import base64
import hashlib
import json
import math
import re
import struct

# Small, boring stopword list -- enough to keep BM25 focused without a dependency.
_STOP = frozenset(
    "a an and are as at be but by for from has have how i if in into is it its me my not of on or "
    "our so that the their them they this to was we what when where which who why will with you "
    "your".split())

CHUNK_WORDS = 1000          # transcript chunk size (words)
TOP_K = 18                  # excerpts handed to the model per question
MAX_CONTEXT_CHARS = 90000   # hard cap on packed context (stays well inside Gemini's window)
INDEX_VERSION = 3           # bump to force a one-time rebuild when the index SHAPE changes
                            # (v3: titles are indexed/embedded, so retrieval finds entities by name)


def _tokens(text):
    """Lowercase word tokens minus stopwords (the BM25 vocabulary)."""
    return [t for t in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(t) > 1 and t not in _STOP]


def _searchable(chunk):
    """What BM25/embeddings actually index for a chunk: its TITLE plus its body.

    The title carries the ENTITY NAME a user searches by -- the creator/channel name ("Fuel Your
    Wander"), the video title, the campaign name, the email subject -- and that name is usually
    ABSENT from the body (a transcript rarely says the channel's own name). Indexing the title too
    is what lets "what would Fuel Your Wander say about ..." retrieve that creator's transcripts;
    without it the name is invisible to retrieval and the Assistant reports it has no such content."""
    title = (chunk.get("title") or "").strip()
    text = chunk.get("text") or ""
    return (title + "\n" + text) if title else text


# --- 1. Flatten the workspace into chunks ---------------------------------------------------------
def build_chunks(ws, archives, dash_data=None, mail_threads=None):
    """Every source in the workspace as a flat list of {id, kind, title, url, date, text} chunks.

    `archives` is [(channel_entry, videos), ...] -- the Watcher registry entries with their video
    lists (loaded by the caller so this stays I/O-free). `dash_data` is the optional client
    dashboard JSON (None when the bucket isn't readable). `mail_threads` is the optional list of
    loaded Mail thread archives (subject + full messages), so the chat can answer over the
    client's actual email correspondence too."""
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

    # Client email threads (the Mail tab's archive), chunked like transcripts so one long thread
    # can yield several retrievable excerpts. The summary rides along for cheap high-level hits.
    snapshot = []
    for t in mail_threads or []:
        head = "Email thread with %s" % (", ".join(t.get("participants") or [])[:200] or "the client")
        st = t.get("stats") or {}
        snapshot.append("%s | last message %s | awaiting AGORA reply: %s | avg AGORA reply time: %s"
                        % (t.get("subject", "(no subject)"), (t.get("last_date") or "?")[:10],
                           "YES" if st.get("awaiting_reply") else "no",
                           ("%s hours" % st["avg_response_hours"])
                           if isinstance(st.get("avg_response_hours"), (int, float)) else "n/a"))
        body_lines = ["Subject: %s" % t.get("subject", "")]
        if st.get("awaiting_reply"):
            body_lines.append("Status: the last word is the client's -- an AGORA reply is due.")
        if t.get("summary"):
            body_lines.append("Summary: %s" % t.get("summary"))
        for m in t.get("messages") or []:
            body_lines.append("From %s on %s: %s" % (m.get("from", ""), (m.get("date") or "")[:10],
                                                     m.get("body", "")))
        words = "\n".join(body_lines).split()
        for i in range(0, len(words), CHUNK_WORDS):
            add("mail:%s:%d" % (t.get("id", ""), i // CHUNK_WORDS), "email",
                "%s — %s" % (head, t.get("subject", "")),
                " ".join(words[i:i + CHUNK_WORDS]), date=(t.get("last_date") or "")[:10])
    # One computed responsiveness snapshot across ALL threads, so "how well are we handling this
    # client's email?" retrieves real numbers (reply speed, threads left hanging) in one hit.
    if snapshot:
        add("mail:responsiveness", "email",
            "Email responsiveness snapshot (reply speed, who owes whom)", "\n".join(snapshot))

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
        "mail": [(t.get("id"), t.get("message_count"), t.get("last_date"))
                 for t in ((ws.get("mail") or {}).get("threads") or [])],
    }
    return hashlib.md5(json.dumps(desc, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# --- 2. BM25 index --------------------------------------------------------------------------------
def build_index(chunks, fp=""):
    """A stored-JSON BM25 index: chunks + document frequencies + average length.

    Pure + dependency-free (no network). The SEMANTIC leg (per-chunk vectors) is attached SEPARATELY
    by `embed_index` so this stays testable off-cloud and a no-embeddings deploy is unchanged."""
    df = {}
    lengths = []
    for c in chunks:
        toks = _tokens(_searchable(c))
        lengths.append(len(toks))
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    from workspace import now_iso
    return {"v": INDEX_VERSION, "fingerprint": fp, "built_at": now_iso(), "chunks": chunks, "df": df,
            "n_docs": len(chunks),
            "avgdl": (sum(lengths) / len(lengths)) if lengths else 0.0}


# --- 2b. Semantic leg: embed chunks + pack vectors compactly into the index -----------------------
# Vectors are unit-normalised and packed as little-endian float16 (`struct` 'e') then base64'd, so
# cosine similarity at query time is a plain dot product and the whole vector store stays small
# (256 dims -> ~512 bytes/chunk -> ~0.7 KB base64) even for a client with thousands of chunks. All
# stdlib -- no numpy, no vector DB.
def _normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _pack_vec(vec):
    """Unit-normalise then pack a float vector to a compact base64 float16 string."""
    v = _normalize(vec)
    return base64.b64encode(struct.pack("<%de" % len(v), *v)).decode("ascii")


def _unpack_vec(b64, dim):
    """Decode a packed vector back to a list[float] of length `dim`, or None if it can't."""
    try:
        return list(struct.unpack("<%de" % dim, base64.b64decode(b64)))
    except Exception:
        return None


def _emb_sig(chunk):
    """A short content signature for a chunk's searchable text. Used to decide whether a carried-over
    vector is still valid: a chunk id can be REUSED with new content (e.g. the 'metrics' snapshot
    changes every refresh, a transcript chunk is re-fetched), so reuse must key on content, not id."""
    return hashlib.md5(_searchable(chunk).encode("utf-8")).hexdigest()


def embed_index(index, embedder, prev=None):
    """Attach a semantic leg to `index` IN PLACE: embed every chunk's text and store packed vectors.

    INCREMENTAL: when `prev` (this client's previous index) carries a semantic leg, any chunk whose id
    AND content signature are unchanged REUSES its stored vector instead of re-embedding. So a Watcher
    fetch that adds a few transcripts embeds only the handful of new chunks, not the whole corpus --
    the fix for the "was fast, now unusable" stall where every fetch re-embedded thousands of chunks
    synchronously inside the ask request. With no `prev` (or a `prev` without embeddings) this embeds
    everything, exactly as before.

    `embedder(list_of_texts) -> (list_of_vectors, error)` is injected (the default caller wires it to
    intel_ai.embed_texts). A per-chunk vector may be None (its batch failed) -- those chunks just
    miss the semantic leg (BM25 still covers them). Any total failure leaves the index BM25-only.
    Returns the same index dict (so callers can `index = embed_index(index, fn)`)."""
    chunks = index.get("chunks") or []
    if not chunks or embedder is None:
        return index
    prev = prev or {}
    prev_emb = prev.get("emb") or {}
    prev_sig = prev.get("emb_sig") or {}
    prev_dim = prev.get("emb_dim") or 0

    emb, sig, dim = {}, {}, 0
    to_embed = []                     # (chunk_id, searchable_text) for the chunks that actually need it
    for c in chunks:
        cid = c["id"]
        s = _emb_sig(c)
        packed = prev_emb.get(cid)
        if packed is not None and prev_dim and prev_sig.get(cid) == s:
            emb[cid] = packed         # unchanged -> reuse the stored vector, skip the embed call
            sig[cid] = s
            dim = dim or prev_dim
        else:
            to_embed.append((cid, _searchable(c), s))

    if to_embed:
        try:
            vectors, _err = embedder([t for _cid, t, _s in to_embed])
        except Exception:
            vectors = None
        for (cid, _t, s), v in zip(to_embed, vectors or []):
            if not v:
                continue
            dim = dim or len(v)
            emb[cid] = _pack_vec(v)
            sig[cid] = s

    if emb:
        index["emb"] = emb
        index["emb_sig"] = sig        # per-embedded-chunk content signature (powers the next reuse)
        index["emb_dim"] = dim
        index["emb_count"] = len(emb)
    return index


def has_embeddings(index):
    """True iff `index` carries a semantic leg (so the ask path should run HYBRID retrieval)."""
    return bool((index or {}).get("emb")) and bool((index or {}).get("emb_dim"))


# --- 2c. Metadata filtering -----------------------------------------------------------------------
# All source kinds the chunker emits (used to validate an inferred single-source filter).
_KINDS = {"video", "intel", "campaign", "content", "metrics", "calendar", "conversation",
          "website", "email", "dashboard"}

# Conservative source inference: a phrase group -> the chunk kinds it means. Pre-filtering is
# POWERFUL but dangerous (wrongly excluding relevant chunks), so we only ever apply it when EXACTLY
# ONE group matches the question (an unambiguous single-source ask like "how are we handling email?")
# -- a cross-source question ("campaigns vs what creators say") matches several groups and stays
# unfiltered. `ask` also relaxes the filter if it would leave nothing eligible.
_KIND_HINTS = (
    (("email", "inbox", "reply", "replied", "responsiveness", "correspond"), {"email"}),
    (("transcript", "video", "youtube", "creator", "episode", "watched"), {"video"}),
    (("market intelligence", "industry news", "competitor news", "briefing", "the news"), {"intel"}),
    (("campaign", "content piece", "ad copy", "creative", "caption"), {"content", "campaign"}),
    (("dashboard", "kpi", "roas", "cpl", "cost per lead", "spend"), {"dashboard", "metrics"}),
)


def _infer_kinds(question):
    """A confident single-source scope for `question`, or None to search every source.

    Returns a set of chunk kinds only when EXACTLY ONE hint group matches; otherwise None (so a
    multi-source or generic question is never over-filtered)."""
    ql = (question or "").lower()
    matched = [kinds for phrases, kinds in _KIND_HINTS if any(p in ql for p in phrases)]
    return matched[0] if len(matched) == 1 else None


def _creator_names(chunks):
    """The lowercased channel/creator names present in the index (the part of a video chunk's title
    before the ' — <video title>' separator). Cheap; derived from the chunks already in hand."""
    names = set()
    for c in chunks:
        if c.get("kind") == "video":
            name = (c.get("title") or "").split(" — ", 1)[0].strip().lower()
            if name:
                names.add(name)
    return names


def _question_names_creator(question, chunks):
    """True if `question` mentions a creator/channel this workspace actually watches.

    This is why "what would Fuel Your Wander say about the Colorado Escape Campaign" must NOT be
    scoped to campaign-only: it names a creator, so it is inherently cross-source. `_infer_kinds`
    can't know that (it only matches the literal word "creator"), but the watched channels' names
    are right here in the index."""
    ql = (question or "").lower()
    return any(name in ql for name in _creator_names(chunks))


def _passes(chunk, date_from, date_to, kinds):
    """Metadata gate: an optional kind scope + a date range (dated chunks only; undated always pass)."""
    if kinds is not None and chunk.get("kind") not in kinds:
        return False
    date = chunk.get("date") or ""
    if date and ((date_from and date < date_from) or (date_to and date > date_to)):
        return False
    return True


# --- 2d. The retriever: BM25 + cosine, both over the SAME in-memory prep (built once per ask) ------
# The old search re-tokenised the WHOLE corpus on EVERY query -- crippling for deep mode's multi-query
# retrieval. This tokenises once per ask, then every query (and every RRF leg) is cheap dict work.
class _Retriever:
    """One-shot retrieval helper over a stored index. Tokenises + decodes vectors LAZILY and ONCE,
    then serves any number of BM25 / cosine queries. `allowed` is the pre-filtered chunk-index set."""

    def __init__(self, index):
        self.index = index or {}
        self.chunks = self.index.get("chunks") or []
        self.n = len(self.chunks)
        self._tf = None       # per-chunk {term: count}
        self._dl = None       # per-chunk token length
        self._df = dict(self.index.get("df") or {})
        self._avgdl = self.index.get("avgdl") or 0.0
        self._emb = None      # {chunk_idx: unit vector}
        self._emb_dim = self.index.get("emb_dim") or 0

    def _ensure_bm25(self):
        if self._tf is not None:
            return
        self._tf, self._dl = [], []
        for c in self.chunks:
            toks = _tokens(_searchable(c))
            tf = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self._tf.append(tf)
            self._dl.append(len(toks))
        if not self._avgdl:
            self._avgdl = (sum(self._dl) / self.n) if self.n else 1.0
        if not self._df:
            for tf in self._tf:
                for t in tf:
                    self._df[t] = self._df.get(t, 0) + 1

    def _ensure_emb(self):
        if self._emb is not None:
            return
        self._emb = {}
        raw = self.index.get("emb") or {}
        if not raw or not self._emb_dim:
            return
        idx_of = {c.get("id"): i for i, c in enumerate(self.chunks)}
        for cid, b64 in raw.items():
            i = idx_of.get(cid)
            if i is None:
                continue
            v = _unpack_vec(b64, self._emb_dim)
            if v:
                self._emb[i] = v

    def bm25(self, query, allowed):
        """Chunk indices in `allowed`, ranked by BM25 for `query` (descending, positive scores only)."""
        self._ensure_bm25()
        q_terms = _tokens(query)
        if not q_terms:
            return []
        n_docs = self.n or 1
        avgdl = self._avgdl or 1.0
        scored = []
        for i in allowed:
            tf = self._tf[i]
            dl = self._dl[i]
            if not dl:
                continue
            score = 0.0
            for t in q_terms:
                f = tf.get(t)
                if not f:
                    continue
                df = self._df.get(t, 0)
                idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
                score += idf * (f * 2.5) / (f + 1.5 * (0.25 + 0.75 * dl / avgdl))
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda s: -s[0])
        return [i for _s, i in scored]

    def cosine(self, qvec, allowed):
        """Chunk indices in `allowed` that HAVE a vector, ranked by cosine similarity to `qvec`.
        Stored vectors are unit-normalised, so cosine is a dot product against the normalised query."""
        self._ensure_emb()
        if not self._emb or not qvec:
            return []
        q = _normalize(qvec)
        scored = []
        for i in allowed:
            v = self._emb.get(i)
            if v is None:
                continue
            scored.append((sum(a * b for a, b in zip(q, v)), i))
        scored.sort(key=lambda s: -s[0])
        return [i for _s, i in scored]


def _rrf(rankings, k=60, limit=None):
    """Reciprocal Rank Fusion of several ranked index-lists into one. Rank-only (score scales never
    fight): each list contributes 1/(k + rank) to an item's fused score. Returns fused indices desc."""
    scores = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    fused = sorted(scores, key=lambda i: -scores[i])
    return fused[:limit] if limit else fused


# BM25-only top-k (kept for callers/tests that want a single lexical ranking without the full ask
# pipeline). A date range filters DATED chunks; `kinds` optionally scopes by source kind.
def search(index, query, k=TOP_K, date_from="", date_to="", kinds=None):
    """Top-k chunks for `query` by BM25, honouring the metadata filter (kind + date range)."""
    chunks = index.get("chunks") or []
    allowed = [i for i, c in enumerate(chunks) if _passes(c, date_from, date_to, kinds)]
    idxs = _Retriever(index).bm25(query, allowed)
    return [chunks[i] for i in idxs[:k]]


# --- 3. Grounded answer ---------------------------------------------------------------------------
# The admin's detail control. Depth shapes retrieval width, provider thinking, and answer style;
# "deep" additionally query-plans (one extra model call) so comparative questions retrieve each
# entity's actual positions instead of just chunks containing the question's words.
DEPTHS = ("quick", "standard", "deep")
DEFAULT_DEPTH = "standard"

# Per-depth retrieval shape: (RRF candidate pool BEFORE rerank, FINAL excerpts handed to the model).
# "Retrieve wide, keep few": the pool is intentionally large (the reranker, when on, sorts it by true
# relevance); the final cap keeps the prompt focused. Without a reranker the top `final` of the fused
# pool are used directly. deep multi-queries + widest pool; quick is lean end-to-end.
_DEPTH_RETRIEVE = {"quick": (25, 8), "standard": (45, TOP_K), "deep": (60, 24)}

_DEPTH_STYLE = {
    "quick": ("Answer in 2-4 tight sentences: the direct answer and the headline numbers or names, "
              "nothing else."),
    "standard": ("Be direct and specific; a focused paragraph or two, with a short list when "
                 "comparing items."),
    "deep": ("Give a thorough, structured analysis. Synthesize ACROSS excerpts: compare positions, "
             "surface patterns and tensions, quote the key lines, and close with what it means for "
             "this client. Prefer short headed sections or bullet lists over one long wall of text."),
}


def _system_prompt(client_name, depth=DEFAULT_DEPTH, as_json=True):
    """The grounded-answer contract. `as_json` picks the OUTPUT shape: True = the `{"answer": ...}`
    envelope the synchronous path parses; False = PLAIN markdown for the STREAMING path (a JSON
    wrapper can't be streamed to the user without showing braces)."""
    tail = (" Answer with JSON only: {\"answer\": \"<your answer>\"}" if as_json else
            " Answer in clear GitHub-flavored markdown (headings, bold, and lists are welcome). Do "
            "NOT wrap your answer in JSON or code fences — just write the answer.")
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
        + tail
    )


def _steer_note(steer):
    """A short prompt suffix carrying the team's mid-flight steer (from the plan checkpoint or a
    pause). Empty when there is none."""
    steer = (steer or "").strip()
    if not steer:
        return ""
    return ("\n\nIMPORTANT — the AGORA team reviewed your approach and added this guidance. Follow "
            "it, and let it override your earlier direction where they conflict:\n%s" % steer[:1000])


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


def _retrieve(index, question, queries, depth, date_from, date_to, query_embedder, reranker):
    """The HYBRID retrieval pipeline. Returns the final list of chunk dicts (best first).

      1. metadata PRE-FILTER (kind scope + date range), relaxing the kind scope if it empties the set;
      2. per query: a BM25 ranking + (when embedded + a query vector) a cosine ranking;
      3. RRF-fuse every ranking into one candidate pool (rank-only -- BM25 and cosine scales can't
         fight), capped to the depth's pool size;
      4. optionally cross-encoder RE-RANK the pool (retrieve wide, keep few), else take the fused top.
    `query_embedder(q) -> (vec, err)` and `reranker(query, records, top_n) -> (records, err)` are
    injected; both are optional and every failure degrades to the lexical/fused result."""
    chunks = index.get("chunks") or []
    pool_cap, final = _DEPTH_RETRIEVE.get(depth, _DEPTH_RETRIEVE[DEFAULT_DEPTH])

    kinds = _infer_kinds(question)
    # A named creator makes the question cross-source: keep the watched-video chunks in scope so
    # "what would <creator> say about <campaign>" retrieves the creator's transcripts, not just the
    # campaign (a lone 'campaign' keyword otherwise filters every transcript out before scoring).
    if kinds is not None and "video" not in kinds and _question_names_creator(question, chunks):
        kinds = set(kinds) | {"video"}
    allowed = [i for i, c in enumerate(chunks) if _passes(c, date_from, date_to, kinds)]
    if not allowed and kinds is not None:                 # scope too tight -> drop the kind filter
        allowed = [i for i, c in enumerate(chunks) if _passes(c, date_from, date_to, None)]
    if not allowed:
        return []

    retr = _Retriever(index)
    hybrid = has_embeddings(index) and query_embedder is not None
    rankings = []
    for q in queries:
        rankings.append(retr.bm25(q, allowed)[:100])
        if hybrid:
            qvec, _verr = query_embedder(q)
            if qvec:
                rankings.append(retr.cosine(qvec, allowed)[:100])
    fused = _rrf(rankings, limit=pool_cap)
    if not fused:
        return []

    if reranker is not None and len(fused) > 1:
        records = [{"id": str(i), "title": chunks[i].get("title", ""),
                    "content": chunks[i].get("text", "")} for i in fused]
        ranked, _rerr = reranker(question, records, final)
        order = []
        for r in ranked:
            try:
                order.append(int(r["id"]))
            except (TypeError, ValueError, KeyError):
                pass
        fused = order or fused
    return [chunks[i] for i in fused[:final]]


def ask(client_name, index, question, history=None, date_from="", date_to="", model=None,
        caller=None, usage_out=None, depth=DEFAULT_DEPTH, query_embedder=None, reranker=None):
    """Answer `question` from the workspace index with HYBRID retrieval. Returns (answer, sources, error).

    `depth` ('quick'|'standard'|'deep') is the admin's detail control: deep query-plans first (an
    extra model call), retrieves wider, turns provider thinking ON, and asks for a structured
    analysis; quick trims retrieval and asks for a few sentences. `sources` is the de-duplicated
    list of {title, kind, date, url} actually retrieved (shown as citation chips).
    `caller(system, user)` -> (text, error) is the LLM seam (used for BOTH the plan and the answer
    call in deep mode); the default uses the intel brain's provider plumbing with its default
    model. A `usage_out` dict is filled by the default caller with the SUMMED token counts of
    every call + the model id (the spend tally).
    `query_embedder(q) -> (vec, err)` adds the SEMANTIC leg (fused with BM25 via RRF) when the index
    was embedded; `reranker(query, records, top_n) -> (records, err)` adds a cross-encoder rerank of
    the candidate pool. Both are optional -- omit them (or leave the index unembedded) and this is
    exactly the old BM25 path. Neither ever raises: a failure degrades to lexical/fused retrieval."""
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

    hits = _retrieve(index, question, queries, depth, date_from, date_to, query_embedder, reranker)
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


# --- Streaming ask + plan checkpoint --------------------------------------------------------------
# The chat UI streams: it shows the model's reasoning live and lets the team PAUSE mid-thinking to
# steer. `plan_stage` is the optional pre-answer checkpoint (Claude-style "plan mode"): it does the
# retrieval + (deep) query planning and returns what the assistant WILL look at, so the team can
# approve or redirect BEFORE a single answer token is written. `ask_stream` then streams the answer
# (reasoning first, then the reply), honouring any `steer` the team added at the checkpoint or a pause.
def _build_queries(question, depth, history, plan_caller):
    """The retrieval queries for `question`: just the question, plus (deep) the planned sub-queries."""
    queries = [question]
    if depth == "deep" and plan_caller is not None:
        queries += [q for q in plan_queries(question, history or [], plan_caller)
                    if q.lower() != question.lower()]
    return queries


def _sources_of(hits):
    """The de-duplicated citation list (by title) for a set of retrieved chunks."""
    sources, seen = [], set()
    for c in hits:
        key = c["title"]
        if key not in seen:
            seen.add(key)
            sources.append({"title": c["title"], "kind": c["kind"],
                            "date": c.get("date", ""), "url": c.get("url", "")})
    return sources


def plan_stage(index, question, history=None, depth=DEFAULT_DEPTH, date_from="", date_to="",
               query_embedder=None, reranker=None, plan_caller=None):
    """The plan-mode checkpoint. Returns (queries, sources): the sub-questions the assistant will
    search and the sources it retrieved -- shown to the team to approve or steer BEFORE answering.
    Never raises; an empty `sources` means nothing matched (the UI says so)."""
    depth = depth if depth in DEPTHS else DEFAULT_DEPTH
    queries = _build_queries(question, depth, history, plan_caller)
    hits = _retrieve(index, question, queries, depth, date_from, date_to, query_embedder, reranker)
    return queries, _sources_of(hits)


def ask_stream(client_name, index, question, history=None, date_from="", date_to="",
               depth=DEFAULT_DEPTH, steer="", query_embedder=None, reranker=None,
               plan_caller=None, stream_caller=None):
    """Stream an answer to `question`. A GENERATOR yielding event dicts (mirrors intel_ai.stream_call):
      {"type":"sources","sources":[...]}         -- retrieved citations (emitted first)
      {"type":"thinking","text":<delta>}         -- reasoning delta (the live think panel)
      {"type":"answer","text":<delta>}           -- answer delta (plain markdown)
      {"type":"usage", ...}                       -- token counts (from the provider, near the end)
      {"type":"error","message":<reason>}        -- a short reason; the stream then ends

    `steer` (from the plan checkpoint or a pause-and-restart) is injected so the answer follows the
    team's redirection. `stream_caller(system, user) -> iterator of intel_ai stream events` is the
    injected model seam (tests pass a fake); `plan_caller(system,user)->(text,err)` powers deep's
    query planning. Retrieval reuses the hybrid pipeline (query_embedder/reranker optional)."""
    depth = depth if depth in DEPTHS else DEFAULT_DEPTH
    queries = _build_queries(question, depth, history, plan_caller)
    hits = _retrieve(index, question, queries, depth, date_from, date_to, query_embedder, reranker)
    if not hits:
        yield {"type": "error", "message": ("Nothing in this workspace matches that question — try "
                                            "rephrasing, or fetch more data first.")}
        return
    yield {"type": "sources", "sources": _sources_of(hits)}
    if stream_caller is None:
        yield {"type": "error", "message": "no streaming model configured"}
        return
    system = _system_prompt(client_name, depth, as_json=False)
    user = _user_prompt(question, hits, history or []) + _steer_note(steer)
    got_answer = False
    for ev in stream_caller(system, user):
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "answer" and ev.get("text"):
            got_answer = True
        yield ev
    if not got_answer:
        yield {"type": "error", "message": "The model returned an empty answer — try again."}


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
