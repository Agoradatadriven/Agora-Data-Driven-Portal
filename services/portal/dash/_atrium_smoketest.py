"""Flask route + template integration smoke test for Agora Atrium (off-cloud, no real GCS).

Stubs google.cloud.storage so main.py (which imports store/feedback) loads without ADC, points the
workspace store at a temp dir, seeds the Riverdance demo there, then drives the real Flask app with
its test client: every client tab renders, and every POST action persists. Proves the route wiring,
the Jinja template, the atrium_dt filter, and atrium_view all work together before any deploy.

Run with a Flask-capable interpreter:
    python _atrium_smoketest.py        # prints PASS / FAIL, exits 0 / 1
"""

import io
import json
import os
import shutil
import sys
import tempfile
import types

# 1. Stub google.cloud.storage BEFORE importing main (store/feedback construct a client at import).
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in smoke test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

# 2. Point the workspace store at a temp dir and sign the session.
_TMP = tempfile.mkdtemp(prefix="atrium_smoke_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP   # admin_atrium console reads the registry (reveal_password)
os.environ["SESSION_SECRET"] = "test-secret"

import seed_workspace   # noqa: E402
import store            # noqa: E402
import workspace        # noqa: E402
import main             # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}


def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def _make_docx(text):
    """Build a minimal-but-valid .docx (a zip with one paragraph) so the docview extraction has a
    real OOXML file to parse -- no python-docx / external dep needed."""
    import zipfile

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:body></w:document>' % text
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def run():
    seed_workspace.seed(register_client=False)
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()

    # Unauthenticated -> redirect to login.
    _check("unauthed /w redirects to login", c.get("/w/%s/" % CLIENT).status_code == 302)

    with c.session_transaction() as s:
        s.update(SUPER)

    # Every client tab renders.
    body = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("overview renders", "Riverdance RV Resort" in body and "Agora Atrium" in body)
    # An unclosed <style>/<script> swallows the ENTIRE body into <head> (blank page in a browser)
    # while every string-presence check still passes -- so check the tags actually balance.
    for tag in ("style", "script"):
        _check("every <%s> is closed (page can render)" % tag,
               body.count("<" + tag) == body.count("</" + tag + ">"))
    _check("greeting present", "Good <span" in body)
    _check("leadgen content present in DOM", "Summer Paid Ads Push" in body)
    _check("organic content present in DOM", "June Nurture &amp; SEO" in body or "June Nurture" in body)
    _check("AI summary present", "AI summary" in body)
    for tab in ("dashboard", "leadgen", "organic", "calendar", "conversations", "settings"):
        _check("tab '%s' returns 200" % tab, c.get("/w/%s/%s" % (CLIENT, tab)).status_code == 200)

    # Communications: the unified timeline + the client/team no-leak guarantee (server-side filter).
    r = c.post("/w/%s/admin/communication" % CLIENT,
               data={"op": "add", "channel": "slack", "audience": "team",
                     "title": "Internal spend note", "summary": "TEAMSECRET reallocate budget"})
    _check("admin adds a team-only communication", r.status_code == 200 and r.get_json().get("ok"))
    r = c.post("/w/%s/admin/communication" % CLIENT,
               data={"op": "add", "channel": "meeting", "audience": "client",
                     "title": "Client kickoff", "summary": "CLIENTVISIBLE kickoff recap"})
    _check("admin adds a client-visible communication", r.get_json().get("ok"))
    admin_conv = c.get("/w/%s/conversations" % CLIENT).get_data(as_text=True)
    _check("admin sees BOTH cards with audience pills + channel badges",
           "TEAMSECRET" in admin_conv and "CLIENTVISIBLE" in admin_conv
           and "Team only" in admin_conv and "Client sees" in admin_conv
           and "ch-slack" in admin_conv and "ch-meeting" in admin_conv)
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    client_conv = c.get("/w/%s/conversations" % CLIENT).get_data(as_text=True)
    # "ax-cm-audseg"/"data-commaddform" also appear as JS string literals, so assert on rendered
    # signals: the team summary text is absent, and none of the admin-only affordances rendered.
    _check("the client sees ONLY their card -- a team-only summary never reaches the client HTML",
           "CLIENTVISIBLE" in client_conv and "TEAMSECRET" not in client_conv
           and 'data-admin="1"' not in client_conv
           and "+ Add a communication" not in client_conv
           and "Client sees" not in client_conv)
    with c.session_transaction() as s:
        s.update(SUPER)

    # Approve an awaiting piece -> persists + confirmation shows on reload.
    r = c.post("/w/%s/approve" % CLIENT, data={"content_id": "RVR-016", "note": "Ship it."})
    _check("approve returns ok json", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, item = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("approval persisted", item["status"] == "approved" and item["client_note"] == "Ship it.")
    _check("confirmation bar on reload",
           "Approved" in c.get("/w/%s/leadgen" % CLIENT).get_data(as_text=True))

    # Re-decide: an already-approved piece can be flipped to changes (status anytime).
    r = c.post("/w/%s/request-changes" % CLIENT, data={"content_id": "RVR-016"})
    _check("re-decide flips approved -> changes",
           r.status_code == 200 and r.get_json().get("status") == "changes")
    r = c.post("/w/%s/approve" % CLIENT, data={"content_id": "RVR-016", "note": "Ship it."})
    _check("re-decide flips back to approved", r.get_json().get("status") == "approved")

    # Request changes on the organic awaiting piece.
    r = c.post("/w/%s/request-changes" % CLIENT, data={"content_id": "RVR-017"})
    _check("request-changes ok", r.status_code == 200 and r.get_json().get("status") == "changes")

    # Client posts a threaded comment on a content piece.
    r = c.post("/w/%s/comment" % CLIENT, data={"content_id": "RVR-017", "body": "Add a guest quote?"})
    _check("client comment ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, c017 = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-017")
    _check("client comment persisted (sender client)",
           c017["comments"][-1]["body"] == "Add a guest quote?" and c017["comments"][-1]["sender"] == "client")

    # Live-state endpoint (drives the no-reload polling): exposes per-content status + comments.
    r = c.get("/w/%s/state.json" % CLIENT)
    sj = r.get_json() if r.status_code == 200 else {}
    _check("state.json returns ok", r.status_code == 200 and sj.get("ok") is True)
    _st017 = (sj.get("content", {}) or {}).get("RVR-017", {})
    _check("state.json carries status + the new comment",
           _st017.get("status") == "changes"
           and any(cm.get("body") == "Add a guest quote?" and cm.get("sender") == "client"
                   for cm in _st017.get("comments", [])))
    _check("state.json gated (logged-out 401/403)",
           main.app.test_client().get("/w/%s/state.json" % CLIENT).status_code in (401, 403))

    # Save a note silently.
    _check("save-note ok",
           c.post("/w/%s/save-note" % CLIENT, data={"content_id": "RVR-014", "note": "Nice"}).status_code == 200)

    # Send a client message -> thread goes awaiting_reply.
    r = c.post("/w/%s/send-message" % CLIENT, data={"conversation_id": "cv_1", "body": "Thanks!"})
    _check("send-message ok", r.status_code == 200 and r.get_json().get("status") == "awaiting_reply")
    _check("message persisted",
           workspace.load_workspace(CLIENT)["conversations"][0]["messages"][-1]["body"] == "Thanks!")

    # Save notification prefs.
    r = c.post("/w/%s/save-notify" % CLIENT,
               data={"master": "1", "content": "0", "replies": "1", "summary": "1",
                     "status": "0", "news": "0", "frequency": "daily"})
    _check("save-notify ok", r.status_code == 200)
    prefs = workspace.get_notify(workspace.load_workspace(CLIENT), SUPER["user"])
    _check("notify persisted", prefs["content"] is False and prefs["frequency"] == "daily")

    # Team console is now the LANDING ONLY. The per-client manage page and its POST routes are GONE:
    # the team edits each workspace IN PLACE via /w/<c>/admin/* (exercised below), and a console card
    # opens /w/<c>/ directly.
    _check("old per-client manage page removed (404)",
           c.get("/admin/atrium/%s" % CLIENT).status_code == 404)
    for path in ("password", "campaign", "content", "conversation", "reply", "metrics"):
        _check("old console POST /%s removed" % path,
               c.post("/admin/atrium/%s/%s" % (CLIENT, path), data={}).status_code in (404, 405))

    # The console landing opens on the Home hub (the "Your Agora suite" welcome), links each client
    # card straight to the workspace, and hides the worked-example `template` client.
    store.add_client(CLIENT, "Riverdance RV Resort")
    store.add_client("template", "Template")
    landing = c.get("/admin/atrium").get_data(as_text=True)
    _check("console landing renders the suite hub welcome",
           "Welcome back" in landing and "Atrium Admin" in landing)
    _check("console card opens the workspace directly", ('href="/w/%s/"' % CLIENT) in landing)
    _check("template client hidden from console", '<div class="name">Template</div>' not in landing)
    store.remove_client("template")

    # ---- In-workspace admin editing (/w/<c>/admin/*), all JSON, super-admin only ----
    # Admin notice bar renders for a super-admin in the real workspace.
    body = c.get("/w/%s/leadgen" % CLIENT).get_data(as_text=True)
    _check("admin edit bar renders for super-admin",
           'class="ax-adminbadge"' in body and 'data-admin="1"' in body)

    # Edit strategy in place.
    r = c.post("/w/%s/admin/strategy" % CLIENT,
               data={"campaign_id": "c_paid_1", "name": "Summer Paid Ads Push v2",
                     "eyebrow": "PAID ADS", "what": "W2", "why": "Y2", "next": "N2"})
    _check("inline strategy ok", r.status_code == 200 and r.get_json().get("ok") is True)
    camp = workspace._find_campaign(workspace.load_workspace(CLIENT), "c_paid_1")
    _check("strategy persisted", camp["strategy"]["what"] == "W2" and camp["name"] == "Summer Paid Ads Push v2")

    # Save a strategy doc link, then generate a summary (AI OFF -> graceful, never 500).
    r = c.post("/w/%s/admin/strategy-doc" % CLIENT,
               data={"campaign_id": "c_paid_1",
                     "doc_url": "https://docs.google.com/document/d/ABC123abc123abc123abc/edit"})
    _check("strategy-doc saved", r.status_code == 200 and r.get_json().get("strategy_doc", "").endswith("/edit"))
    r = c.post("/w/%s/admin/generate-summary" % CLIENT, data={"campaign_id": "c_paid_1"})
    _check("generate-summary degrades gracefully (no 500)", r.status_code == 200)
    _check("generate-summary reports unreadable doc when docs disabled",
           r.get_json().get("ok") is False and r.get_json().get("source") == "none")

    # Hand-edit the AI summary.
    r = c.post("/w/%s/admin/summary" % CLIENT,
               data={"campaign_id": "c_paid_1", "ai_summary": "Hand-written summary."})
    _check("manual summary saved",
           r.status_code == 200 and r.get_json().get("ai_summary") == "Hand-written summary.")

    # Add content in place, then edit + comment as the team + delete it.
    r = c.post("/w/%s/admin/content" % CLIENT,
               data={"campaign_id": "c_paid_1", "ref": "RVR-099", "type_tag": "Reel",
                     "platform": "Instagram", "caption": "A reel for review."})
    _check("inline add-content ok", r.status_code == 200 and r.get_json().get("id") == "RVR-099")

    # Content posted WITH a date mirrors onto the Content Calendar as a linked, paid/leadgen event.
    r = c.post("/w/%s/admin/content" % CLIENT,
               data={"campaign_id": "c_paid_1", "ref": "RVR-100", "type_tag": "Reel",
                     "caption": "Dated reel.", "date": "2026-08-20"})
    _check("inline add-content with date ok", r.status_code == 200)
    _linked = [e for e in workspace.load_workspace(CLIENT).get("calendar", [])
               if e.get("content_id") == "RVR-100"]
    _check("dated content mirrored onto the calendar via the route",
           len(_linked) == 1 and _linked[0]["date"] == "2026-08-20"
           and _linked[0]["kind"] == "paid" and _linked[0]["tab"] == "leadgen")
    c.post("/w/%s/admin/delete-content" % CLIENT, data={"content_id": "RVR-100"})
    _check("deleting dated content removes its calendar event via the route",
           not [e for e in workspace.load_workspace(CLIENT).get("calendar", [])
                if e.get("content_id") == "RVR-100"])
    r = c.post("/w/%s/admin/edit-content" % CLIENT,
               data={"content_id": "RVR-099", "caption": "An edited reel caption."})
    _check("inline edit-content ok", r.status_code == 200)
    _camp, v099 = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("content edit persisted", v099["caption"] == "An edited reel caption.")
    r = c.post("/w/%s/admin/content-comment" % CLIENT,
               data={"content_id": "RVR-099", "body": "Team note.", "sender_name": "Maya"})
    _check("team comment ok", r.status_code == 200)
    _camp, v099b = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("team comment persisted (sender agora)", v099b["comments"][-1]["sender"] == "agora")
    # The Delete-comment control renders for the team on PAID/paid-ads content too (not just organic):
    # RVR-099 lives on c_paid_1, so its comment's Delete button must appear in the leadgen render.
    _paid_cm = v099b["comments"][-1]["id"]
    _check("team Delete-comment button renders on paid content",
           ('data-comdelete="%s"' % _paid_cm) in c.get("/w/%s/leadgen" % CLIENT).get_data(as_text=True))

    # Upload a creative, fetch it back through the authed proxy, then remove it.
    png = b"\x89PNG\r\n\x1a\n" + b"riverdance-creative-bytes"
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(png), "ad.png", "image/png")},
               content_type="multipart/form-data")
    _check("upload-creative ok", r.status_code == 200 and r.get_json().get("ok") is True)
    served = c.get("/w/%s/creative/RVR-099" % CLIENT)
    _check("creative served via authed proxy",
           served.status_code == 200 and served.get_data() == png and served.mimetype == "image/png")
    r = c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})
    _check("remove-creative ok", r.status_code == 200)
    _check("creative 404 after removal", c.get("/w/%s/creative/RVR-099" % CLIENT).status_code == 404)

    # A short VIDEO creative is accepted, served with its mime, and rendered as a <video>.
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"riverdance-clip-bytes"
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(mp4), "reel.mp4", "video/mp4")},
               content_type="multipart/form-data")
    _check("video upload ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, vitem = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("video mime stored", vitem.get("image_mime") == "video/mp4")
    served = c.get("/w/%s/creative/RVR-099" % CLIENT)
    _check("video served with mime", served.status_code == 200 and served.mimetype == "video/mp4")
    vpage = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("workspace renders a playable video thumbnail for the clip",
           ('data-playvideo="/w/%s/creative/RVR-099"' % CLIENT) in vpage)
    _check("uploaded video creative shows a Remove-video button",
           'data-removecreative="RVR-099"' in vpage)
    c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})

    # Add-video "link" half: a pasted URL is stored on the piece, rendered for the client, then cleared.
    r = c.post("/w/%s/admin/video-link" % CLIENT,
               data={"content_id": "RVR-099", "url": "https://example.com/clip.mp4"})
    _check("video-link save ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _camp, litem = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("video_url stored", litem.get("video_url") == "https://example.com/clip.mp4")
    page = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("workspace renders a playable video thumbnail for a direct mp4 link",
           'data-playvideo="https://example.com/clip.mp4"' in page)
    _check("type thumbnail is a clickable play link when a video is attached",
           'ax-ch-playable' in page and 'href="https://example.com/clip.mp4"' in page)
    r = c.post("/w/%s/admin/video-link" % CLIENT,
               data={"content_id": "RVR-099", "url": "javascript:alert(1)"})
    _check("video-link rejects non-http url", r.status_code == 400 and r.get_json().get("ok") is False)
    r = c.post("/w/%s/admin/video-link" % CLIENT, data={"content_id": "RVR-099", "url": ""})
    _camp, litem = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("video-link clear ok", r.status_code == 200 and litem.get("video_url") == "")

    # Local backend: an in-app .mp4 upload OVER the 30 MB cloud cap is accepted (no Cloud Run cap
    # off-cloud), so the same Upload-.mp4 button works locally for big files via the in-app fallback.
    big = b"\x00" * (32 * 1024 * 1024)   # 32 MB > the 30 MB in-app cloud cap
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(big), "big.mp4", "video/mp4")},
               content_type="multipart/form-data")
    _check("local backend accepts a >30 MB in-app .mp4", r.status_code == 200 and r.get_json().get("ok") is True)
    c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})

    # Reject a non-media upload on the LEGACY single-creative route (still image/video only).
    r = c.post("/w/%s/admin/upload-creative" % CLIENT,
               data={"content_id": "RVR-099", "file": (io.BytesIO(b"x"), "a.txt", "text/plain")},
               content_type="multipart/form-data")
    _check("non-media single-creative upload rejected", r.status_code == 400)

    # add-images now accepts ANY file type. A PDF is stored, served INLINE by default (so it previews
    # in an <iframe>) and as an attachment with its original name under ?dl=1, and renders as a live
    # document preview (the doc lightbox), NOT a bare download chip.
    r = c.post("/w/%s/admin/add-images" % CLIENT,
               data={"content_id": "RVR-014", "files": (io.BytesIO(b"%PDF-1.4 hi"), "brief.pdf", "application/pdf")},
               content_type="multipart/form-data")
    j = r.get_json()
    _check("add-images accepts a non-media file",
           r.status_code == 200 and j.get("ok") is True and bool(j.get("added")))
    fid = j["added"][0]["id"]
    served = c.get("/w/%s/creative/RVR-014/%s" % (CLIENT, fid))
    _check("PDF served inline by default (previewable)",
           served.status_code == 200 and served.mimetype == "application/pdf"
           and served.headers.get("Content-Disposition", "").startswith("inline"))
    dl = c.get("/w/%s/creative/RVR-014/%s?dl=1" % (CLIENT, fid))
    _check("PDF served as a download with its name under ?dl=1",
           'attachment; filename="brief.pdf"' in dl.headers.get("Content-Disposition", ""))
    page = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("PDF renders as a doc tile (PDF icon + opens the doc lightbox)",
           'class="ax-shot-media ax-shot-doc"' in page and 'data-doc-kind="pdf"' in page
           and ">PDF</text>" in page and "brief.pdf" in page)
    c.post("/w/%s/admin/remove-image" % CLIENT, data={"content_id": "RVR-014", "image_id": fid})

    # An Office doc (docx) is rendered to a scrollable HTML preview by /docview -- stdlib extraction,
    # so its actual text shows "inside" the iframe; it renders as an 'office' doc preview in the card.
    docx_bytes = _make_docx("Riverdance summer brief. Eagle River access.")
    DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    r = c.post("/w/%s/admin/add-images" % CLIENT,
               data={"content_id": "RVR-014", "files": (io.BytesIO(docx_bytes), "brief.docx", DOCX_MIME)},
               content_type="multipart/form-data")
    j = r.get_json()
    _check("add-images accepts a docx", r.status_code == 200 and bool(j.get("added")))
    did = j["added"][0]["id"]
    dv = c.get("/w/%s/docview/RVR-014/%s" % (CLIENT, did))
    _check("docview renders the docx text inside a scrollable HTML page",
           dv.status_code == 200 and "text/html" in dv.mimetype
           and "Eagle River access" in dv.get_data(as_text=True))
    page = c.get("/w/%s/" % CLIENT).get_data(as_text=True)
    _check("docx renders as a Word doc tile pointing at /docview",
           'data-doc-kind="office"' in page and ">DOC</text>" in page
           and ("/w/%s/docview/RVR-014/%s" % (CLIENT, did)) in page)
    c.post("/w/%s/admin/remove-image" % CLIENT, data={"content_id": "RVR-014", "image_id": did})

    # Signed-URL "bypass the cap" flow (the GCS signing itself needs cloud; here we test the app side).
    # 1) upload-url degrades gracefully on the local backend (no signing) -> ok:false, never crashes.
    r = c.post("/w/%s/admin/creative-upload-url" % CLIENT,
               data={"content_id": "RVR-099", "content_type": "video/mp4"})
    _check("creative-upload-url responds gracefully", r.status_code == 200 and r.get_json().get("ok") is False)
    # 2) confirm records a creative uploaded out-of-band (simulating the direct-to-GCS PUT).
    workspace.write_creative(CLIENT, "RVR-099", b"\x00\x00\x00\x18ftypmp42" + b"0123456789" * 5000, content_type="video/mp4")
    r = c.post("/w/%s/admin/creative-confirm" % CLIENT, data={"content_id": "RVR-099", "content_type": "video/mp4"})
    _check("creative-confirm records the upload", r.status_code == 200 and r.get_json().get("ok") is True)
    # 3) a Range request streams a 206 partial (video seeking + bounded memory).
    served = c.get("/w/%s/creative/RVR-099" % CLIENT, headers={"Range": "bytes=0-1023"})
    body = served.get_data()  # drain the streaming generator so its file handle closes (Windows)
    _check("range request -> 206 partial",
           served.status_code == 206 and len(body) == 1024
           and served.headers.get("Content-Range", "").startswith("bytes 0-1023/"))
    c.post("/w/%s/admin/remove-creative" % CLIENT, data={"content_id": "RVR-099"})

    # Delete the content piece in place.
    r = c.post("/w/%s/admin/delete-content" % CLIENT, data={"content_id": "RVR-099"})
    _check("inline delete-content ok", r.status_code == 200)
    _camp, gone = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-099")
    _check("content deleted", gone is None)

    # Add a campaign in place, then delete it.
    n_before = len(workspace.load_workspace(CLIENT)["campaigns"])
    r = c.post("/w/%s/admin/campaign" % CLIENT,
               data={"channel": "organic", "name": "Inline Organic", "eyebrow": "ORG"})
    _check("inline add-campaign ok", r.status_code == 200)
    new_cid = r.get_json().get("id")
    _check("campaign added", len(workspace.load_workspace(CLIENT)["campaigns"]) == n_before + 1)
    r = c.post("/w/%s/admin/delete-campaign" % CLIENT, data={"campaign_id": new_cid})
    _check("inline delete-campaign ok",
           r.status_code == 200 and len(workspace.load_workspace(CLIENT)["campaigns"]) == n_before)

    # Inline metrics + calendar edits.
    r = c.post("/w/%s/admin/metrics" % CLIENT,
               data={"today_leads": "33", "split_paid": "44", "metric_value_0": "999"})
    _check("inline metrics ok", r.status_code == 200 and workspace.load_workspace(CLIENT)["today"]["leads"] == 33)
    r = c.post("/w/%s/admin/calendar" % CLIENT,
               data={"op": "add", "date": "2026-07-04", "label": "July promo", "kind": "milestone"})
    _check("inline calendar add ok", r.status_code == 200)
    cal_n = len(workspace.load_workspace(CLIENT)["calendar"])
    # Mark the just-added event done, then clear it (the "Mark as done" toggle).
    r = c.post("/w/%s/admin/calendar" % CLIENT, data={"op": "status", "index": str(cal_n - 1), "status": "done"})
    _check("inline calendar mark-done ok",
           r.status_code == 200 and workspace.load_workspace(CLIENT)["calendar"][cal_n - 1].get("status") == "done")
    r = c.post("/w/%s/admin/calendar" % CLIENT, data={"op": "status", "index": str(cal_n - 1), "status": ""})
    _check("inline calendar clear-done ok",
           r.status_code == 200 and "status" not in workspace.load_workspace(CLIENT)["calendar"][cal_n - 1])
    r = c.post("/w/%s/admin/calendar" % CLIENT, data={"op": "delete", "index": str(cal_n - 1)})
    _check("inline calendar delete ok",
           r.status_code == 200 and len(workspace.load_workspace(CLIENT)["calendar"]) == cal_n - 1)

    # Inline reply to a conversation as AGORA.
    r = c.post("/w/%s/admin/reply" % CLIENT,
               data={"conversation_id": "cv_1", "body": "Inline team reply.", "resolve": "1"})
    _check("inline reply ok + resolved",
           r.status_code == 200 and r.get_json().get("status") == "resolved")

    # ---- Market Intelligence (team-written briefing, client-read) --------------------------------
    intel_page = c.get("/w/%s/intel" % CLIENT).get_data(as_text=True)
    _check("market intelligence pane + nav render",
           'data-pane="intel"' in intel_page and 'data-tab="intel"' in intel_page
           and "Market Intelligence" in intel_page)
    _check("seeded intel entry renders", "AI Search Ads expansion" in intel_page)
    _check("super-admin sees the per-section add form",
           "Add to Business Research" in intel_page and "Add to Media Buying News" in intel_page)
    # Add an entry in place, edit it, then delete it.
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "add", "section": "business_research", "heading": "Competitor Watch",
                     "title": "Cruise America expands fleet", "body": "More vehicles in Denver.",
                     "source": "Press release"})
    _check("inline add-intel ok", r.status_code == 200 and r.get_json().get("ok") is True)
    new_eid = r.get_json().get("id")
    _check("intel entry persisted newest-first",
           workspace.load_workspace(CLIENT)["intel"]["business_research"][0]["id"] == new_eid)
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "edit", "section": "business_research", "entry_id": new_eid,
                     "body": "Edited via route."})
    _check("inline edit-intel ok", r.status_code == 200)
    _check("intel edit persisted",
           workspace.load_workspace(CLIENT)["intel"]["business_research"][0]["body"] == "Edited via route.")
    # A bad section is rejected; an empty add is rejected.
    _check("intel rejects unknown section",
           c.post("/w/%s/admin/intel" % CLIENT,
                  data={"op": "add", "section": "nope", "body": "x"}).status_code == 400)
    _check("intel rejects an empty add",
           c.post("/w/%s/admin/intel" % CLIENT,
                  data={"op": "add", "section": "media_buying"}).status_code == 400)
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "delete", "section": "business_research", "entry_id": new_eid})
    _check("inline delete-intel ok",
           r.status_code == 200
           and new_eid not in [e["id"] for e in
                               workspace.load_workspace(CLIENT)["intel"]["business_research"]])

    # ---- Market Intelligence AI brain (model dropdown + tunable prompts + keywords) --------------
    _check("super-admin sees the AI Research Brain panel",
           "AI Research Brain" in intel_page and 'id="ax-intel-model"' in intel_page)
    _check("ai_settings rejects an unknown model",
           c.post("/w/%s/admin/intel" % CLIENT,
                  data={"op": "ai_settings", "model": "gpt-9"}).status_code == 400)
    _check("ai_settings rejects a model whose key isn't configured",
           c.post("/w/%s/admin/intel" % CLIENT,
                  data={"op": "ai_settings", "model": "gemini-2.5-pro"}).status_code == 400)
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "ai_settings", "model": "", "business_prompt": "Watch RV rentals.",
                     "media_prompt": ""})
    _check("ai_settings (off) ok + prompt persisted",
           r.status_code == 200
           and workspace.load_workspace(CLIENT)["intel_ai"]["business_prompt"] == "Watch RV rentals.")
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "topics", "topics": "RV rentals, campgrounds"})
    _check("intel topics saved",
           r.status_code == 200
           and workspace.get_intel_topics(workspace.load_workspace(CLIENT)) == ["RV rentals", "campgrounds"])
    # "Write these for me" (op=suggest): no provider configured -> a friendly reason, never a 500.
    _check("suggest button renders in the AI panel", 'id="ax-intel-suggest"' in intel_page)
    r = c.post("/w/%s/admin/intel" % CLIENT, data={"op": "suggest"})
    _check("suggest with no provider -> ok:false + reason",
           r.status_code == 200 and r.get_json().get("ok") is False
           and "model" in r.get_json().get("message", "").lower())
    # With the AI stubbed, the route returns the three drafts (fields only -- nothing saved).
    import intel_ai   # noqa: E402
    _real_suggest = intel_ai.suggest_config
    intel_ai.suggest_config = lambda name, context="", model=None: (
        {"topics": "boutique RV rentals, roadtrip travellers", "business_prompt": "Watch RV demand.",
         "media_prompt": "Watch travel-ad platforms."}, "")
    try:
        r = c.post("/w/%s/admin/intel" % CLIENT, data={"op": "suggest"})
        j = r.get_json()
        _check("suggest returns the three drafted fields",
               r.status_code == 200 and j.get("ok") is True
               and j.get("topics") == "boutique RV rentals, roadtrip travellers"
               and j.get("business_prompt") and j.get("media_prompt"))
        _check("suggest does NOT save (keywords unchanged until Save settings)",
               workspace.get_intel_topics(workspace.load_workspace(CLIENT)) == ["RV rentals", "campgrounds"])
    finally:
        intel_ai.suggest_config = _real_suggest
    # Bulk favourite + delete on selected entries.
    bid = workspace.add_intel_entry(CLIENT, "media_buying", {"title": "Bulk fav me"})["id"]
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "bulk", "action": "favourite", "section": "media_buying", "entry_ids": bid})
    _check("bulk favourite ok + pinned",
           r.status_code == 200
           and [e for e in workspace.load_workspace(CLIENT)["intel"]["media_buying"]
                if e["id"] == bid][0].get("favourite") is True)
    r = c.post("/w/%s/admin/intel" % CLIENT,
               data={"op": "bulk", "action": "delete", "section": "media_buying", "entry_ids": bid})
    _check("bulk delete ok",
           r.status_code == 200
           and bid not in [e["id"] for e in workspace.load_workspace(CLIENT)["intel"]["media_buying"]])
    _check("bulk rejects a bad action",
           c.post("/w/%s/admin/intel" % CLIENT,
                  data={"op": "bulk", "action": "nuke", "section": "media_buying", "entry_ids": "x"}).status_code == 400)

    # ---- Website Health (team-only tab: admins see it, THE super admin edits) --------------------
    import atrium_health   # noqa: E402
    # Pure tag detection: GTM container + GA4 + Meta pixel are recognised straight from page markup.
    _sample = ('<title>Demo</title>'
               '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC1234"></script>'
               'gtag("config","G-ABCDE12345"); fbq("init","123456789012345");')
    _types = set(t["type"] for t in atrium_health.detect_tags(_sample))
    _check("detect_tags finds GTM + GA4 + Meta", {"gtm", "ga4", "meta"} <= _types)
    _check("detect_tags captures the GTM container id",
           any(t["id"] == "GTM-ABC1234" for t in atrium_health.detect_tags(_sample)))
    # check_website never raises on a dead site (injected fetcher raises) -> graceful ok:false result.
    def _boom(url, timeout):
        raise RuntimeError("getaddrinfo failed")
    _dead = atrium_health.check_website("nope.invalid", fetcher=_boom)
    _check("check_website degrades on a dead site", _dead["ok"] is False and bool(_dead["error"]))

    # Patch the live check so the ROUTE uses a canned result (no real network in the smoke test).
    def _fake_check(url, timeout=10, fetcher=None):
        return {"url": "https://riverdanceresort.com", "input_url": url,
                "checked_at": workspace.now_iso(), "ok": True, "status_code": 200,
                "final_url": "https://riverdanceresort.com", "redirected": False, "https": True,
                "response_ms": 120, "page_title": "Riverdance", "error": "",
                "tags": [{"type": "gtm", "label": "Google Tag Manager", "id": "GTM-RVR123"}],
                "tag_count": 1, "gtm": ["GTM-RVR123"],
                "issues": [{"level": "ok", "text": "Site is online and tags were detected - no problems found."}]}
    atrium_health.check_website = _fake_check

    with c.session_transaction() as s:
        s.update(SUPER)
    wh = c.get("/w/%s/website-health" % CLIENT).get_data(as_text=True)
    _check("website-health pane renders for super-admin",
           'data-pane="website-health"' in wh and "Website Health" in wh)
    _check("website-health nav link present for team", 'data-tab="website-health"' in wh)
    _check("super admin gets the editable URL input", 'id="ax-wh-url"' in wh)
    r = c.post("/w/%s/admin/website-health/save" % CLIENT, data={"url": "riverdanceresort.com"})
    _check("save website url ok", r.status_code == 200 and r.get_json().get("ok") is True)
    _check("website url persisted",
           workspace.load_workspace(CLIENT).get("website_health", {}).get("url") == "riverdanceresort.com")
    r = c.post("/w/%s/admin/website-health/check" % CLIENT, data={"url": "riverdanceresort.com"})
    _check("run health check ok + result stored",
           r.status_code == 200 and r.get_json().get("ok") is True
           and workspace.load_workspace(CLIENT)["website_health"]["last_check"]["gtm"] == ["GTM-RVR123"])
    _check("check result renders (status + GTM container)",
           "GTM-RVR123" in c.get("/w/%s/website-health" % CLIENT).get_data(as_text=True))
    # Running the check normalised + stored the url (https://...); saving notes must NOT clobber it.
    r = c.post("/w/%s/admin/website-health/save" % CLIENT, data={"notes": "Pixel verified."})
    _check("save notes ok + does not clobber the url",
           r.status_code == 200
           and workspace.load_workspace(CLIENT)["website_health"]["url"] == "https://riverdanceresort.com"
           and workspace.load_workspace(CLIENT)["website_health"]["notes"] == "Pixel verified.")

    # An ADMIN who is NOT the root super admin: SEES the tab but it is READ-ONLY, and every edit route
    # is forbidden ("the admin can just see it").
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "staff@agoradatadriven.com", "clients": ["*"]})
    ap = c.get("/w/%s/website-health" % CLIENT).get_data(as_text=True)
    _check("non-root admin sees the Website Health tab", 'data-pane="website-health"' in ap)
    _check("non-root admin view is read-only (no URL editor)", 'id="ax-wh-url"' not in ap)
    _check("non-root admin still sees the stored result", "GTM-RVR123" in ap)
    for path in ("save", "check"):
        _check("website-health/%s forbidden for non-root admin" % path,
               c.post("/w/%s/admin/website-health/%s" % (CLIENT, path), data={}).status_code == 403)

    # A CLIENT never sees the tab and cannot hit its routes.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    cp = c.get("/w/%s/website-health" % CLIENT).get_data(as_text=True)
    _check("client never sees the Website Health nav/pane",
           'data-tab="website-health"' not in cp and 'data-pane="website-health"' not in cp)
    for path in ("save", "check"):
        _check("website-health/%s forbidden for client" % path,
               c.post("/w/%s/admin/website-health/%s" % (CLIENT, path), data={}).status_code == 403)
    with c.session_transaction() as s:
        s.update(SUPER)

    # A non-super-admin grantee can open the workspace but is FORBIDDEN on every admin route.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    _check("grantee can open workspace", c.get("/w/%s/" % CLIENT).status_code == 200)
    _check("grantee cannot see admin bar", 'data-admin="1"' not in c.get("/w/%s/" % CLIENT).get_data(as_text=True))
    # Market Intelligence is client-visible (the nav + seeded entries render) but read-only (no add form).
    gi = c.get("/w/%s/intel" % CLIENT).get_data(as_text=True)
    _check("grantee sees the Market Intelligence tab + entries",
           'data-tab="intel"' in gi and "AI Search Ads expansion" in gi)
    _check("grantee gets a read-only intel view (no add form)", "Add to Business Research" not in gi)
    for path in ("strategy", "campaign", "content", "delete-content", "metrics", "calendar",
                 "generate-summary", "upload-creative", "reply", "intel"):
        _check("admin route /%s forbidden for grantee" % path,
               c.post("/w/%s/admin/%s" % (CLIENT, path), data={}).status_code == 403)
    # But a grantee CAN comment + re-decide (client powers). A "Request changes" comment is a client
    # power; RESOLVING it is TEAM-ONLY -- the grantee is forbidden, and the resolve button is not
    # rendered in their view (gated is_superadmin).
    rc = c.post("/w/%s/comment" % CLIENT,
                data={"content_id": "RVR-014", "body": "please tweak", "kind": "changes"})
    _check("grantee can request changes via comment", rc.status_code == 200)
    cm_id = rc.get_json()["comment"]["id"]
    _check("resolve-comment is team-only (grantee 403)",
           c.post("/w/%s/resolve-comment" % CLIENT,
                  data={"content_id": "RVR-014", "comment_id": cm_id}).status_code == 403)
    _check("resolve button NOT rendered for grantee",
           'data-comresolve="%s"' % cm_id not in c.get("/w/%s/organic" % CLIENT).get_data(as_text=True))

    # A grantee CAN set the client's own logo from inside the workspace (client-facing /w/<c>/logo).
    logo_png = b"\x89PNG\r\n\x1a\n" + b"riverdance-logo-bytes"
    rl = c.post("/w/%s/logo" % CLIENT,
                data={"logo": (io.BytesIO(logo_png), "logo.png", "image/png")},
                content_type="multipart/form-data")
    _check("grantee logo upload ok", rl.status_code == 200 and rl.get_json().get("ok") is True)
    _check("logo persisted inline as a data: URI img",
           "data:image/png;base64," in (workspace.load_workspace(CLIENT).get("brand", {}).get("client_logo") or ""))
    _check("non-image logo upload rejected (400)",
           c.post("/w/%s/logo" % CLIENT,
                  data={"logo": (io.BytesIO(b"x"), "a.txt", "text/plain")},
                  content_type="multipart/form-data").status_code == 400)

    # The team CAN resolve the grantee's change request.
    with c.session_transaction() as s:
        s.update(SUPER)
    _check("team can resolve a change request",
           c.post("/w/%s/resolve-comment" % CLIENT,
                  data={"content_id": "RVR-014", "comment_id": cm_id}).get_json().get("ok") is True)
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})

    # A user who cannot open the client is forbidden.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "x@y.com", "clients": ["someoneelse"]})
    _check("non-grantee forbidden", c.get("/w/%s/" % CLIENT).status_code == 403)
    _check("non-grantee creative forbidden", c.get("/w/%s/creative/RVR-014" % CLIENT).status_code == 403)

    # ---- Task tracker: internal board + client Progress tab (TASK_TRACKER_INTEGRATION.md). ----
    with c.session_transaction() as s:
        s.update(SUPER)
    # Team creates a client-facing task (with internal-only fields) + a purely internal one.
    rt = c.post("/w/%s/admin/task" % CLIENT, data={
        "op": "add", "title": "Park & Porch funnel", "department": "acquisition",
        "lead_id": "zhen@100.digital",
        "priority": "High", "start_date": "2026-07-10", "due_date": "2026-07-20",
        "service_charge": "4200", "client_facing": "1",
        "client_note": "Funnel is live.", "deliverable_url": "https://drive.google.com/x",
        "internal_notes": "INTERNAL-ONLY-MARKER-XYZ",
    })
    _check("team adds a task", rt.status_code == 200 and rt.get_json().get("ok") is True)
    task_id = rt.get_json()["task_id"]
    made = workspace._find_task(workspace.load_workspace(CLIENT), task_id)
    _check("new service always starts In Process", made["stage"] == "in_process")
    _forced = c.post("/w/%s/admin/task" % CLIENT,
                     data={"op": "add", "title": "Stage-forced check",
                           "department": "lifecycle", "stage": "launched"})
    _forced_task = workspace._find_task(workspace.load_workspace(CLIENT), _forced.get_json()["task_id"])
    _check("a submitted stage on create is ignored (always In Process)",
           _forced_task["stage"] == "in_process")
    _check("label auto-derived from the department", made["labels"] == ["Paid Media"])
    _check("start date + service charge stored",
           made["start_date"] == "2026-07-10" and made["service_charge"] == "4200")
    _check("support people assigned after creation (edit patches them)",
           made["support_ids"] == [] and
           c.post("/w/%s/admin/task" % CLIENT,
                  data={"op": "edit", "task_id": task_id, "title": "Park & Porch funnel",
                        "department": "acquisition", "lead_id": "zhen@100.digital",
                        "priority": "High", "client_facing": "1", "has_support": "1",
                        "support_ids": "ehjay@agoradatadriven.com"}).get_json().get("ok") is True
           and workspace._find_task(workspace.load_workspace(CLIENT),
                                    task_id)["support_ids"] == ["ehjay@agoradatadriven.com"])
    _check("internal-only task created",
           c.post("/w/%s/admin/task" % CLIENT,
                  data={"op": "add", "title": "HIDDEN-INTERNAL-TASK"}).get_json().get("ok") is True)
    # Main tasks + sub-tasks + a team comment (the two-level breakdown, via the routes).
    _check("main task added",
           c.post("/w/%s/admin/task/maintask" % CLIENT,
                  data={"op": "add", "task_id": task_id, "text": "SECRET-PHASE-BUILD",
                        "assignee_id": "zhen@100.digital"}).get_json().get("ok") is True)
    main_id = workspace._find_task(workspace.load_workspace(CLIENT), task_id)["maintasks"][0]["id"]
    _check("sub-task added under the main task",
           c.post("/w/%s/admin/task/subtask" % CLIENT,
                  data={"op": "add", "task_id": task_id, "maintask_id": main_id,
                        "text": "Create info pack",
                        "assignee_id": "ehjay@agoradatadriven.com"}).get_json().get("ok") is True)
    sub_id = workspace.task_subtasks(
        workspace._find_task(workspace.load_workspace(CLIENT), task_id))[0]["id"]
    _check("sub-task toggled done",
           c.post("/w/%s/admin/task/subtask" % CLIENT,
                  data={"op": "toggle", "task_id": task_id, "subtask_id": sub_id,
                        "done": "1"}).get_json().get("ok") is True)
    _check("main-task owner reassigned via the route",
           c.post("/w/%s/admin/task/maintask" % CLIENT,
                  data={"op": "assign", "task_id": task_id, "maintask_id": main_id,
                        "assignee_id": "ehjay@agoradatadriven.com"}).get_json().get("ok") is True)
    _check("main task renamed via the route",
           c.post("/w/%s/admin/task/maintask" % CLIENT,
                  data={"op": "rename", "task_id": task_id, "maintask_id": main_id,
                        "text": "SECRET-PHASE-RENAMED"}).get_json().get("ok") is True
           and workspace._find_task(workspace.load_workspace(CLIENT),
                                    task_id)["maintasks"][0]["text"] == "SECRET-PHASE-RENAMED")
    # On hold <-> ongoing via the route (reason is internal-only).
    _check("service put on hold via the route",
           c.post("/w/%s/admin/task/hold" % CLIENT,
                  data={"task_id": task_id, "on_hold": "1",
                        "hold_reason": "HOLD-REASON-INTERNAL-XYZ"}).get_json().get("on_hold") is True)
    _check("hold shows on the console card",
           "tk-hold" in c.get("/admin/atrium").get_data(as_text=True))
    # Reopen-after-action: a console form that carries back_task/back_tab redirects with
    # ?task=&tab= so the page script reopens the same overlay on the same tab.
    back = c.post("/w/%s/admin/task/subtask" % CLIENT,
                  data={"redirect": "console", "op": "toggle", "task_id": task_id,
                        "subtask_id": "nope", "back_task": "%s:%s" % (CLIENT, task_id),
                        "back_tab": "tasks"})
    _check("console redirect carries the reopen params",
           back.status_code == 302 and ("task=%s:%s" % (CLIENT, task_id)) in back.headers["Location"]
           and "tab=tasks" in back.headers["Location"])
    _check("team comments on the task",
           c.post("/w/%s/admin/task/comment" % CLIENT,
                  data={"op": "add", "task_id": task_id,
                        "body": "First draft is up."}).get_json().get("ok") is True)
    # The console renders the board with the task; a console-posted form redirects back to it.
    console = c.get("/admin/atrium").get_data(as_text=True)
    _check("console shows the Task Board nav + the task",
           'data-section="tasks"' in console and "Park &amp; Porch funnel" in console)
    _check("console has the Delivery Calendar tab + pane",
           'data-section="calendar"' in console and 'data-pane="calendar"' in console)
    _check("client creation = page-head button + overlay (inline panel gone)",
           'id="nc-new-btn"' in console and "data-ncnew" in console
           and "Add a new client" not in console)
    _check("scheduled (dated) service becomes a calendar event",
           '<div class="cal-ev" data-date="2026-07-20"' in console
           and 'data-open="%s:%s"' % (CLIENT, task_id) in console)
    _check("console form posts redirect back to the Tasks pane",
           c.post("/w/%s/admin/task/move" % CLIENT,
                  data={"redirect": "console", "task_id": task_id,
                        "stage": "for_launch"}).status_code == 302)

    # The CLIENT sees the Progress tab: their client-facing task, client-safe fields ONLY.
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    pg = c.get("/w/%s/progress" % CLIENT).get_data(as_text=True)
    _check("client Progress tab renders the client-facing task",
           'data-pane="progress"' in pg and "Park &amp; Porch funnel" in pg)
    _check("internal notes never reach the client HTML", "INTERNAL-ONLY-MARKER-XYZ" not in pg)
    _check("lead/support identities never reach the client HTML", "zhen@100.digital" not in pg)
    _check("main-task owner identities never reach the client HTML",
           "ehjay@agoradatadriven.com" not in pg)
    _check("phases (main-task names) DO reach the client", "SECRET-PHASE-RENAMED" in pg)
    _check("campaign chip carries the discipline tint (dept=acquisition -> lb-paid)",
           "ax-pg-chip lb-paid" in pg)
    _check("the service charge never reaches the client HTML", "4200" not in pg and "$4,200" not in pg)
    _check("client sees a plain 'Paused' for a held service", "Paused" in pg)
    _check("the hold reason never reaches the client HTML", "HOLD-REASON-INTERNAL-XYZ" not in pg)
    _check("internal-only tasks never reach the client HTML", "HIDDEN-INTERNAL-TASK" not in pg)
    for path in ("task", "task/move", "task/delete", "task/subtask", "task/maintask", "task/comment"):
        _check("admin task route /%s forbidden for the client" % path,
               c.post("/w/%s/admin/%s" % (CLIENT, path), data={}).status_code == 403)
    # The client's ONE write: comment + request changes.
    _check("client comments on a task",
           c.post("/w/%s/task-comment" % CLIENT,
                  data={"task_id": task_id, "body": "Looks great!"}).get_json().get("ok") is True)
    rchg = c.post("/w/%s/task-comment" % CLIENT,
                  data={"task_id": task_id, "body": "Please swap the hero.", "kind": "changes"})
    _check("client raises a task change request", rchg.get_json().get("open_changes") == 1)
    chg_id = rchg.get_json()["comment"]["id"]
    hidden_id = next(t["id"] for t in workspace.load_workspace(CLIENT)["tasks"]
                     if t["title"] == "HIDDEN-INTERNAL-TASK")
    _check("client cannot comment on an internal-only task (404)",
           c.post("/w/%s/task-comment" % CLIENT,
                  data={"task_id": hidden_id, "body": "hi"}).status_code == 404)

    # Back to the team: the open change request blocks closing, resolving unblocks it.
    with c.session_transaction() as s:
        s.update(SUPER)
    blocked = c.post("/w/%s/admin/task/move" % CLIENT, data={"task_id": task_id, "stage": "closed"})
    _check("close is blocked while a change request is open",
           blocked.get_json().get("ok") is False and "change request" in blocked.get_json()["error"])
    _check("team resolves the change request",
           c.post("/w/%s/admin/task/comment" % CLIENT,
                  data={"op": "resolve", "task_id": task_id,
                        "comment_id": chg_id}).get_json().get("open_changes") == 0)
    _check("close allowed once resolved",
           c.post("/w/%s/admin/task/move" % CLIENT,
                  data={"task_id": task_id, "stage": "closed"}).get_json().get("ok") is True)

    # Delete -> Bin -> restore round-trip.
    _check("task delete is a soft-delete",
           c.post("/w/%s/admin/task/delete" % CLIENT,
                  data={"task_id": task_id}).get_json().get("ok") is True)
    _check("task gone from the workspace",
           workspace._find_task(workspace.load_workspace(CLIENT), task_id) is None)
    import audit as _audit_mod
    entry = next(t for t in _audit_mod.trash_list() if t.get("kind") == "task")
    _check("deleted task is in the Bin", entry["payload"]["id"] == task_id)
    _check("task restore returns it to the board",
           c.post("/admin/atrium/restore", data={"entry_id": entry["id"]}).status_code == 302
           and workspace._find_task(workspace.load_workspace(CLIENT), task_id) is not None)

    # ---- Task board Export / Import (JSON backup + non-destructive restore, super-admin). ----
    with c.session_transaction() as s:
        s.update(SUPER)
    ex = c.get("/admin/atrium/tasks/export")
    _check("task export returns a JSON attachment",
           ex.status_code == 200 and "attachment" in ex.headers.get("Content-Disposition", ""))
    exported = ex.get_json()
    _check("export payload carries this client's tasks",
           CLIENT in exported.get("clients", {}) and exported["clients"][CLIENT]["tasks"])
    with c.session_transaction() as s:
        s.update({"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]})
    _check("client cannot export (super-admin only)",
           c.get("/admin/atrium/tasks/export").status_code == 403)
    _check("client cannot import (super-admin only)",
           c.post("/admin/atrium/tasks/import").status_code == 403)
    # Import an edited copy: change one task's title (update-by-id) + add a brand-new task.
    with c.session_transaction() as s:
        s.update(SUPER)
    imp = dict(exported)
    imp_tasks = list(exported["clients"][CLIENT]["tasks"])
    imp_tasks[0] = dict(imp_tasks[0], title="Imported title change")
    imp_tasks.append({"id": "tk_imported_new", "title": "Imported new task", "stage": "in_process"})
    imp = {"version": 1, "clients": {CLIENT: {"name": "Riverdance", "tasks": imp_tasks},
                                     "ghostclient": {"tasks": [{"id": "x", "title": "skip me"}]}}}
    ri = c.post("/admin/atrium/tasks/import",
                data={"file": (io.BytesIO(json.dumps(imp).encode()), "backup.json", "application/json")},
                content_type="multipart/form-data")
    _check("import redirects back to the Tasks pane", ri.status_code == 302)
    after = {t["id"]: t for t in workspace.load_workspace(CLIENT)["tasks"]}
    _check("import UPDATED an existing task by id", after[imp_tasks[0]["id"]]["title"] == "Imported title change")
    _check("import ADDED the new task", "tk_imported_new" in after)
    _check("import skipped a client that no longer exists",
           workspace.load_workspace("ghostclient") is None)

    print("[smoketest] PASS")
    return 0


def main_():
    try:
        return run()
    except AssertionError as exc:
        print("[smoketest] FAIL: %s" % exc)
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_())
