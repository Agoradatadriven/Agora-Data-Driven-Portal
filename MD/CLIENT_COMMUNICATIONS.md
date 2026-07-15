# Communications — features & setup

The **Communications** tab (`/w/<c>/conversations`) is the running record of every conversation with
a client. **Rebuilt 2026-07-15** from a two-column Email | Meeting layout into ONE unified,
channel-tagged, date-filterable timeline, and the old standalone **Mail** tab was folded into it.
This doc lists what it does and how to run it. It is the practical companion to the contract notes in
[`CLAUDE.md`](../CLAUDE.md) (Atrium section) and the dash [`CLAUDE.md`](../services/portal/dash/CLAUDE.md).

> **Nothing here needs new infrastructure.** The timeline, filters, channel badges, audience split,
> and the manual "add" flow all work on a default deploy. Only the folded-in **email** machinery
> (auto-pulling a client's mail) needs a mailbox connected — see §4.

---

## 1. One timeline, many channels

Every conversation is a single card in a date-sorted feed. Each card carries a coloured **channel
badge** — **Email** (violet), **Upwork** (teal), **Slack** (plum), **Meeting** (green), **Call**
(amber), **Note** (grey) — plus a title, an optional "who was involved" line, the summary, and a date.

State is ONE list in the per-client workspace JSON: `ws["communications"]`, each entry
`{id, channel, audience, title, summary, date, people, origin, thread_key}`. `workspace.py` is the
only writer (`add_communication` / `update_communication` / `delete_communication` /
`upsert_email_summary`; `add_email_summary` / `add_meeting_summary` are kept as thin wrappers). The
old split lists (`email_summaries[]` / `meeting_summaries[]`) migrate into this one list in place the
first time it is touched (`workspace._ensure_communications`) — no data is lost.

**How to use (team/super-admin):** click **+ Add a communication**, pick the **Channel** and who can
see it, fill in a title + summary (+ optional date + people), then **Add communication**. Edit or
delete any card in place. Clients see the saved cards read-only.

---

## 2. The client / team split (audience)

Every card has an **audience**: **client** (the client sees it) or **team** (internal only). This is
what lets internal Slack notes live in the same record without ever reaching the client.

- The split is enforced **server-side**: a client render is filtered to `audience=="client"` in
  `main._communications_view` BEFORE the template, so a team card's text never reaches the client's
  HTML (same no-leak posture as the Progress tab's `_progress_tasks`).
- Admins get an **All / Client sees / Team only** toggle and a per-card visibility pill, plus a
  live "N visible to client" count. Use **Preview as client →** to see the exact client feed.

---

## 3. Filtering

A filter bar sits above the timeline, all **client-side** (no reload), and runs for clients too:

- **Channel chips** — All + one per channel present, each with a live count. Multi-select.
- **Date range** — All time · Last 4 weeks · This week.
- **Audience** (admin only) — All · Client sees · Team only.

---

## 4. Email folded in (the former Mail tab)

The client's email archive + AI briefing now lives **inside** Communications, team-only:

- A collapsible **Email intelligence** panel (admin-only) holds the contact list, **Sync now** /
  **Refresh briefing** buttons, the rolling AI digest, and the response-stats strip.
- Email **threads** appear as email-channel cards in the timeline: the **client-tier** recap is
  mirrored as a client-visible card (`upsert_email_summary`, stable `mail_<key>` id); other tiers
  (operations / security / noise) show as **team-only** cards. Each email card has a **Read full
  thread** button (the reader modal).
- To pull mail: connect a mailbox once in the operator console (`/admin/atrium` → **Mailboxes**),
  add this client's addresses in the Email intelligence panel, then **Sync now**. Full setup
  (dwd domain-wide delegation vs imap app password, the hourly `mail-refresh` job) is in
  `mailroom.py` and the dash [`CLAUDE.md`](../services/portal/dash/CLAUDE.md).

The old `/w/<c>/mail` URL now renders the Communications tab (back-compat).

---

## 5. Where the code lives

| File | Responsibility |
|------|----------------|
| `templates/atrium.html` | The Communications pane (Email intelligence panel, filter bar, timeline cards, composer, reader modal) + the filter/admin JS |
| `workspace.py` | `ws["communications"]` data layer: `_ensure_communications` migration, `add/update/delete_communication`, `upsert_email_summary`, `COMM_CHANNELS` |
| `main.py` | `_communications_view` (merge + client/team filter + Mail projection); route `POST /w/<c>/admin/communication` (op add/edit/delete, `channel`+`audience`); `/w/<c>/mail` → Communications |
| `mailroom.py` / `mail_refresh.py` | The folded-in email machinery (pull/archive/summarize; mirrors the client recap into the timeline) |

### Tests (run from `services/portal/dash/`)

```bash
python _workspace_localtest.py   # data layer: unified model, migration, audience
python _atrium_smoketest.py      # routes + render + the client/team no-leak guarantee
python _mail_localtest.py        # the email fold: mirror into the timeline, folded-in panel
python ..\..\..\tools\_validate_dash_js.py templates\atrium.html   # inline-JS gate
```

---

## 6. Planned follow-on (not yet built)

- **AI paste-split for chat channels.** For Slack/Upwork, paste a raw thread and have the model split
  it into **one card per topic, bounded to the week** (e.g. `Upwork · Creative approvals (week of Jun
  2)`). Email/meetings stay one-card-each. There is no summarizer wired for this yet
  (`feedback_ai.py` has no `summarize_conversation_weekly` — the earlier version of this doc described
  it aspirationally; it was never implemented).
