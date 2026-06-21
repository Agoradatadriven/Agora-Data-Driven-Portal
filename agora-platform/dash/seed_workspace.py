"""Seed the Agora Atrium demo workspace for Riverdance RV Resort (workspace/riverdance.json).

Run this ONCE during standup to write the Riverdance demo workspace into the portal's private
bucket. Like seed_registry.py it is idempotent in the safe direction: if the workspace object
already exists it REFUSES to overwrite and exits without writing, so re-running can never clobber
edits the client/team made later through the UI.

It also registers `riverdance` in the portal registry (store.add_client, idempotent) so an
"Open workspace" card appears on the portal landing for the demo. That registry add never clobbers
an existing entry.

Usage (from the repo .venv, with ADC configured):
    python seed_workspace.py

Local smoke test WITHOUT touching prod (no GCS/ADC needed):
    set WORKSPACE_LOCAL_DIR=<some dir>   # PowerShell: $env:WORKSPACE_LOCAL_DIR="..."
    python seed_workspace.py             # writes <dir>/workspace/riverdance.json
or run _workspace_localtest.py, which drives the whole data layer against a temp directory.

To force a re-seed of a genuinely broken workspace, delete the workspace/riverdance.json object
first; this script intentionally has no --force flag.
"""

import os
import sys

import brand
import workspace

CLIENT = "riverdance"
DISPLAY_NAME = "Riverdance RV Resort"

# Brand assets live in agora-platform/Creatives/ (the brand kit). The seed runs from the REPO, so it
# READS those files and INLINES them into workspace/<c>.json (brand.agora_logo / brand.client_logo).
# The deployed container only bundles dash/, so it cannot read Creatives/ at runtime -- logos must be
# embedded, self-contained (no external refs). Sources, with graceful fallbacks to brand.py (which IS
# bundled in the container, so the fallback art matches what the portal/login chrome renders):
#   * AGORA master logo : Creatives/logo.svg          -> brand.AGORA_LOGO_LIGHT
#   * Per-client logo   : Creatives/clients/<c>.svg   -> brand.monogram(display_name)
_CREATIVES = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "Creatives"))


def _read_svg(path):
    """Return a self-contained SVG file's text, or None if it is absent/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def agora_logo():
    """The shared AGORA logo from the brand kit (Creatives/logo.svg), else the bundled master mark."""
    return _read_svg(os.path.join(_CREATIVES, "logo.svg")) or brand.AGORA_LOGO_LIGHT


def client_logo(client, display_name):
    """The per-client logo (Creatives/clients/<c>.svg), else a generated initials monogram."""
    return _read_svg(os.path.join(_CREATIVES, "clients", "%s.svg" % client)) or brand.monogram(display_name)


def brand_for(client, display_name):
    """Assemble the {agora_logo, client_logo} brand dict, sourcing the brand kit with fallbacks."""
    return {"agora_logo": agora_logo(), "client_logo": client_logo(client, display_name)}


def riverdance_workspace():
    """Return the full Riverdance demo workspace as a plain dict (pure -- no I/O).

    Kept I/O-free so the local smoke test can build/inspect it without GCS. seed() is what writes
    it. Mirrors the §3 spec: two campaigns (paid + organic), four content pieces, a June calendar,
    three conversations, an activity feed, and the headline metrics.
    """
    return {
        "version": 1,
        "client": CLIENT,
        "display_name": DISPLAY_NAME,
        "tagline": "Client workspace",
        "brand": brand_for(CLIENT, DISPLAY_NAME),

        # Six headline KPIs (trend_up = a good change -> rendered green).
        "metrics": [
            {"icon": "users", "label": "New leads", "value": "148", "trend": "+22%", "trend_up": True},
            {"icon": "calendar", "label": "Bookings", "value": "37", "trend": "+14%", "trend_up": True},
            {"icon": "dollar", "label": "Revenue", "value": "$48.6k", "trend": "+18%", "trend_up": True},
            {"icon": "home", "label": "Occupancy", "value": "82%", "trend": "+9 pts", "trend_up": True},
            {"icon": "tag", "label": "Cost / lead", "value": "$14.20", "trend": "-18%", "trend_up": True},
            {"icon": "trending", "label": "ROAS", "value": "4.3x", "trend": "+0.6", "trend_up": True},
        ],
        "today": {"leads": 9, "visitors": 312, "bookings": 3},
        "split": {"paid": 86, "organic": 62},
        "series": [6, 5, 8, 7, 9, 8, 11, 9, 10, 12, 11, 13, 12, 14],

        # Monthly goal: three independently-configurable tiers (Target / Stretch / Breakthrough).
        # `current` is pulled live from the matching KPI when `source_metric` is set.
        "goal": {"label": "leads", "format": "number",
                 "target": 150, "exceed": 180, "breakthrough": 220,
                 "current": 148, "source_metric": "New leads"},

        # Total reach headline (Overview card): this month vs last month -> ~ +22% MoM.
        "reach": {"current": "38400", "previous": "31500"},

        "activity": [
            {"icon": "check", "text": "You approved the riverside email (RVR-015).",
             "time_label": "Today, 9:12 AM"},
            {"icon": "message", "text": "Maya replied about the Spanish-language $80 post.",
             "time_label": "Yesterday, 4:30 PM"},
            {"icon": "bell", "text": "New content RVR-017 was added for your review.",
             "time_label": "Yesterday, 11:02 AM"},
        ],

        "campaigns": [
            {
                "id": "c_paid_1",
                "channel": "paid",
                "name": "Summer Lead-Gen Push",
                "eyebrow": "PAID ADS · LEAD GEN",
                "strategy": {
                    "what": "Launched Meta and Google campaigns on the $80/night summer offer, with "
                            "three creative angles all pointing at a dedicated riverside landing page.",
                    "why": "The $80 offer drove a 2.3x higher click-through rate than our generic "
                           "rate messaging, and Meta Advantage+ delivered the lowest cost per lead "
                           "— so we shifted 60% of the budget there.",
                    "next": "Scale the winning Meta angle, add a retargeting layer for "
                            "landing-page visitors who didn't convert, and test Google "
                            "Performance Max.",
                },
                "ai_summary": "This campaign is working. The $80 angle is your strongest hook and "
                              "Meta is your cheapest lead source, so we're concentrating spend on "
                              "what converts and adding retargeting to recapture warm visitors.",
                "strategy_doc": "",
                "content": [
                    {
                        "id": "RVR-014", "ref": "RVR-014", "type_tag": "Static Post",
                        "sub_tag": "Lead Gen", "platform": "Instagram · Facebook",
                        "caption": "Riverside mornings start at $80/night. Wake up to the sound of "
                                   "the river, just minutes from Vail. Book your summer escape →",
                        "file_name": "5.png", "thumb_kind": "mountains",
                        "status": "approved",
                        "client_note": "Love this one — the river shot is exactly the feel we "
                                       "want. Approved!",
                        "decided_at": "2026-06-18T15:40:00Z",
                        "comments": [
                            {"sender": "client", "sender_name": "Sarah",
                             "body": "Could we try a sunrise version of this for July?",
                             "created_at": "2026-06-18T16:02:00Z"},
                            {"sender": "agora", "sender_name": "Maya",
                             "body": "Love that — we'll mock up a sunrise cut this week.",
                             "created_at": "2026-06-18T16:20:00Z"},
                        ],
                    },
                    {
                        "id": "RVR-016", "ref": "RVR-016", "type_tag": "Static Post",
                        "sub_tag": "Lead Gen", "platform": "Instagram · Facebook",
                        "caption": "Last call for summer — $80/night riverside sites are going "
                                   "fast. Reserve your spot before the season fills up.",
                        "file_name": "6.png", "thumb_kind": "river",
                        "status": "awaiting", "client_note": "", "decided_at": "",
                        "comments": [],
                    },
                ],
            },
            {
                "id": "c_org_1",
                "channel": "organic",
                "name": "June Nurture & SEO",
                "eyebrow": "ORGANIC · LIFECYCLE",
                "strategy": {
                    "what": "Built a June nurture email sequence, published two SEO posts targeting "
                            "\"RV resort near Vail\", and scheduled four organic social posts.",
                    "why": "Email opens hit 47% and drove 21 bookings at no media cost, and "
                           "\"near Vail\" searches are up 30% — organic is quietly your most "
                           "efficient channel.",
                    "next": "Publish two more SEO pages for high-intent local terms, repurpose the "
                            "top-performing email into a landing page, and start a monthly "
                            "newsletter.",
                },
                "ai_summary": "Your organic engine is punching above its weight — nearly half "
                              "your email list is opening, and local search demand is climbing. "
                              "We're doubling down on the content that already converts for free.",
                "strategy_doc": "",
                "content": [
                    {
                        "id": "RVR-015", "ref": "RVR-015", "type_tag": "Email",
                        "sub_tag": "Nurture", "platform": "Email",
                        "caption": "Subject: Your riverside site is waiting \U0001f3de️ — "
                                   "why June is the best month to visit, plus a guest's favourite "
                                   "morning trail.",
                        "file_name": "email-1.png", "thumb_kind": "email",
                        "status": "approved",
                        "client_note": "Perfect tone. The trail tip is a lovely touch — "
                                       "approved.",
                        "decided_at": "2026-06-19T09:12:00Z",
                    },
                    {
                        "id": "RVR-017", "ref": "RVR-017", "type_tag": "Blog",
                        "sub_tag": "SEO", "platform": "Website",
                        "caption": "The 7 Best RV Resorts Near Vail (and Why Riverdance Tops the "
                                   "List) — a 1,200-word guide targeting \"RV resort near "
                                   "Vail\" with a booking call to action.",
                        "file_name": "blog-1.png", "thumb_kind": "blog",
                        "status": "awaiting", "client_note": "", "decided_at": "",
                    },
                ],
            },
        ],

        "calendar": [
            {"date": "2026-06-02", "label": "Project kickoff", "kind": "milestone"},
            {"date": "2026-06-09", "label": "Weekly sync", "kind": "milestone"},
            {"date": "2026-06-12", "label": "Summer Lead-Gen Push goes live", "kind": "paid"},
            {"date": "2026-06-16", "label": "Weekly sync", "kind": "milestone"},
            {"date": "2026-06-18", "label": "June nurture email sent", "kind": "organic"},
            {"date": "2026-06-22", "label": "UTM tracking due", "kind": "due"},
            {"date": "2026-06-24", "label": "SEO blog post live", "kind": "organic", "status": "done"},
            {"date": "2026-06-25", "label": "Retargeting reel live", "kind": "paid"},
            {"date": "2026-06-26", "label": "Lead magnet deliverables due", "kind": "due"},
            {"date": "2026-06-30", "label": "June deliverables review", "kind": "milestone"},
        ],

        "conversations": [
            {
                "id": "cv_1",
                "subject": "Spanish-language version of the $80 post",
                "status": "awaiting_reply",
                "messages": [
                    {"sender": "client", "sender_name": "Sarah",
                     "body": "Hi team — could we get a Spanish-language version of the "
                             "$80/night riverside post? A lot of our summer guests come from "
                             "Denver's Spanish-speaking communities.",
                     "created_at": "2026-06-19T17:55:00Z"},
                    {"sender": "agora", "sender_name": "Maya",
                     "body": "Great idea, Sarah — we can absolutely localise it. I'll have a "
                             "Spanish adaptation of RVR-016 drafted for your review by Thursday. "
                             "Want us to mirror it on both Meta and Google?",
                     "created_at": "2026-06-19T18:40:00Z"},
                ],
            },
            {
                "id": "cv_2",
                "subject": "June reporting walkthrough",
                "status": "resolved",
                "messages": [
                    {"sender": "client", "sender_name": "Sarah",
                     "body": "Thanks for the May numbers — can we do a quick walkthrough of "
                             "the June dashboard next week?",
                     "created_at": "2026-06-15T16:10:00Z"},
                    {"sender": "agora", "sender_name": "Priya",
                     "body": "Of course! I've put a 30-minute slot on Tuesday at 10am MT and added "
                             "the dashboard link to the calendar. Talk soon.",
                     "created_at": "2026-06-15T16:48:00Z"},
                ],
            },
            {
                "id": "cv_3",
                "subject": "Riverside photo shoot assets",
                "status": "resolved",
                "messages": [
                    {"sender": "agora", "sender_name": "Maya",
                     "body": "The new riverside photos from last week's shoot are in — we'll "
                             "use the best three for the July creative. Anything specific you'd "
                             "like us to feature?",
                     "created_at": "2026-06-12T14:05:00Z"},
                    {"sender": "client", "sender_name": "Sarah",
                     "body": "Love them! Please make sure the fire-pit evening shot makes the cut "
                             "— guests always ask about that.",
                     "created_at": "2026-06-12T15:20:00Z"},
                    {"sender": "agora", "sender_name": "Maya",
                     "body": "Done — it's our hero image for July.",
                     "created_at": "2026-06-12T15:31:00Z"},
                ],
            },
        ],

        # Per-user notification prefs are filled in when a logged-in user saves their settings;
        # get_notify() applies the defaults (on for master/content/replies/summary) for any user
        # not present here.
        "notify": {},
    }


def seed(register_client=True):
    """Write the Riverdance demo workspace if absent. Returns an exit code (0 ok, 1 refused)."""
    if workspace.workspace_exists(CLIENT):
        print(
            "[seed_workspace] workspace/%s.json already exists -- refusing to overwrite. "
            "Delete the object first if you truly want to re-seed." % CLIENT
        )
        return 1

    workspace.save_workspace(CLIENT, riverdance_workspace())
    print("[seed_workspace] seeded workspace/%s.json (%s)." % (CLIENT, DISPLAY_NAME))

    if register_client:
        # Make the demo visible on the portal landing. add_client is idempotent and never clobbers.
        import store  # lazy: the local smoke test runs with register_client=False and no GCS
        store.add_client(CLIENT, DISPLAY_NAME)
        print("[seed_workspace] ensured registry client '%s' exists (idempotent)." % CLIENT)

    return 0


def reapply_brand(client, display_name=None):
    """Refresh ONLY the brand logos on an existing workspace from Creatives (no other field touched).

    Run after dropping/updating Creatives/logo.svg or Creatives/clients/<c>.svg. It does not clobber
    content, decisions, conversations, or notify prefs. Logos live in the workspace JSON and are read
    at render time, so no redeploy is needed afterwards -- just refresh the page.

        python seed_workspace.py --rebrand [<client>]   # defaults to riverdance
    """
    ws = workspace.load_workspace(client)
    if ws is None:
        print("[seed_workspace] no workspace for '%s' to rebrand." % client)
        return 1
    ws["brand"] = brand_for(client, display_name or ws.get("display_name") or client)
    workspace.save_workspace(client, ws)
    print("[seed_workspace] rebranded workspace/%s.json from Creatives." % client)
    return 0


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--rebrand":
        return reapply_brand(argv[1] if len(argv) > 1 else CLIENT)
    return seed()


if __name__ == "__main__":
    sys.exit(main())
