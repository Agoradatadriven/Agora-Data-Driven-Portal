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

_DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
_TIMEOUT = 60          # seconds; a slow model must never hang the whole refresh run.
_BODY_MAX = 320        # a briefing summary is short by design (matches intel_refresh._BODY_MAX-ish).

# Vertex AI (GCP-billed Gemini). Project/location come from env (the deploy sets them); the token is
# the runtime SA's, fetched from the metadata server -- no API key. `global` and a regional location
# both work; we default to the project's region so data stays in-region.
_VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "agora-data-driven"
_VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "asia-southeast1")
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
    try:
        import requests  # lazy
        r = requests.get(_METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=10)
        if getattr(r, "status_code", 0) >= 400:
            return ""
        return (r.json().get("access_token") or "").strip()
    except Exception:
        return ""


def _call_deepseek(model_id, system, user, fetcher, max_tokens):
    """DeepSeek chat/completions (OpenAI-compatible). Returns (text, error)."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return "", "DeepSeek not configured"
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
        return "", "could not reach DeepSeek (%s)" % type(exc).__name__
    if getattr(resp, "status_code", 0) >= 400:
        return "", _short_error(resp, "DeepSeek error")
    try:
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip(), ""
    except Exception:
        return "", "DeepSeek returned an unexpected response"


def _call_vertex_gemini(model_id, system, user, fetcher, max_tokens, token_fetcher=None):
    """Vertex AI Gemini :generateContent (GCP-billed, SA-token auth). Returns (text, error)."""
    token = _gcp_access_token(token_fetcher)
    if not token:
        return "", "could not get GCP credentials for Vertex"
    loc = _VERTEX_LOCATION
    host = "aiplatform.googleapis.com" if loc == "global" else ("%s-aiplatform.googleapis.com" % loc)
    url = ("https://%s/v1/projects/%s/locations/%s/publishers/google/models/%s:generateContent"
           % (host, _VERTEX_PROJECT, loc, model_id))
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "maxOutputTokens": max_tokens,
        },
    }
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    fn = fetcher or _requests_post
    try:
        resp = fn(url, headers, payload, _TIMEOUT)
    except Exception as exc:
        return "", "could not reach Vertex AI (%s)" % type(exc).__name__
    if getattr(resp, "status_code", 0) >= 400:
        return "", _short_error(resp, "Vertex error")
    try:
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip(), ""
    except Exception:
        return "", "Vertex returned an unexpected response"


def _call(model, system, user, fetcher, max_tokens, token_fetcher=None):
    """Dispatch to the right provider for `model` (a MODELS dict). Returns (text, error)."""
    if model["provider"] == "deepseek":
        return _call_deepseek(model["id"], system, user, fetcher, max_tokens)
    if model["provider"] == "gemini":
        return _call_vertex_gemini(model["id"], system, user, fetcher, max_tokens, token_fetcher)
    return "", "unknown provider"


# --- Parsing ------------------------------------------------------------------------------------
def _parse_entries(raw):
    """Best-effort parse of a model reply into a list of {n, heading, title, summary} dicts.

    Tolerates a ```json fence or surrounding prose and both {"entries":[...]} and a bare [...]
    array. Returns [] if nothing parseable is found (never raises)."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    obj = None
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"[\[{].*[\]}]", text, re.DOTALL)  # grab the first JSON-looking block
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
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
           limit=6, heading_default="", fetcher=None, token_fetcher=None, max_tokens=2400):
    """Curate real `candidates` into up to `limit` briefing entries for `section`, using `model`.

    Returns `(entries, error)`:
      * on success: (list of intel-field dicts mapped onto the real articles, most-important first, "").
      * on failure: (None, "<short reason>") -- e.g. the model is unavailable, out of quota, or
        returned nothing usable. There is NO news-feed fallback: the caller records the reason and
        shows it, rather than filling the tab with junk.
    `prompt` is the admin's editorial guidance for the section (blank -> the module default);
    `fetcher`/`token_fetcher` are the transport injection seams for tests."""
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
    raw, err = _call(meta, system, user, fetcher, max_tokens, token_fetcher)
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
