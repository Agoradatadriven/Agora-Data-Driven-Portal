"""Local smoke test for the Atrium data layer -- runs entirely off-cloud, never touches prod.

It points the workspace store at a throwaway temp directory (WORKSPACE_LOCAL_DIR), seeds the
Riverdance demo there, then exercises every client-facing mutation and re-reads from disk to prove
persistence. No GCS, no ADC, and no `google-cloud-storage` package required.

    python _workspace_localtest.py        # prints PASS / FAIL and exits 0 / 1
"""

import os
import shutil
import sys
import tempfile

# Point the store at a temp dir BEFORE importing the module under test.
_TMP = tempfile.mkdtemp(prefix="atrium_localtest_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP

import workspace            # noqa: E402  (must follow the env setup above)
import seed_workspace       # noqa: E402

CLIENT = "riverdance"
USER = "owner@riverdanceresort.com"


def _check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def run():
    print("[localtest] WORKSPACE_LOCAL_DIR = %s" % _TMP)

    # 1. Seed writes the object; re-seeding refuses to clobber.
    rc = seed_workspace.seed(register_client=False)
    _check("first seed returns 0", rc == 0)
    _check("workspace object exists after seed", workspace.workspace_exists(CLIENT))
    _check("re-seed refuses to clobber (returns 1)", seed_workspace.seed(register_client=False) == 1)

    # 2. The seeded shape matches the spec.
    ws = workspace.load_workspace(CLIENT)
    _check("display_name seeded", ws["display_name"] == "Riverdance RV Resort")
    _check("six KPI metrics", len(ws["metrics"]) == 6)
    _check("two campaigns (paid + organic)",
           [c["channel"] for c in ws["campaigns"]] == ["paid", "organic"])
    _check("14-point leads series", len(ws["series"]) == 14)
    _check("three conversations", len(ws["conversations"]) == 3)
    _check("intel seeded with both sections",
           len(ws["intel"]["business_research"]) == 3 and len(ws["intel"]["media_buying"]) == 2)

    # 3. Approve an awaiting piece with a note.
    item = workspace.decide_content(CLIENT, "RVR-016", "approved", note="Looks great, ship it.")
    _check("decide_content set status approved", item["status"] == "approved")
    _check("decide_content stamped decided_at", item["decided_at"].endswith("Z"))
    _check("decide_content saved the note", item["client_note"] == "Looks great, ship it.")

    # 4. Standalone note on another piece.
    workspace.set_content_note(CLIENT, "RVR-017", "Can we add a guest quote?")
    _camp, rvr017 = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-017")
    _check("set_content_note persisted", rvr017["client_note"] == "Can we add a guest quote?")

    # 5. Client sends a message -> thread goes awaiting_reply.
    conv, msg = workspace.add_message(CLIENT, "cv_1", "client", "Sarah",
                                      "Thursday works great, thank you!", set_status="awaiting_reply")
    _check("message appended", conv["messages"][-1]["body"].startswith("Thursday works"))
    _check("message sender recorded", msg["sender"] == "client")
    _check("conversation status updated", conv["status"] == "awaiting_reply")

    # 6. Notification prefs: defaults for an unknown user, merge on save.
    defaults = workspace.get_notify(ws, "nobody@example.com")
    _check("default notify: master on", defaults["master"] is True)
    _check("default notify: status off", defaults["status"] is False)
    saved = workspace.set_notify(CLIENT, USER, {"status": True, "frequency": "daily"})
    _check("set_notify merged status", saved["status"] is True)
    _check("set_notify merged frequency", saved["frequency"] == "daily")
    _check("set_notify kept defaults", saved["replies"] is True)

    # 7. Activity prepends, most-recent first.
    workspace.add_activity(CLIENT, "check", "You approved RVR-016.", "Today, 10:00 AM")
    _check("activity prepended",
           workspace.load_workspace(CLIENT)["activity"][0]["text"] == "You approved RVR-016.")

    # 8a. Threaded comments on a content piece (client + team).
    item, comment = workspace.add_content_comment(CLIENT, "RVR-016", "client", "Sarah", "Can we brighten it?")
    _check("comment appended", comment["body"] == "Can we brighten it?")
    workspace.add_content_comment(CLIENT, "RVR-016", "agora", "Maya", "On it!")
    _camp, rvr016c = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("two comments persisted", len(rvr016c["comments"]) == 2 and rvr016c["comments"][-1]["sender"] == "agora")

    # 8b. Strategy doc attach + campaign update with new fields.
    workspace.set_strategy_doc(CLIENT, "c_paid_1", "https://docs.google.com/document/d/ABC123abc123abc123abc/edit")
    _check("strategy_doc saved",
           workspace._find_campaign(workspace.load_workspace(CLIENT), "c_paid_1")["strategy_doc"].endswith("/edit"))

    # 8c. Creative image bytes round-trip + pointer set/clear.
    obj = workspace.write_creative(CLIENT, "RVR-016", b"\x89PNG\r\n\x1a\nFAKE", content_type="image/png")
    _check("creative object name under prefix", obj == "workspace/creatives/riverdance/RVR-016")
    _check("creative bytes round-trip", workspace.read_creative_bytes(CLIENT, "RVR-016") == b"\x89PNG\r\n\x1a\nFAKE")
    workspace.set_content_image(CLIENT, "RVR-016", obj, "image/png")
    _camp, rvr016i = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("image pointer set", rvr016i["image_object"] == obj and rvr016i["image_mime"] == "image/png")
    workspace.clear_content_image(CLIENT, "RVR-016")
    workspace.delete_creative(CLIENT, "RVR-016")
    _camp, rvr016n = workspace._find_content(workspace.load_workspace(CLIENT), "RVR-016")
    _check("image pointer cleared", "image_object" not in rvr016n)
    _check("creative bytes deleted", workspace.read_creative_bytes(CLIENT, "RVR-016") is None)

    # 8d. Delete content + delete campaign.
    n_org = len(workspace._find_campaign(workspace.load_workspace(CLIENT), "c_org_1")["content"])
    workspace.delete_content(CLIENT, "RVR-017")
    _check("content removed",
           len(workspace._find_campaign(workspace.load_workspace(CLIENT), "c_org_1")["content"]) == n_org - 1)
    workspace.delete_campaign(CLIENT, "c_org_1")
    _check("campaign removed",
           workspace._find_campaign(workspace.load_workspace(CLIENT), "c_org_1") is None)

    # 8e. Calendar add + delete.
    before_cal = len(workspace.load_workspace(CLIENT).get("calendar", []))
    workspace.add_calendar_event(CLIENT, "2026-07-04", "Independence Day promo", "milestone")
    _check("calendar event added", len(workspace.load_workspace(CLIENT)["calendar"]) == before_cal + 1)
    workspace.delete_calendar_event(CLIENT, before_cal)
    _check("calendar event deleted", len(workspace.load_workspace(CLIENT)["calendar"]) == before_cal)

    # 8f. Content with a date mirrors onto the Content Calendar as a linked event; editing the date
    #     re-syncs it (paid -> 'paid'/leadgen); clearing the date removes it; deleting the piece too.
    base_cal = len(workspace.load_workspace(CLIENT).get("calendar", []))
    dated = workspace.add_content(CLIENT, "c_paid_1",
                                  {"ref": "Dated teaser", "date": "2026-08-01"})
    linked = [e for e in workspace.load_workspace(CLIENT)["calendar"]
              if e.get("content_id") == dated["id"]]
    _check("dated content mirrored to calendar", len(linked) == 1)
    _check("linked event is paid/leadgen",
           linked and linked[0]["kind"] == "paid" and linked[0]["tab"] == "leadgen")
    _check("linked event carries the date + label",
           linked and linked[0]["date"] == "2026-08-01" and linked[0]["label"] == "Dated teaser")
    workspace.update_content(CLIENT, dated["id"], {"date": "2026-08-15", "ref": "Renamed teaser"})
    relinked = [e for e in workspace.load_workspace(CLIENT)["calendar"]
                if e.get("content_id") == dated["id"]]
    _check("edit re-synced the linked event (no duplicate)", len(relinked) == 1)
    _check("edit overwrote date + label on the event",
           relinked and relinked[0]["date"] == "2026-08-15" and relinked[0]["label"] == "Renamed teaser")
    workspace.update_content(CLIENT, dated["id"], {"date": ""})
    _check("clearing the date removes the linked event",
           not [e for e in workspace.load_workspace(CLIENT)["calendar"]
                if e.get("content_id") == dated["id"]])
    # A piece WITHOUT a date never touches the calendar.
    undated = workspace.add_content(CLIENT, "c_paid_1", {"ref": "No date"})
    _check("undated content adds no calendar event",
           len(workspace.load_workspace(CLIENT)["calendar"]) == base_cal)
    # Re-date it, then delete the piece: the linked event goes with it.
    workspace.update_content(CLIENT, undated["id"], {"date": "2026-09-09"})
    _check("re-dating creates the linked event",
           len(workspace.load_workspace(CLIENT)["calendar"]) == base_cal + 1)
    workspace.delete_content(CLIENT, undated["id"])
    _check("deleting the piece removes its linked event",
           len(workspace.load_workspace(CLIENT)["calendar"]) == base_cal)

    # 8g. Market Intelligence: add (newest-first) + edit + delete an entry, and reject a bad section.
    before_mb = len(workspace.load_workspace(CLIENT)["intel"]["media_buying"])
    entry = workspace.add_intel_entry(CLIENT, "media_buying",
                                      {"heading": "Meta Updates", "title": "Advantage+ broadens",
                                       "body": "More automated placements.", "source": "Meta"})
    mb = workspace.load_workspace(CLIENT)["intel"]["media_buying"]
    _check("intel entry added newest-first",
           len(mb) == before_mb + 1 and mb[0]["id"] == entry["id"] and mb[0]["title"] == "Advantage+ broadens")
    workspace.update_intel_entry(CLIENT, "media_buying", entry["id"], {"body": "Edited body."})
    mb = workspace.load_workspace(CLIENT)["intel"]["media_buying"]
    _check("intel entry edited in place", mb[0]["body"] == "Edited body.")
    workspace.delete_intel_entry(CLIENT, "media_buying", entry["id"])
    _check("intel entry deleted",
           len(workspace.load_workspace(CLIENT)["intel"]["media_buying"]) == before_mb)
    try:
        workspace.add_intel_entry(CLIENT, "not_a_section", {"body": "x"})
        _check("unknown intel section raises", False)
    except KeyError:
        _check("unknown intel section raises", True)

    # 8h. Task tracker: the full add / edit / move / sub-task / comment / delete round-trip.
    task = workspace.add_task(CLIENT, {
        "title": "Park & Porch — lead-gen funnel", "department": "acquisition",
        "lead_id": "zhen@100.digital", "support_ids": ["ehjay@agoradatadriven.com", "zhen@100.digital"],
        "priority": "High", "labels": ["Paid Media"], "campaign": "Park & Porch | Leads",
        "content_type": "Funnel", "due_date": "2026-07-20", "client_facing": True,
        "client_note": "Funnel is live.", "deliverable_url": "https://drive.google.com/x",
        "internal_notes": "Watch CPL.",
    }, actor="info@agoradatadriven.com")
    _check("task created with id + default stage",
           task["id"].startswith("tk_") and task["stage"] == "in_process")
    _check("task lead never duplicated into support", task["support_ids"] == ["ehjay@agoradatadriven.com"])
    _check("task history stamped created", task["history"][0]["field"] == "created")
    try:
        workspace.add_task(CLIENT, {"title": "bad", "stage": "not_a_stage"})
        _check("unknown task stage raises", False)
    except KeyError:
        _check("unknown task stage raises", True)

    workspace.update_task(CLIENT, task["id"],
                          {"priority": "Urgent", "support_ids": ["zhen@100.digital", "ian@100.digital"]})
    t2 = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("task edit patched priority", t2["priority"] == "Urgent")
    _check("task edit re-enforced lead-not-in-support", t2["support_ids"] == ["ian@100.digital"])

    # Two-level breakdown: a sub-task with no main task grows a group named after the content
    # type; explicit main tasks carry their own owner and their own subs.
    _task, sub1 = workspace.add_subtask(CLIENT, task["id"], "Propose funnel", "zhen@100.digital")
    _task, sub2 = workspace.add_subtask(CLIENT, task["id"], "Create info pack")
    _task, mt_qa = workspace.add_maintask(CLIENT, task["id"], "QA", "ian@100.digital")
    _task, sub3 = workspace.add_subtask(CLIENT, task["id"], "QA events fire",
                                        "ian@100.digital", maintask_id=mt_qa["id"])
    workspace.set_subtask_done(CLIENT, task["id"], sub1["id"], True)
    workspace.set_subtask_owner(CLIENT, task["id"], sub2["id"], "ehjay@agoradatadriven.com")
    t3 = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    mains = t3["maintasks"]
    _check("auto main task named after the content type",
           len(mains) == 2 and mains[0]["text"] == "Funnel" and mains[1]["text"] == "QA")
    _check("main task carries its own owner", mains[1]["assignee_id"] == "ian@100.digital")
    _check("sub-tasks persisted with done + owner across groups",
           mains[0]["subs"][0]["done"] is True
           and mains[0]["subs"][1]["assignee_id"] == "ehjay@agoradatadriven.com"
           and mains[1]["subs"][0]["id"] == sub3["id"])
    _check("task_subtasks flattens every group",
           [s["id"] for s in workspace.task_subtasks(t3)] == [sub1["id"], sub2["id"], sub3["id"]])
    workspace.set_maintask_owner(CLIENT, task["id"], mt_qa["id"], "ehjay@agoradatadriven.com")
    t3b = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("main-task owner reassigned", t3b["maintasks"][1]["assignee_id"] == "ehjay@agoradatadriven.com")

    # Legacy flat subtasks migrate in place the first time the task is touched.
    legacy = {"id": "tk_legacy", "title": "Old shape", "stage": "in_process", "lead_id": "zhen@100.digital",
              "content_type": "Report", "subtasks": [{"id": "st_a", "text": "Old sub", "done": True,
                                                      "assignee_id": ""}]}
    workspace.normalize_task(legacy)
    _check("legacy flat subtasks migrate into one main task",
           "subtasks" not in legacy and len(legacy["maintasks"]) == 1
           and legacy["maintasks"][0]["text"] == "Report"
           and legacy["maintasks"][0]["assignee_id"] == "zhen@100.digital"
           and legacy["maintasks"][0]["subs"][0]["id"] == "st_a")
    _check("normalize is idempotent",
           workspace.normalize_task(legacy)["maintasks"][0]["subs"][0]["text"] == "Old sub")

    # Every service has a start date: created-without-one defaults to today; legacy tasks
    # backfill theirs from the creation day.
    dated = workspace.add_task(CLIENT, {"title": "No start given"})
    _check("new task defaults start_date to its creation day",
           dated["start_date"] == dated["created_at"][:10])
    old = workspace.normalize_task({"id": "tk_old", "title": "Pre-start-date task",
                                    "created_at": "2026-07-01T09:00:00Z"})
    _check("legacy task backfills start_date from created_at", old["start_date"] == "2026-07-01")

    # On hold <-> ongoing: a plain boolean + internal reason, with a hold/resume history entry.
    workspace.set_task_hold(CLIENT, task["id"], True, "Client asked to pause", actor="info@agoradatadriven.com")
    th = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("task put on hold with reason",
           th["on_hold"] is True and th["hold_reason"] == "Client asked to pause"
           and th["history"][-1]["field"] == "hold" and th["history"][-1]["new"] == "on hold")
    workspace.set_task_hold(CLIENT, task["id"], False, actor="info@agoradatadriven.com")
    th2 = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("task resumed clears the reason",
           th2["on_hold"] is False and th2["hold_reason"] == "" and th2["history"][-1]["new"] == "resumed")

    # A client change request blocks closing until the team resolves it (and open sub-tasks block too).
    _task, chg = workspace.add_task_comment(CLIENT, task["id"], "client", "Daniela",
                                            "Please swap the hero image.", kind="changes")
    _check("change request recorded unresolved", chg["resolved"] is False)
    _check("open-changes flag derives from the thread",
           len(workspace.task_open_changes(t3)) == 0 and
           len(workspace.task_open_changes(workspace._find_task(workspace.load_workspace(CLIENT), task["id"]))) == 1)
    try:
        workspace.move_task_stage(CLIENT, task["id"], "closed")
        _check("close blocked while sub-task + change request open", False)
    except ValueError as exc:
        _check("close blocked while sub-task + change request open",
               "Create info pack" in str(exc) and "change request" in str(exc))
    workspace.move_task_stage(CLIENT, task["id"], "launched", actor="info@agoradatadriven.com")
    t4 = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("stage move recorded in history",
           t4["stage"] == "launched" and t4["history"][-1]["old"] == "in_process"
           and t4["history"][-1]["new"] == "launched")
    workspace.resolve_task_comment(CLIENT, task["id"], chg["id"])
    workspace.set_subtask_done(CLIENT, task["id"], sub2["id"], True)
    workspace.set_subtask_done(CLIENT, task["id"], sub3["id"], True)
    workspace.move_task_stage(CLIENT, task["id"], "closed")
    _check("close allowed once sub-tasks done + changes resolved",
           workspace._find_task(workspace.load_workspace(CLIENT), task["id"])["stage"] == "closed")

    workspace.delete_subtask(CLIENT, task["id"], sub2["id"])
    _check("sub-task deleted",
           len(workspace.task_subtasks(
               workspace._find_task(workspace.load_workspace(CLIENT), task["id"]))) == 2)
    workspace.delete_maintask(CLIENT, task["id"], mt_qa["id"])
    t5 = workspace._find_task(workspace.load_workspace(CLIENT), task["id"])
    _check("main task deleted with its sub-tasks",
           len(t5["maintasks"]) == 1 and len(workspace.task_subtasks(t5)) == 1)
    removed = workspace.delete_task(CLIENT, task["id"])
    _check("delete_task returns the payload for the Trash", removed["id"] == task["id"])
    _check("task gone after delete",
           workspace._find_task(workspace.load_workspace(CLIENT), task["id"]) is None)
    workspace.insert_task(CLIENT, removed)
    workspace.insert_task(CLIENT, removed)   # double-restore must not duplicate
    _check("insert_task restores once (idempotent)",
           len([t for t in workspace.load_workspace(CLIENT)["tasks"] if t["id"] == task["id"]]) == 1)

    # 9. Everything survived a reload from disk.
    reloaded = workspace.load_workspace(CLIENT)
    _camp, rvr016 = workspace._find_content(reloaded, "RVR-016")
    _check("approval persisted across reload", rvr016["status"] == "approved")
    _check("notify persisted across reload", reloaded["notify"][USER]["status"] is True)
    _check("comments persisted across reload", len(rvr016["comments"]) == 2)

    print("[localtest] PASS")
    return 0


def main():
    try:
        return run()
    except AssertionError as exc:
        print("[localtest] FAIL: %s" % exc)
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
