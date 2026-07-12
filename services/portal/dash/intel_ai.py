"""AI 'brain' for Market Intelligence -- pick a model, curate REAL news into client briefings.

Gated + graceful, mirroring feedback_ai.py and intel_feed.py. Uses `requests` directly (already
pinned) against each provider's REST endpoint -- there is NO SDK dependency and an unconfigured
provider is simply unavailable (never an import error). Two providers are supported, chosen per
client by the team from inside the workspace (the model dropdown):

  * gemini    -- **Vertex AI** generateContent (`{loc}-aiplatform.googleapis.com`). Models:
                 gemini-2.5-pro, gemini-2.5-flash. Auth = the Cloud Run runtime SA's OAuth token
                 (metadata server), NOT an API key -- so it bills the GCP project directly (one card,
                 one invoice), unlike an AI-Studio key on separate prepaid credits. Gated on
                 VERTEX_GEMINI_ENABLED=1 (the deploy sets it once the SA has roles/aiplatform.user).
  * deepseek  -- OpenAI-compatible /chat/completions (api.deepseek.com). Models: deepseek-v4-pro,
                 deepseek-v4-flash. Needs DEEPSEEK_API_KEY (the same Secret-Manager secret + model
                 ids mastery-engine's lib/deepseek.js uses).

Every model call returns `(text, error)`: on failure `error` is a SHORT human reason (e.g. "out of
quota/credits", "auth failed") so the tab can show WHY instead of silently filling with junk. There
is NO news-feed fallback -- if the model can't run, the team sees the error and fixes it.

RESEARCH METHOD = RETRIEVE-THEN-CURATE. intel_refresh fetches REAL candidate articles first (keyless
Google News RSS, via intel_feed -- a 12-month window on the first run, a short recent window every
day after), then `curate()` hands those candidates to the selected model. The model SELECTS the most
relevant items for THIS client, writes a clean 1-2 sentence client-facing briefing, and returns them
mapped back to the REAL link + source + date of the chosen candidate. The model never invents a URL:
an entry it returns that doesn't point at a real candidate is dropped. Any failure (no key, bad
model, network/parse error) returns None so the caller falls back to the plain-RSS behaviour -- the
tab always fills.

The two per-section instruction prompts are ADMIN-TUNABLE (stored in the workspace); when an admin
leaves one blank the module default below is used. `_call_*` take an injectable `fetcher` so the
whole pipeline runs with no network in tests (see _intel_ai_localtest.py).
"""

import json
import os
import re

# --- The models offered in the dropdown ---------------------------------------------------------
# Order = the order the dropdown shows them. `provider` maps to the transport + env key below.
# Flash first (fast + cheap) so the recommended default sits at the top.
MODELS = (
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "gemini"},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "gemini"},
    {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash", "provider": "deepseek"},
    {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "provider": "deepseek"},
)

# Admin-choosable search look-back (how far back to pull candidate articles). The value is the
# Google-News `when:` operator suffix; publisher feeds ignore it and just return their latest.
WINDOWS = (
    {"value": "7d", "label": "Past week"},
    {"value": "30d", "label": "Past month"},
    {"value": "3m", "label": "Past 3 months"},
    {"value": "6m", "label": "Past 6 months"},
    {"value": "12m", "label": "Past 12 months"},
)
DEFAULT_WINDOW = "3m"
DEFAULT_COUNT = 8            # target articles the model selects per section per run
MIN_COUNT, MAX_COUNT = 1, 25


def valid_window(value):
    """True iff `value` is one of the offered look-back windows."""
    return any(o["value"] == value for o in WINDOWS)


def window_label(value):
    """The human label for a look-back window value ('12m' -> 'Past 12 months'), else 'recent'."""
    for o in WINDOWS:
        if o["value"] == value:
            return o["label"]
    return "recent"


def window_of(cfg):
    """The configured look-back window for a client's intel_ai config (validated; default 3m)."""
    w = ((cfg or {}).get("window") or "").strip()
    return w if valid_window(w) else DEFAULT_WINDOW


def count_of(cfg):
    """The configured article target per section (int, clamped MIN..MAX; default 8)."""
    try:
        n = int((cfg or {}).get("count") or DEFAULT_COUNT)
    except (TypeError, ValueError):
        n = DEFAULT_COUNT
    return max(MIN_COUNT, min(n, MAX_COUNT))


_DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
_TIMEOUT = 60          # seconds; a slow model must never hang the whole refresh run.
# Grounded research is a MUCH slower call: the model plans, runs several live Google searches, reads
# real pages, then curates -- and with the "show reasoning" toggle on (includeThoughts) it is slower
# still. 60s reliably times out (ReadTimeout); give it its own generous budget. Both sections research
# concurrently, so wall time stays ~one call -- under the web service's 300s and the job's 900s caps.
# Overridable via env for tuning without a code change.
try:
    _RESEARCH_TIMEOUT = int(os.environ.get("INTEL_RESEARCH_TIMEOUT", "240"))
except (TypeError, ValueError):
    _RESEARCH_TIMEOUT = 240
_BODY_MAX = 320        # a briefing summary is short by design (matches intel_refresh._BODY_MAX-ish).

# Vertex AI (GCP-billed Gemini). Project/location come from env (the deploy sets them); the token is
# the runtime SA's, fetched from the metadata server -- no API key. `global` and a regional location
# both work; we default to the project's region so data stays in-region.
_VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "agora-data-driven"
# `global` serves the widest model set -- gemini-2.5-pro is NOT available in asia-southeast1 (404),
# but pro AND flash both work at `global`. (Only public news + the client's keywords go to Vertex;
# the workspace itself stays in-region in GCS.)
_VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")
_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)

# --- The default, admin-tunable editorial prompts (one per section) ------------------------------
# These are the "prompt engineering" an admin can override per client in the workspace. They are
# EDITORIAL GUIDANCE injected into the fixed curation contract in _system_prompt -- they steer WHAT
# to pick and HOW to frame it, never the output format (that stays locked so parsing is reliable).
DEFAULT_BUSINESS_PROMPT = (
    "Focus on news that genuinely affects THIS client's industry and their customers: competitor "
    "moves, market and demand trends, pricing shifts, regulation, and changes in how their customers "
    "buy or behave. Prefer concrete, recent, decision-relevant developments over evergreen "
    "explainers or generic 'top tips' listicles. Skip anything not clearly tied to their world."
)
DEFAULT_MEDIA_PROMPT = (
    "Focus on changes to the major advertising platforms a media buyer must act on -- Google Ads, "
    "Meta/Instagram, TikTok, LinkedIn, Amazon Ads: new ad formats and features, targeting or policy "
    "changes, measurement/tracking/privacy updates, and pricing or auction changes. Prefer official "
    "or well-sourced platform news over speculation. Frame each so the client sees why it matters."
)

_DEFAULT_PROMPTS = {
    "business_research": DEFAULT_BUSINESS_PROMPT,
    "media_buying": DEFAULT_MEDIA_PROMPT,
}


# --- Model / provider helpers -------------------------------------------------------------------
def provider_configured(provider):
    """True iff `provider` is usable on this deploy (so its models can be selected + run).

    * deepseek -- DEEPSEEK_API_KEY present.
    * gemini   -- Vertex enabled (VERTEX_GEMINI_ENABLED=1); the deploy sets this once the runtime SA
                  has roles/aiplatform.user, so it doubles as the "Gemini is wired" gate."""
    if provider == "deepseek":
        return bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    if provider == "gemini":
        return os.environ.get("VERTEX_GEMINI_ENABLED", "") in ("1", "true", "True")
    return False


def _provider_keys():
    """The set of provider keys we know how to gate (used by any_provider_configured)."""
    return ("gemini", "deepseek")


def model_meta(model_id):
    """The MODELS entry for `model_id` (dict), or None if it isn't one we offer."""
    mid = (model_id or "").strip()
    for m in MODELS:
        if m["id"] == mid:
            return m
    return None


def model_available(model_id):
    """True iff `model_id` is a known model AND its provider key is configured."""
    m = model_meta(model_id)
    return bool(m and provider_configured(m["provider"]))


def available_models():
    """Every offered model decorated with an `available` flag (provider key present).

    The UI shows all of them so the operator sees what's possible, disabling the ones whose key
    isn't wired yet (with a hint to add the secret). Returns a list of plain dicts (copyable)."""
    out = []
    for m in MODELS:
        row = dict(m)
        row["available"] = provider_configured(m["provider"])
        out.append(row)
    return out


def default_model():
    """The model to use when the admin hasn't picked one: the first AVAILABLE model, else "".

    Prefers the dropdown order (Gemini Flash first), so a configured deploy gets a sensible brain
    with zero setup; returns "" when no provider key is present (the feature stays a no-op)."""
    for m in MODELS:
        if provider_configured(m["provider"]):
            return m["id"]
    return ""


def default_prompt(section):
    """The default editorial prompt for a section ('business_research' | 'media_buying')."""
    return _DEFAULT_PROMPTS.get(section, "")


def any_provider_configured():
    """True iff at least one provider is usable (so the AI brain can run at all)."""
    return any(provider_configured(p) for p in _provider_keys())


def classify_text(system, user, max_tokens=256, fetcher=None, token_fetcher=None):
    """One small structured call with the default available model. Returns (text, error).

    A generic hook for tiny classification jobs elsewhere in the app (e.g. the Watcher tab's
    auto-industry label). Uses the same provider plumbing as the intel brain; JSON output mode,
    no grounding, no thinking capture. Degrades to ("", reason) when no provider is configured."""
    mid = default_model()
    if not mid:
        return "", "no AI provider configured"
    text, err, _thinking = _call(model_meta(mid), system, user, fetcher, max_tokens, token_fetcher)
    return text, err


# --- The prompts --------------------------------------------------------------------------------
def _system_prompt(section, client_name, editorial):
    """The fixed curation contract + the admin's editorial guidance. Locks the JSON output shape."""
    section_label = ("Media Buying News" if section == "media_buying" else "Business Research")
    return (
        "You are the research editor for AGORA, a marketing agency. You curate the "
        "\"%s\" section of the Market Intelligence briefing shown to the client "
        "\"%s\" in their workspace.\n\n"
        "You will be given a numbered list of REAL, recently-published news articles (each with its "
        "publisher, date, and a short snippet). Your job:\n"
        "  1. SELECT only the items that are genuinely relevant and useful to this client. Discard "
        "duplicates, off-topic items, pure ads, and low-signal listicles. Quality over quantity -- "
        "returning fewer strong items is better than padding.\n"
        "  2. For each selected item, write a `summary`: 1-2 plain-English sentences telling the "
        "client what happened and why it matters to them. No jargon, no hype, no preamble.\n"
        "  3. Give each a short `heading` (2-4 words, e.g. \"Platform Update\", \"Industry News\", "
        "\"Competitor Move\") and keep the original headline as `title`.\n\n"
        "EDITORIAL GUIDANCE (what to prioritise for this client):\n%s\n\n"
        "HARD RULES:\n"
        "  - Only ever reference items from the numbered list. NEVER invent an article, link, "
        "publisher, or date. Refer to each item by its number `n`.\n"
        "  - Return STRICT JSON and nothing else: "
        "{\"entries\": [{\"n\": <number>, \"heading\": \"...\", \"title\": \"...\", "
        "\"summary\": \"...\"}]}\n"
        "  - Order `entries` most-important first. No markdown, no code fences, no commentary."
        % (section_label, client_name or "the client", editorial or _DEFAULT_PROMPTS.get(section, ""))
    )


def _candidates_block(candidates):
    """Render the retrieved articles as a numbered list the model selects from."""
    lines = []
    for i, c in enumerate(candidates, 1):
        title = (c.get("title") or "").strip()
        source = (c.get("source") or "").strip()
        date = (c.get("date") or "").strip()
        snippet = (c.get("body") or "").strip()
        if len(snippet) > 240:
            snippet = snippet[:240].rsplit(" ", 1)[0] + "…"
        head = "[%d] %s" % (i, title)
        meta = " (%s%s%s)" % (source, ", " if source and date else "", date)
        lines.append(head + (meta if (source or date) else "") + (("\n    " + snippet) if snippet else ""))
    return "\n".join(lines)


def _user_prompt(client_name, topics, candidates, target):
    """The user turn: client context + the numbered candidate list + how many to pick."""
    topic_line = ""
    tops = [t for t in (topics or []) if (t or "").strip()]
    if tops:
        topic_line = "This client's focus areas / industry keywords: %s.\n" % ", ".join(tops)
    return (
        "Client: %s.\n%s\n"
        "Select up to %d of the most relevant articles below and return the JSON described.\n\n"
        "ARTICLES:\n%s"
        % (client_name or "the client", topic_line, target, _candidates_block(candidates))
    )


# --- Transport (the ONLY networked code; injectable for tests) ----------------------------------
def _requests_post(url, headers, payload, timeout):
    """Default POST fetcher. Lazy `requests` import so importing this module is side-effect free."""
    import requests  # lazy: tests inject their own fetcher, so this import is never needed off-cloud

    return requests.post(url, headers=headers, json=payload, timeout=timeout)


def _short_error(resp, fallback):
    """A SHORT human-readable reason from an error response body, mapped to friendly phrasing.

    Reads the provider's JSON `error.message` (Vertex + DeepSeek both use that shape) and collapses
    the common cases (quota/credits, auth) so the tab shows "out of quota/credits" not a paragraph."""
    msg = ""
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            msg = err.get("message") or ""
        elif isinstance(err, str):
            msg = err
    except Exception:
        msg = ""
    low = msg.lower()
    if any(w in low for w in ("quota", "credit", "exhaust", "resource_exhausted", "billing", "429")):
        return "out of quota/credits — check billing"
    if any(w in low for w in ("permission", "denied", "unauthor", "auth", "401", "403", "api key")):
        return "authentication/permission failed"
    status = getattr(resp, "status_code", 0)
    if msg:
        return (msg[:140] + "…") if len(msg) > 141 else msg
    return "%s (HTTP %s)" % (fallback, status or "?")


def _gcp_access_token(token_fetcher=None):
    """The runtime SA's OAuth access token (Cloud Run metadata server). "" if unavailable.

    `token_fetcher` is an injection seam for tests; the default hits the metadata server (works in
    Cloud Run / any GCE-family runtime the SA runs on)."""
    if token_fetcher is not None:
        return token_fetcher() or ""
    # Local/dev override: a token minted outside Cloud Run (e.g. `gcloud auth print-access-token`)
    # lets the SAME code paths run off-cloud without a metadata server.
    env_token = os.environ.get("VERTEX_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        import requests  # lazy
        r = requests.get(_METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=10)
        if getattr(r, "status_code", 0) >= 400:
            return ""
        return (r.json().get("access_token") or "").strip()
    except Exception:
        return ""


def _call_deepseek(model_id, system, user, fetcher, max_tokens, capture=False):
    """DeepSeek chat/completions (OpenAI-compatible). Returns (text, error, thinking).

    When `capture` is set we surface the model's `reasoning_content` (only the reasoner-class models
    emit it; the V4 chat models may return none, in which case thinking is "" -- the candidate list
    and raw output still make the run transparent)."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return "", "DeepSeek not configured", ""
    payload = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    fn = fetcher or _requests_post
    try:
        resp = fn(_DEEPSEEK_BASE + "/chat/completions", headers, payload, _TIMEOUT)
    except Exception as exc:
        return "", "could not reach DeepSeek (%s)" % type(exc).__name__, ""
    if getattr(resp, "status_code", 0) >= 400:
        return "", _short_error(resp, "DeepSeek error"), ""
    try:
        data = resp.json()
        msg = data["choices"][0]["message"]
        thinking = (msg.get("reasoning_content") or "").strip() if capture else ""
        return (msg.get("content") or "").strip(), "", thinking
    except Exception:
        return "", "DeepSeek returned an unexpected response", ""


def _call_vertex_gemini(model_id, system, user, fetcher, max_tokens, token_fetcher=None, capture=False):
    """Vertex AI Gemini :generateContent (GCP-billed, SA-token auth). Returns (text, error, thinking).

    Gemini 2.5 "thinks" by default and that thinking is billed against maxOutputTokens -- on a big
    curation prompt it can consume the whole budget and return EMPTY text. Normally this is structured
    extraction, not reasoning, so we minimise thinking (0 for Flash, the 128 floor for Pro) and the
    JSON answer always has room. When `capture` is set (the admin's "show reasoning" toggle) we give
    thinking its OWN budget, ask for the thought parts (`includeThoughts`), and RAISE the output cap
    so the answer still fits -- and separate the thought parts from the answer parts on the way out."""
    token = _gcp_access_token(token_fetcher)
    if not token:
        return "", "could not get GCP credentials for Vertex", ""
    loc = _VERTEX_LOCATION
    host = "aiplatform.googleapis.com" if loc == "global" else ("%s-aiplatform.googleapis.com" % loc)
    url = ("https://%s/v1/projects/%s/locations/%s/publishers/google/models/%s:generateContent"
           % (host, _VERTEX_PROJECT, loc, model_id))
    if capture:
        think_budget = 2048
        thinking_cfg = {"thinkingBudget": think_budget, "includeThoughts": True}
        out_cap = max(max_tokens, 4096) + think_budget   # leave room for the JSON on top of thinking
    else:
        thinking_cfg = {"thinkingBudget": 0 if "flash" in model_id else 128}
        out_cap = max_tokens
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "maxOutputTokens": out_cap,
            "thinkingConfig": thinking_cfg,
        },
    }
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    fn = fetcher or _requests_post
    try:
        resp = fn(url, headers, payload, _TIMEOUT)
    except Exception as exc:
        return "", "could not reach Vertex AI (%s)" % type(exc).__name__, ""
    if getattr(resp, "status_code", 0) >= 400:
        return "", _short_error(resp, "Vertex error"), ""
    try:
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        # A part flagged `thought:true` is reasoning, not the answer -- keep them apart.
        answer = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        thinking = "".join(p.get("text", "") for p in parts if p.get("thought")).strip() if capture else ""
        return answer, "", thinking
    except Exception:
        return "", "Vertex returned an unexpected response", ""


def _call(model, system, user, fetcher, max_tokens, token_fetcher=None, capture=False):
    """Dispatch to the right provider for `model` (a MODELS dict). Returns (text, error, thinking)."""
    if model["provider"] == "deepseek":
        return _call_deepseek(model["id"], system, user, fetcher, max_tokens, capture)
    if model["provider"] == "gemini":
        return _call_vertex_gemini(model["id"], system, user, fetcher, max_tokens, token_fetcher, capture)
    return "", "unknown provider", ""


# --- Parsing ------------------------------------------------------------------------------------
def _parse_json(raw):
    """Lenient JSON parse of a model reply (dict OR list). Tolerates a ```json fence or surrounding
    prose (grabs the first JSON-looking block). Returns the parsed object or None (never raises)."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"[\[{].*[\]}]", text, re.DOTALL)  # grab the first JSON-looking block
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _parse_entries(raw):
    """Best-effort parse of a model reply into a list of {n, heading, title, summary} dicts.

    Accepts both {"entries":[...]} and a bare [...] array (fences/prose tolerated via _parse_json).
    Returns [] if nothing parseable is found (never raises)."""
    obj = _parse_json(raw)
    if obj is None:
        return []
    if isinstance(obj, dict):
        obj = obj.get("entries") or obj.get("items") or []
    return obj if isinstance(obj, list) else []


def _shape(entry, candidates, heading_default):
    """Turn one parsed model entry into a stored intel-field dict, mapped onto its REAL candidate.

    Returns None if the entry doesn't point at a real candidate (guards against a fabricated item)."""
    if not isinstance(entry, dict):
        return None
    try:
        n = int(entry.get("n"))
    except (TypeError, ValueError):
        return None
    if n < 1 or n > len(candidates):
        return None
    cand = candidates[n - 1]
    body = (entry.get("summary") or "").strip()
    if not body:
        body = (cand.get("body") or "").strip()
    if len(body) > _BODY_MAX:
        body = body[:_BODY_MAX].rsplit(" ", 1)[0] + "…"
    title = (entry.get("title") or "").strip() or (cand.get("title") or "").strip()
    heading = (entry.get("heading") or "").strip() or heading_default
    return {
        "heading": heading,
        "title": title,
        "body": body,
        # link/source/date ALWAYS come from the real retrieved article -- never the model.
        "source": (cand.get("source") or "").strip(),
        "link": (cand.get("link") or "").strip(),
        "date": (cand.get("date") or "").strip(),
    }


# --- The one call the refresh job makes ---------------------------------------------------------
def curate(section, client_name, topics, candidates, prompt=None, model=None,
           limit=6, heading_default="", fetcher=None, token_fetcher=None, max_tokens=8192,
           capture_thinking=False, trace=None):
    """LEGACY retrieve-then-curate primitive (curate a supplied candidate list). Not used by the daily
    refresh anymore -- that now uses `research()` (grounded live web search). Kept as a general
    curation helper (and covered by tests); prefer `research()` for real intelligence.

    Curate real `candidates` into up to `limit` briefing entries for `section`, using `model`.

    Returns `(entries, error)`:
      * on success: (list of intel-field dicts mapped onto the real articles, most-important first, "").
      * on failure: (None, "<short reason>") -- e.g. the model is unavailable, out of quota, or
        returned nothing usable. There is NO news-feed fallback: the caller records the reason and
        shows it, rather than filling the tab with junk.
    `prompt` is the admin's editorial guidance for the section (blank -> the module default);
    `fetcher`/`token_fetcher` are the transport injection seams for tests. When `capture_thinking` is
    set the model's reasoning is surfaced; pass a `trace` dict and it is filled IN PLACE with
    `thinking` + `raw` (the model's reasoning and raw output) so the caller can show the admin what
    the brain actually did -- the 2-tuple return is unchanged."""
    meta = model_meta(model)
    if meta is None:
        return None, "no model selected"
    if not provider_configured(meta["provider"]):
        return None, "%s isn't configured on the server" % meta["label"]
    cands = [c for c in (candidates or []) if (c.get("title") or "").strip()]
    if not cands:
        return None, "no source articles found to research"
    editorial = (prompt or "").strip() or default_prompt(section)
    system = _system_prompt(section, client_name, editorial)
    user = _user_prompt(client_name, topics, cands, limit)
    raw, err, thinking = _call(meta, system, user, fetcher, max_tokens, token_fetcher, capture_thinking)
    if trace is not None:
        trace["thinking"] = thinking or ""
        trace["raw"] = raw or ""
    if err:
        return None, err
    parsed = _parse_entries(raw)
    if not parsed:
        return None, "the model returned nothing usable"
    out, seen = [], set()
    hd = heading_default or ("Platform Update" if section == "media_buying" else "Industry News")
    for e in parsed:
        row = _shape(e, cands, hd)
        if row is None:
            continue
        key = (row["title"].lower(), row["link"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    if not out:
        return None, "the model found no relevant items"
    return out, ""


# ================================================================================================
# GROUNDED RESEARCH -- the real research engine (Gemini + live Google Search grounding).
# ================================================================================================
# This is what makes the feature behave like Gemini chat: instead of scraping Google News RSS and
# re-ranking it, we let Gemini PLAN -> SEARCH the whole web (grounding) -> CURATE with citations.
# The model reads the client profile, decides the angles that matter, runs its own Google searches,
# reads real pages, and returns items each with a REAL source URL and a "why this matters to THIS
# client" line. Grounding is a Gemini/Vertex capability (DeepSeek can't search the live web), so this
# path is Gemini-only; a non-Gemini model returns a clear reason (no junk, no fallback).

def provider_supports_grounding(provider):
    """True iff `provider` can do live web-search grounding (Gemini via Vertex only)."""
    return provider == "gemini"


def model_supports_grounding(model_id):
    """True iff `model_id` is a known, configured model that can ground on Google Search."""
    m = model_meta(model_id)
    return bool(m and provider_supports_grounding(m["provider"]) and provider_configured(m["provider"]))


def _grounded_system_prompt(section, client_name, topics, editorial, limit, recency="recent"):
    """The research contract for a grounded run: PLAN -> SEARCH -> CURATE, with a per-item rationale."""
    section_label = ("Media Buying News" if section == "media_buying" else "Business Research")
    name = client_name or "the client"
    tops = ", ".join(t for t in (topics or []) if (t or "").strip()) or "(none given)"
    return (
        "You are the senior research editor at AGORA, a marketing agency, preparing the \"%s\" "
        "section of the Market Intelligence briefing for the client \"%s\".\n\n"
        "Do REAL web research -- do not summarise a supplied list. Work in three steps:\n"
        "  1) PLAN: from the client profile below, think about THIS client's business and decide the "
        "few angles that genuinely matter to them right now (competitor moves, market/demand shifts, "
        "pricing, regulation, how their customers buy; for Media Buying: ad-platform format/policy/"
        "targeting/measurement/pricing changes on Google, Meta, TikTok, LinkedIn, Amazon).\n"
        "  2) SEARCH: use Google Search to find developments from roughly %s across the WHOLE web "
        "for those angles -- not just news sites. Prefer concrete, decision-relevant, well-sourced "
        "items; avoid evergreen explainers and generic 'top tips' listicles.\n"
        "  3) CURATE: select the %d strongest, most relevant items. Keep the REAL source name and the "
        "REAL URL you actually found.\n\n"
        "CLIENT PROFILE:\n"
        "  - Client: %s\n"
        "  - Their focus / seed keywords (treat as SEEDS to expand into real angles, not literal "
        "search strings): %s\n"
        "  - Editorial guidance: %s\n\n"
        "Return STRICT JSON and nothing else (no markdown, no code fences, no commentary):\n"
        "{\"entries\": [{\"heading\": \"2-4 word tag\", \"title\": \"the real headline\", "
        "\"summary\": \"1-2 plain sentences: what happened\", \"relevance\": \"one sentence: why this "
        "specifically matters to %s\", \"source\": \"publisher name\", \"url\": \"the real source "
        "URL\", \"date\": \"YYYY-MM-DD if known, else empty\"}]}\n"
        "Order most-important first. Quality over quantity -- fewer strong, on-topic items beat "
        "padding. Only include items you actually found via search, each with a real URL."
        % (section_label, name, recency, limit, name, tops, editorial or default_prompt(section), name)
    )


def _call_vertex_grounded(model_id, system, user, fetcher, token_fetcher=None, capture=False, max_tokens=8192):
    """Vertex Gemini :generateContent WITH the Google Search tool. Returns (text, error, thinking, grounding).

    `grounding` = {queries:[...], sources:[{title,uri}], suggestions:<html>} pulled from the response's
    groundingMetadata -- the exact searches it ran (the visible "gameplan"), the sources it found, and
    Google's required Search-Suggestions chip HTML. We do NOT set responseMimeType: JSON mode is
    unreliable alongside the search tool, so we ask for JSON in the prompt and parse leniently."""
    token = _gcp_access_token(token_fetcher)
    if not token:
        return "", "could not get GCP credentials for Vertex", "", {}
    loc = _VERTEX_LOCATION
    host = "aiplatform.googleapis.com" if loc == "global" else ("%s-aiplatform.googleapis.com" % loc)
    url = ("https://%s/v1/projects/%s/locations/%s/publishers/google/models/%s:generateContent"
           % (host, _VERTEX_PROJECT, loc, model_id))
    gen = {}
    if capture:
        gen["thinkingConfig"] = {"thinkingBudget": 2048, "includeThoughts": True}
        gen["maxOutputTokens"] = max(max_tokens, 8192) + 2048
    else:
        gen["thinkingConfig"] = {"thinkingBudget": 128 if "pro" in model_id else 512}
        gen["maxOutputTokens"] = max(max_tokens, 8192)
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": gen,
    }
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    fn = fetcher or _requests_post
    try:
        resp = fn(url, headers, payload, _RESEARCH_TIMEOUT)
    except Exception as exc:
        return "", "could not reach Vertex AI (%s)" % type(exc).__name__, "", {}
    if getattr(resp, "status_code", 0) >= 400:
        return "", _short_error(resp, "Vertex error"), "", {}
    try:
        data = resp.json()
        cand = data["candidates"][0]
        parts = cand["content"]["parts"]
        answer = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        thinking = "".join(p.get("text", "") for p in parts if p.get("thought")).strip() if capture else ""
        gm = cand.get("groundingMetadata") or {}
        chunks = gm.get("groundingChunks") or []
        grounding = {
            "queries": [q for q in (gm.get("webSearchQueries") or []) if q],
            "sources": [{"title": (c.get("web") or {}).get("title", ""),
                         "uri": (c.get("web") or {}).get("uri", "")}
                        for c in chunks if c.get("web")],
            "suggestions": ((gm.get("searchEntryPoint") or {}).get("renderedContent") or ""),
        }
        return answer, "", thinking, grounding
    except Exception:
        return "", "Vertex returned an unexpected response", "", {}


def _shape_grounded(entry, heading_default):
    """Turn one grounded model entry into a stored intel-field dict (with `relevance`). None if empty."""
    if not isinstance(entry, dict):
        return None
    title = (entry.get("title") or "").strip()
    body = (entry.get("summary") or entry.get("body") or "").strip()
    if not (title or body):
        return None
    if len(body) > _BODY_MAX:
        body = body[:_BODY_MAX].rsplit(" ", 1)[0] + "…"
    relevance = (entry.get("relevance") or "").strip()
    if len(relevance) > _BODY_MAX:
        relevance = relevance[:_BODY_MAX].rsplit(" ", 1)[0] + "…"
    url = (entry.get("url") or entry.get("link") or "").strip()
    if not url.lower().startswith("http"):
        url = ""                          # never keep a fabricated/relative URL
    return {
        "heading": (entry.get("heading") or "").strip() or heading_default,
        "title": title,
        "body": body,
        "relevance": relevance,
        "source": (entry.get("source") or "").strip(),
        "link": url,
        "date": (entry.get("date") or "").strip(),
    }


def research(section, client_name, topics, prompt=None, model=None, limit=8, heading_default="",
             recency="recent", capture_thinking=False, trace=None, fetcher=None, token_fetcher=None,
             max_tokens=8192):
    """Grounded web research for `section` using `model` (Gemini only). Returns `(entries, error)`.

    The model plans, searches Google live, and curates -- each returned entry carries a real source
    URL and a `relevance` ("why this matters to the client") line. No fallback: on any failure returns
    (None, <short reason>). `trace` (if given) is filled with the model's reasoning, the searches it
    ran (`queries`), the `sources` it grounded on, Google's `suggestions` chip HTML, and the raw text."""
    meta = model_meta(model)
    if meta is None:
        return None, "no model selected"
    if not provider_supports_grounding(meta["provider"]):
        return None, "%s can't do live web research — pick a Gemini model" % meta["label"]
    if not provider_configured(meta["provider"]):
        return None, "%s isn't configured on the server" % meta["label"]
    editorial = (prompt or "").strip() or default_prompt(section)
    system = _grounded_system_prompt(section, client_name, topics, editorial, limit, recency)
    user = "Research the client's world now and return the briefing JSON."
    raw, err, thinking, grounding = _call_vertex_grounded(
        meta["id"], system, user, fetcher, token_fetcher, capture_thinking, max_tokens)
    if trace is not None:
        trace["thinking"] = thinking or ""
        trace["raw"] = raw or ""
        trace["queries"] = grounding.get("queries") or []
        trace["sources"] = grounding.get("sources") or []
        trace["suggestions"] = grounding.get("suggestions") or ""
    if err:
        return None, err
    parsed = _parse_entries(raw)
    if not parsed:
        return None, "the model returned nothing usable"
    hd = heading_default or ("Platform Update" if section == "media_buying" else "Industry News")
    src_uris = [s["uri"] for s in (grounding.get("sources") or []) if s.get("uri")]
    out, seen = [], set()
    for i, e in enumerate(parsed):
        row = _shape_grounded(e, hd)
        if row is None:
            continue
        if not row["link"] and src_uris:            # back the item with a real grounded source URL
            row["link"] = src_uris[min(i, len(src_uris) - 1)]
        key = (row["title"].lower(), row["link"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    if not out:
        return None, "the model found no relevant items"
    return out, ""


# ================================================================================================
# CONFIG DRAFTING -- the panel's "Write with AI" button: draft a client's keywords + focus prompts.
# ================================================================================================
# Given whatever the workspace already knows about the client (campaign strategies, website,
# watcher industries...), the model drafts the three admin-tunable settings: the research keywords
# and the two per-section editorial prompts. Nothing is saved here -- the route returns the drafts
# and the panel fills the fields for the admin to review and Save. On a grounding-capable model
# (Gemini) the draft is grounded: the model first LOOKS THE CLIENT UP on live Google Search, so a
# nearly-empty workspace still gets specific, informed suggestions instead of generic filler.

_SUGGEST_FIELD_MAX = {"topics": 400, "business_prompt": 900, "media_prompt": 900}


def _suggest_system_prompt(client_name, grounded):
    """The drafting contract: what each of the three settings is for + the locked JSON shape."""
    name = client_name or "the client"
    search_step = (
        "First, use Google Search to look the client up -- their website, what they sell, who "
        "their customers and competitors are -- so the drafts are specific, never generic.\n\n"
        if grounded else "")
    return (
        "You configure the \"AI Research Brain\" for AGORA, a marketing agency. The brain runs "
        "daily web research that fills the Market Intelligence briefing shown to the client "
        "\"%s\"; it is steered by three admin-written settings you must now draft for this "
        "client:\n"
        "  1. \"topics\" -- 4-8 comma-separated research keywords: their industry and product "
        "category, who their customers are, and named competitors if known. Specific beats "
        "generic (\"boutique RV rentals\", not \"travel\"). These are SEEDS the researcher "
        "expands into real web searches.\n"
        "  2. \"business_prompt\" -- 2-4 sentences of editorial guidance for the Business "
        "Research section: which competitor, market, customer, and regulatory developments "
        "genuinely matter to THIS client, and what to skip.\n"
        "  3. \"media_prompt\" -- 2-4 sentences for the Media Buying News section: which ad "
        "platforms this client most plausibly spends on, and which kinds of platform changes "
        "(formats, targeting, policy, measurement, pricing) matter to them.\n\n"
        "%s"
        "Write the prompts as direct instructions to a researcher, in the same instructional "
        "style as these defaults -- tailored to this client, never a copy:\n"
        "  DEFAULT business_prompt: %s\n"
        "  DEFAULT media_prompt: %s\n\n"
        "Return STRICT JSON and nothing else (no markdown, no code fences, no commentary):\n"
        "{\"topics\": \"kw1, kw2, ...\", \"business_prompt\": \"...\", \"media_prompt\": \"...\"}"
        % (name, search_step, DEFAULT_BUSINESS_PROMPT, DEFAULT_MEDIA_PROMPT)
    )


def suggest_config(client_name, context="", model=None, fetcher=None, token_fetcher=None,
                   max_tokens=2048):
    """Draft the AI Research Brain settings for a client. Returns (fields, error).

    `fields` = {"topics", "business_prompt", "media_prompt"} (strings; a field the model skipped
    is ""), or None with a SHORT human reason on failure -- gated + graceful like every call here.
    `context` is the plain-text digest of what the workspace already knows about the client. Uses
    the client's selected `model` when it's available, else the first configured model; a Gemini
    model grounds the draft on a live Google-Search lookup of the client first."""
    mid = model if model_available(model) else default_model()
    if not mid:
        return None, "no AI model is configured on the server"
    meta = model_meta(mid)
    grounded = model_supports_grounding(mid)
    system = _suggest_system_prompt(client_name, grounded)
    user = (
        "Client: %s.\nWhat the agency already knows about them:\n%s\n\n"
        "Draft the three settings for this client now and return the JSON."
        % (client_name or "the client",
           (context or "").strip() or "(very little yet -- just the name; infer what you can)"))
    if grounded:
        raw, err, _thinking, _grounding = _call_vertex_grounded(
            meta["id"], system, user, fetcher, token_fetcher, capture=False, max_tokens=max_tokens)
    else:
        raw, err, _thinking = _call(meta, system, user, fetcher, max_tokens, token_fetcher)
    if err:
        return None, err
    obj = _parse_json(raw)
    if not isinstance(obj, dict):
        return None, "the model returned nothing usable"
    out = {}
    for field, cap in _SUGGEST_FIELD_MAX.items():
        v = obj.get(field)
        if isinstance(v, (list, tuple)):        # tolerate topics coming back as a JSON array
            v = ", ".join(str(x).strip() for x in v if str(x).strip())
        v = (str(v) if v is not None else "").strip()
        if len(v) > cap:
            v = v[:cap].rsplit(" ", 1)[0] + "…"
        out[field] = v
    if not any(out.values()):
        return None, "the model returned nothing usable"
    return out, ""
