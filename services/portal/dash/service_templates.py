"""Service templates -- the recipe book for the Task tracker's Delivery board.

Picking a department + a service type on the New-Service form auto-generates the whole two-level
work breakdown (main tasks -> sub-tasks), so the team edits from a filled-in starting point instead
of retyping the same phases for every campaign. This is the Python home of what the interactive
prototype in `atrium/service_template_prototype.html` demonstrated.

Design notes:
- **One source of truth.** `TEMPLATES` (keyed by a stable service key) + `AD_PRODUCTION` describe the
  recipes; `build_maintasks()` turns a recipe + params + chosen ad-production sets into the exact
  `maintasks[]` shape `workspace.add_task` stores. Nothing here touches infra -- pure data.
- **Departments match `main.TASK_DEPARTMENTS`** (acquisition / lifecycle / data / development). Each
  template belongs to exactly one department, so the service-type dropdown is just a department filter.
- **Acquisition = ONE service type** ("Google / Meta Campaign") whose creative work is chosen from the
  ad-production picker (Video / Static / Carousel, each with a quantity); everything else is a fixed
  recipe, some with per-service params (quantities, platform, tool).
- **`build_maintasks` is a seed, not a lock.** After creation the maintasks are ordinary stored data
  the existing helpers rename / add / delete, and the close-guard still governs completion.
- Each sub-task carries an optional **`dod`** ("done when") -- an INTERNAL definition of done shown in
  the team detail overlay only (the client Progress tab strips steps to text + done).

A template dict:
    {label, dept, content_type, camp(bool), camp_type("text"|"select"), ad_production(bool),
     params:[{k, kind("qty"|"choice"|"text"), label, def, opts?}],
     mt:[{t, s:[{t, d, u?}]}]}          # u:"qty" marks a per-unit step multiplied by params[u]
"""

import uuid


# ---------------------------------------------------------------------------------------------------
# The recipe book (subset + shape mirrors the HTML prototype's `T`).
# ---------------------------------------------------------------------------------------------------
TEMPLATES = {
    # --- Acquisition: ONE service type; creative work comes from the ad-production picker ----------
    "google_meta_campaign": {
        "label": "Google / Meta Campaign", "dept": "acquisition", "content_type": "Campaign",
        "camp": True, "camp_type": "text", "ad_production": True, "params": [],
        "mt": [
            {"t": "Campaign build", "s": [
                {"t": "Audience, interest & budget research", "d": "Research sheet completed and linked on the card"},
                {"t": "Campaign structure plan", "d": "Objective + conversion event + CBO/ABO approved by the lead"},
                {"t": "Audience build", "d": "Custom / lookalike / interest audiences saved in Ads Manager"},
                {"t": "Ad set configuration", "d": "Placements, schedule and budget set per the plan"},
                {"t": "Load creatives + copy", "d": "All approved creatives, headlines, primary text and CTAs loaded"},
                {"t": "Compliance check", "d": "Client rules verified before launch"}]},
            {"t": "Launch & verify", "s": [
                {"t": "Tracking confirmation with Systems & Dev", "d": "Pixel / CAPI events verified firing before spend starts"},
                {"t": "Launch", "d": "Campaign published; campaign ID logged on the card"},
                {"t": "Post-launch QA (24h)", "d": "Delivery, spend pacing and attribution confirmed"}]},
        ],
    },

    # --- Lifecycle (typed campaign names -- unique) -----------------------------------------------
    "email_automation": {
        "label": "Email Automation / Sequence Build", "dept": "lifecycle", "content_type": "Email Automation",
        "camp": True, "camp_type": "text", "ad_production": False,
        "params": [
            {"k": "qty", "kind": "qty", "label": "How many emails?", "def": 5},
            {"k": "platform", "kind": "choice", "label": "Platform",
             "opts": ["ActiveCampaign", "GHL", "Other"], "def": "ActiveCampaign"}],
        "mt": [
            {"t": "Plan", "s": [
                {"t": "Automation map (trigger, waits, if/else, exit)", "d": "Flow documented in plain language, handover-ready"}]},
            {"t": "Email production", "s": [
                {"t": "Email {n} — copy", "u": "qty", "d": "Copy drafted to the approved angle"},
                {"t": "Email {n} — HTML build", "u": "qty", "d": "Built, responsive, merge fields correct"}]},
            {"t": "Build & activate", "s": [
                {"t": "Automation built in platform", "d": "All steps, conditions and tags created"},
                {"t": "Entry points connected", "d": "Forms / links / imports wired to the trigger"},
                {"t": "End-to-end test", "d": "A test contact travels the full flow correctly"},
                {"t": "Activate", "d": "Sequence live; logged on the card"}]},
        ],
    },
    "content_cycle": {
        "label": "Content Cycle (theme)", "dept": "lifecycle", "content_type": "Content",
        "camp": True, "camp_type": "text", "ad_production": False,
        "params": [{"k": "qty", "kind": "qty", "label": "Supporting blogs", "def": 5}],
        "mt": [
            {"t": "Plan", "s": [
                {"t": "Topic ideation approved", "d": "Theme and angle signed off"},
                {"t": "Angle brief written", "d": "Brief on the card"}]},
            {"t": "Write", "s": [
                {"t": "Anchor blog", "d": "Anchor drafted and reviewed"},
                {"t": "Supporting blog {n}", "u": "qty", "d": "Drafted and reviewed against the angle brief"},
                {"t": "Newsletter from the theme", "d": "Newsletter drafted"}]},
            {"t": "Produce & publish", "s": [
                {"t": "Blog graphics", "d": "Graphics produced for every blog"},
                {"t": "Publish", "d": "All pieces live; URLs logged on the card"}]},
        ],
    },
    "organic_social": {
        "label": "Organic Social Post Production", "dept": "lifecycle", "content_type": "Social",
        "camp": False, "camp_type": "text", "ad_production": False,
        "params": [
            {"k": "qty", "kind": "qty", "label": "How many posts?", "def": 2},
            {"k": "channel", "kind": "text", "label": "Channel", "def": "Facebook"}],
        "mt": [
            {"t": "Post batch", "s": [
                {"t": "Post {n} — concept + caption", "u": "qty", "d": "Concept and caption approved"},
                {"t": "Post {n} — design", "u": "qty", "d": "Designed to brand spec"},
                {"t": "Approver sign-off", "d": "Named approver has signed off the batch"},
                {"t": "Scheduled", "d": "Scheduled per cadence; logged"}]},
        ],
    },

    # --- Data ------------------------------------------------------------------------------------
    "market_research": {
        "label": "Market Research Package", "dept": "data", "content_type": "Research",
        "camp": False, "camp_type": "text", "ad_production": False, "params": [],
        "mt": [
            {"t": "Research package", "s": [
                {"t": "Company profile", "d": "What they do, offer and positioning documented"},
                {"t": "Brand / business identity", "d": "Voice, values and differentiators captured"},
                {"t": "PESTEL analysis", "d": "All six factors documented with client relevance"},
                {"t": "Industry overview", "d": "Market size, dynamics and key players summarised"},
                {"t": "Competitor research", "d": "Three or more competitors profiled (offer, pricing, angle)"},
                {"t": "Trend research", "d": "Current demand and content trends noted"},
                {"t": "Link in central sheet", "d": "Research doc linked on the central sheet"}]},
        ],
    },
    "dashboard_build": {
        "label": "Dashboard Build", "dept": "data", "content_type": "Dashboard",
        "camp": True, "camp_type": "text", "ad_production": False,
        "params": [{"k": "tool", "kind": "choice", "label": "Tool",
                    "opts": ["Looker Studio", "Sheets", "Custom"], "def": "Looker Studio"}],
        "mt": [
            {"t": "Define & connect", "s": [
                {"t": "Define metrics", "d": "Metric list plus source per metric agreed with the requester"},
                {"t": "Data sources connected", "d": "All sources connected and pulling"}]},
            {"t": "Build & hand over", "s": [
                {"t": "Build views", "d": "All agreed views built"},
                {"t": "Validate numbers", "d": "Figures match source-of-truth spot checks"},
                {"t": "Share + walkthrough", "d": "Access granted and walkthrough done"}]},
        ],
    },

    # --- Development ------------------------------------------------------------------------------
    "tracking_setup": {
        "label": "Tracking Infrastructure Setup", "dept": "development", "content_type": "Tracking",
        "camp": False, "camp_type": "text", "ad_production": False, "params": [],
        "mt": [
            {"t": "Install", "s": [
                {"t": "GTM container", "d": "Installed; publish access confirmed"},
                {"t": "GA4", "d": "Connected and receiving data"},
                {"t": "Meta Pixel", "d": "Installed in the correct native field and firing"},
                {"t": "Meta CAPI", "d": "Server events configured and deduplicating"}]},
            {"t": "Configure & verify", "s": [
                {"t": "Conversion events / triggers", "d": "Every key event built and named per convention"},
                {"t": "UTM scheme", "d": "Naming convention documented for campaigns"},
                {"t": "QA / verify", "d": "Events confirmed in GTM Preview and GA4 Realtime"}]},
        ],
    },
    "website_fix": {
        "label": "Website Edit / Fix / Integration", "dept": "development", "content_type": "Website",
        "camp": False, "camp_type": "text", "ad_production": False, "params": [],
        "mt": [
            {"t": "Fix", "s": [
                {"t": "Change implemented", "d": "Edit made on the live site or staging"},
                {"t": "Integration connected", "d": "Payment / form / pixel connected where applicable"},
                {"t": "Verified in production", "d": "Confirmed working on the live site"}]},
        ],
    },
}


# --- Ad production task sets (chosen from the picker on an Acquisition campaign) --------------------
AD_PRODUCTION = {
    "video": {"label": "Video Ad Production", "qty_label": "How many videos?", "def": 3,
              "group": {"t": "Video ad production", "s": [
                  {"t": "Script / concept per video approved", "d": "Every video has an approved hook and script before editing"},
                  {"t": "Footage secured", "d": "All required footage available to the editor"},
                  {"t": "Video {n} — draft edit", "u": "qty", "d": "First cut produced to the approved script"},
                  {"t": "Video {n} — internal review", "u": "qty", "d": "Brand colours, fonts, pacing and hook checked"},
                  {"t": "Export + file", "d": "Final exports filed; folder link on the card"}]}},
    "static": {"label": "Static Ad Production", "qty_label": "How many statics?", "def": 5,
               "group": {"t": "Static ad production", "s": [
                   {"t": "Batch brief approved (angles + copy direction)", "d": "Brief signed off before any design starts"},
                   {"t": "Static {n} — design", "u": "qty", "d": "Designed to brand spec, copy matches the approved brief"},
                   {"t": "Internal review of the batch", "d": "All statics checked against brief + compliance"},
                   {"t": "Export + file to campaign folder", "d": "Exports filed; folder link on the card"}]}},
    "carousel": {"label": "Carousel Ad Production", "qty_label": "How many carousels?", "def": 1,
                 "group": {"t": "Carousel ad production", "s": [
                     {"t": "Card structure + copy approved", "d": "Card-by-card structure and copy signed off"},
                     {"t": "Carousel {n} — design all cards", "u": "qty", "d": "All cards designed; sequence reads in order"},
                     {"t": "Internal review", "d": "Sequence logic and brand checked"},
                     {"t": "Export + file", "d": "Exports filed; folder link on the card"}]}},
}
AD_ORDER = ("video", "static", "carousel")

MAX_QTY = 50


# ---------------------------------------------------------------------------------------------------
# Lookups + the builder.
# ---------------------------------------------------------------------------------------------------
def get(key):
    """The template dict for a service key, or None."""
    return TEMPLATES.get((key or "").strip())


def services_for(dept):
    """[(key, label), ...] of the service types in a department (stable insertion order)."""
    return [(k, t["label"]) for k, t in TEMPLATES.items() if t["dept"] == (dept or "").strip()]


def _qty(params, key):
    """A sanitized 1..MAX_QTY integer from params[key] (defaults to 1 on junk)."""
    try:
        n = int(str((params or {}).get(key, "")).strip())
    except (TypeError, ValueError):
        n = 1
    if n < 1:
        n = 1
    if n > MAX_QTY:
        n = MAX_QTY
    return n


def _expand(steps, n, new_id):
    """Expand a recipe's step list into stored sub-tasks, multiplying any per-unit ({n}, u) step."""
    out = []
    for s in steps:
        if s.get("u"):
            for k in range(1, n + 1):
                out.append({"id": new_id("st"), "text": s["t"].replace("{n}", str(k)),
                            "done": False, "assignee_id": "", "dod": s.get("d", "")})
        else:
            out.append({"id": new_id("st"), "text": s["t"], "done": False,
                        "assignee_id": "", "dod": s.get("d", "")})
    return out


def build_maintasks(key, params=None, added=None, id_factory=None):
    """Build the stored `maintasks[]` for a service.

    `key` -- template key; `params` -- {param_key: value} from the form; `added` -- an iterable of
    (ad_type, qty) chosen from the ad-production picker (Acquisition only); `id_factory(prefix)` --
    the id generator (pass `workspace._new_id` so ids match; falls back to a uuid stub for tests).
    Returns a list of {id, text, assignee_id, subs:[{id, text, done, assignee_id, dod}]}.
    """
    tpl = get(key)
    if not tpl:
        return []
    new_id = id_factory or (lambda p: "%s_%s" % (p, uuid.uuid4().hex[:8]))
    params = params or {}
    out = []
    # Fixed / automatic groups from the recipe (per-unit steps read a service param).
    for m in tpl["mt"]:
        out.append({"id": new_id("mt"), "text": m["t"], "assignee_id": "",
                    "subs": _expand(m["s"], _qty(params, _unit_key(m)), new_id)})
    # Chosen ad-production sets, appended on top.
    if tpl.get("ad_production") and added:
        for ad_type, ad_qty in added:
            defn = AD_PRODUCTION.get((ad_type or "").strip())
            if not defn:
                continue
            n = _qty({"qty": ad_qty}, "qty")
            out.append({"id": new_id("mt"), "text": defn["group"]["t"], "assignee_id": "",
                        "subs": _expand(defn["group"]["s"], n, new_id)})
    return out


def _unit_key(maintask):
    """The param key a group's per-unit steps multiply by (the first `u` found), or "qty"."""
    for s in maintask.get("s", []):
        if s.get("u"):
            return s["u"]
    return "qty"


# ---------------------------------------------------------------------------------------------------
# Front-end catalog (rendered as hidden DOM the console JS reads -- no Jinja in <script>).
# ---------------------------------------------------------------------------------------------------
def catalog():
    """A render-ready list of every service type for the New-Service picker.

    Each entry: {key, dept, label, content_type, camp, camp_type, ad_production,
    params:[{k, kind, label, def, opts:[...]}]}.
    """
    out = []
    for k, t in TEMPLATES.items():
        out.append({
            "key": k, "dept": t["dept"], "label": t["label"], "content_type": t["content_type"],
            "camp": bool(t.get("camp")), "camp_type": t.get("camp_type", "text"),
            "ad_production": bool(t.get("ad_production")),
            "params": [{"k": p["k"], "kind": p["kind"], "label": p["label"],
                        "def": p.get("def", ""), "opts": list(p.get("opts", []))}
                       for p in t.get("params", [])],
        })
    return out


def ad_catalog():
    """A render-ready list of the ad-production sets: [{key, label, qty_label, def}, ...]."""
    return [{"key": k, "label": AD_PRODUCTION[k]["label"],
             "qty_label": AD_PRODUCTION[k]["qty_label"], "def": AD_PRODUCTION[k]["def"]}
            for k in AD_ORDER]
