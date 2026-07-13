# Task Tracker + Client Progress — Integration Spec

> **Status: BUILT (2026-07-13) in the working tree — all five phases (§9), pending commit → PR →
> CI → deploy.** `workspace.py` helpers + `_workspace_localtest.py`, the console Delivery → Task
> Board (`admin_atrium.html` + `/w/<c>/admin/task*` routes), the client Progress tab
> (`atrium.html` + `/w/<c>/task-comment`), the comment loop + `notify.py` task functions, stage
> guards, Bin restore (`kind:"task"`), audit entries, and the `_atrium_smoketest.py` coverage
> (routes, gating, no-leak render) are all in place and passing. The §13 prompts remain useful as
> the per-phase review map. Original instruction set below, unchanged.
> Companion planning docs: `ATRIUM_CONSOLE_REDESIGN_PLAN.md`.
> **Design contract:** §14 is the pixel-level spec (tokens + component specs) taken from the two
> prototypes — build/verify the UI against it.

---

## 1. What this is

A **task/delivery tracker** for Agora's client work, in two surfaces that share **one data model**:

1. **Internal Task Board** (team-only) — every client deliverable as a card on a stage board, with
   Lead + Support people, sub-tasks (each with its own owner), comments, and an internal vs
   client-safe field split.
2. **Client "Progress" tab** (client-facing) — a **read-only** view inside the client's own Atrium
   workspace showing only *that* client's *client-safe* deliverables and where each stands. The one
   thing a client can do is **comment / request changes** (mirrors the existing content-approval loop).

**Design principle already decided:** a **service/deliverable travels across four stages**
(In Process → For Launch → Launched → Closed). The internal board and the client board are the same
tasks; the client board relabels the stages and hides internal fields.

---

## 2. Prototype files (source of truth for the UI)

| File | Surface | Notes |
|------|---------|-------|
| `atrium/task_tracker_prototype.html` | Internal Task Board (Kanban) | Stages as columns, drag-to-move, detail modal (sub-tasks + owners, Lead/Support, comments, activity), New/Edit form, delete→Archived, filters, localStorage persistence (versioned via `SEED_VERSION`). Themed to the **admin console** design system (green `#4FA84A` + purple `#5A54DD`, website tokens). |
| `atrium/client_progress_prototype.html` | Client "Progress" tab | The real client-workspace chrome (sidebar + top bar) with **Progress** added as a nav tab. Read-only board + client comment/request-changes. Themed to the **client workspace** design system (`--ax-*`, Roboto, real `assets/logo.svg` + `assets/clients/*.svg`). |
| `TASKTRACKER/Agora_Task_Tracker_Atrium.html` | Alt internal (matrix) | Earlier client×stage **matrix** layout + full launch-gate/approval logic + Dates calendar. Reference for the matrix option and the guard rules. |

Open any prototype directly (`file://`). They are ephemeral demos: mock data, no backend.

---

## 3. Product / data model

### 3.1 Stages (internal key → client label)

The **key is canonical** (used in routes/data); the client sees a friendlier label.

| Stage key | Internal label | Client label | Dot colour |
|-----------|----------------|--------------|-----------|
| `in_process` | In Process | **In progress** | blue |
| `for_launch` | For Launch | **In review** | amber/orange |
| `launched` | Launched | **Live** | green |
| `closed` | Closed | **Completed** | grey |

> Never rename the keys once shipped (same rule as the `leadgen` tab key). Colour note: the internal
> board reserves **red** for at-risk/overdue flags, so the *In Process* stage is **blue**, not red.

### 3.2 Task shape (stored per client)

Store a `tasks` list on each client's workspace JSON. Proposed shape (mirrors `content[]`):

```jsonc
{
  "id": "t_ab12cd",
  "title": "Park & Porch — lead-gen funnel",
  "stage": "in_process",              // in_process | for_launch | launched | closed
  "department": "acquisition",        // acquisition | lifecycle | data | development | creative | bidbrain
  "lead_id": "acct_or_person_id",     // the MAIN lead
  "support_ids": ["id2", "id3"],      // support people (never includes lead_id)
  "priority": "High",                 // Low | Medium | High | Urgent  (internal only)
  "labels": ["Paid Media", "Strategy"],
  "campaign": "Park & Porch | Leads",  // from the Campaign Reference list
  "content_type": "Funnel",
  "due_date": "2026-07-18",            // ISO date or null
  "subtasks": [                        // "main task + sub-tasks", each with its OWN owner
    { "id": "s1", "text": "Create info pack", "done": false, "assignee_id": "id3" }
  ],
  "comments": [                        // reuse the content-comment pattern (see 3.4)
    { "id": "c1", "sender": "agora", "body": "...", "kind": null, "resolved": false, "ts": "..." }
  ],
  "history": [                         // activity log (stage moves, edits)
    { "actor_id": "id", "field": "stage", "old": "for_launch", "new": "launched", "at": "..." }
  ],

  // ---- client-safe (syncs to the client Progress tab) ----
  "client_facing": true,              // if false, the client NEVER sees this task
  "client_note": "Funnel is live — first leads coming in.",
  "deliverable_url": "https://…",

  // ---- internal only (NEVER sent to the client) ----
  "internal_notes": "Watching CPL before scaling.",
  "account_manager_id": "id"
}
```

> **client_id is implicit** — the task lives in that client's `workspace/<c>.json`, so it does not
> need to be stored on the task. The internal cross-client board derives the client by which file it
> read the task from.

### 3.3 People (Lead + Support + sub-task owners)

- Assignees come from the **team roster**. Source options (pick one, document it): the admin
  `accounts` in `store.py` (role `admin`/`superadmin`), or a small dedicated team list. The roster
  used in the prototypes: Charles, Christian, Ehjay, Ian, Jerome, John, Justine, Lance, Nico, Paulo,
  Samuel, Zhen — departments Acquisition / Lifecycle / Data Analyst / Development / Bidbrain.
- **Lead** = the one owner; **Support** = 0..n helpers. A person filter matches lead **or** support.
- **Sub-tasks each carry an optional `assignee_id`** so a support member can own a specific piece.
- **Never expose lead/support/owner identities or `internal_notes`/`priority` to the client.**

### 3.4 Comments (reuse the existing content-comment flow)

Atrium already has threaded content comments with a `kind:"changes"` (client "request changes" that
flips status and carries `resolved`) and team-only `resolve`. **Reuse that exact pattern** for tasks:

- Client can **Comment** and **Request changes** on a client-facing task (client power).
- Team can comment and **Resolve** a change request (team-only, `is_superadmin()`), which returns the
  task to the prior stage/`awaiting` state.
- Bubble styling already exists (`.ax-com`, `.ax-com.client`, `.ax-com.agora`, `.ax-com.changes`,
  `.ax-com.resolved`) in `atrium.html` — the client prototype copies it verbatim.

---

## 4. Architecture & storage (no new infra)

Follows the Atrium contract exactly — **no database, no new bucket/service/IAM**:

- **State** = one more key `ws["tasks"]` in `workspace/<c>.json` (registry bucket
  `agora-data-driven-platform-dash`). `dash/workspace.py` is the **only** reader/writer.
- Add `workspace.py` helpers mirroring `add_content`/`update_content`/`delete_content`:
  `add_task`, `update_task`, `move_task_stage`, `delete_task`, `add_task_comment`,
  `resolve_task_comment`, `add_subtask`, `set_subtask_done`, `set_subtask_owner`. Last-write-wins.
- Keep the local-fs backend working (`WORKSPACE_LOCAL_DIR`) so it tests off-cloud.
- **client_facing sync is free**: the client Progress tab reads the same `ws["tasks"]`, filtered to
  `client_facing == true`, and renders only client-safe fields. No copy/export step.

---

## 5. Surface A — Internal Task Board (team-only)

**Where:** it is cross-client (every client's tasks on one board). Two viable homes:

- **(Recommended)** a new **Delivery / Tasks** section in the operator console `admin_atrium.html`
  (which already loads every workspace for the awaiting-count). `main.admin_atrium()` walks clients,
  collects `ws["tasks"]`, tags each with its client, and passes the list to the template.
- Or a team-only tab per client workspace (narrower; loses the cross-client view).

**Theme:** the admin console design system (website tokens: green `#4FA84A`, purple `#5A54DD`).
Port from `task_tracker_prototype.html` — its CSS already uses those tokens.

**Routes to add** (team-only, gated `is_superadmin()`, mirror the `/w/<c>/admin/*` shape):

| Method + path | Purpose |
|---|---|
| `POST /w/<c>/admin/task` | create / edit a task (`op=add\|edit`) |
| `POST /w/<c>/admin/task/move` | change stage (guarded — see below) |
| `POST /w/<c>/admin/task/delete` | soft-delete → Trash (reuse `audit._trash`, restorable 30 days) |
| `POST /w/<c>/admin/task/subtask` | add / toggle / assign a sub-task |
| `POST /w/<c>/admin/task/comment` | team comment / resolve change request |

**Stage guards** (optional, from the matrix prototype — port if wanted): block `→ launched` unless
billing/paid or launch conditions met; block `→ closed` while any sub-task/task is still open. Show a
clear message listing what to resolve.

---

## 6. Surface B — Client "Progress" tab (client-facing, read-only + comment)

**Where:** a **new nav tab in the client workspace** `services/portal/dash/templates/atrium.html`,
alongside Dashboard · Campaigns · Insights · Content Calendar · Communications. Key `progress`.

**Route:** extend the existing client tab route `GET /w/<c>/<tab>` to accept `progress`, gated
`authed()` + `can_open(<c>)` (same as the other client tabs). Renders `ws["tasks"]` filtered to
`client_facing`.

**Client write action (the only one):** `POST /w/<c>/task-comment` — client Comment / Request changes,
gated `authed()`+`can_open(<c>)` (mirrors the existing `/w/<c>/comment`). Everything else on this tab
is **read-only** (no create/edit/move/delete, no assignment, no priority, no owners shown).

**Theme:** MUST stay in the client-workspace design system — scope every selector under `.atrium`,
use the `--ax-*` tokens, **Roboto**, and the real logos (`ws.brand.agora_logo`, and the client crest
`brand.client_logo`). The prototype `client_progress_prototype.html` already matches this 1:1
(client labels, blue-green palette, real `assets/logo.svg` + `assets/clients/*.svg`).

**What the client sees per card:** campaign chip · title · content type · progress bar (steps
done/total, **no owner names**) · target date · "Ready for your review" / "Changes requested" tag ·
comment count. Detail: client note ("Update from your team"), the step list (check/empty, not
toggleable), a "View deliverable →" link, and the comment thread + composer.

---

## 7. Field mapping — prototype → production

| Prototype field | Production (`ws["tasks"][]`) | Client sees? |
|---|---|---|
| `assignee_id` | `lead_id` | no |
| `support_ids` | `support_ids` | no |
| `checklist[]` (`{text,done,assignee_id}`) | `subtasks[]` | text + done only (no owner) |
| `team_id` | `department` | no |
| `priority` | `priority` | no |
| `labels`, `campaign`, `content_type` | same | campaign + type only |
| `due_date` | `due_date` | yes (shown as "Target") |
| `deliverable_url`, `client_notes` | `deliverable_url`, `client_note` | yes |
| `internal_notes`, `account_manager_id` | same | **no** |
| `client_facing` | `client_facing` | gates visibility |
| `status` (kanban) | `stage` | yes (relabeled) |
| `comments[]`, `history[]` | same | comments yes; history no |

---

## 8. Guardrails (from CLAUDE.md — must follow)

- **One self-contained template each; no build step, no external JS/CSS/fonts.** Client tab lives in
  `atrium.html`; internal board in `admin_atrium.html` (or its own template).
- **Inline JS must be esprima-4.x-safe:** no optional chaining `?.`, no nullish `??`; classic
  `&&`/`||`. Gate: `python tools/_validate_dash_js.py <template>` must pass.
- **No Jinja inside `<script>`** — JS reads state from `data-*` attributes on the DOM.
- **Never leak internal fields to the client** (lead/support/owners, `internal_notes`, `priority`,
  `client_facing:false` tasks). Filter server-side before render.
- **Client workspace design stays scoped under `.atrium`** with `--ax-*` tokens (decision 2026-07-10).
- **No new infra** — one more workspace JSON key, reusing session auth + bucket + runtime SA.

---

## 9. Phased plan

1. **Schema + workspace.py helpers** — add `ws["tasks"]` + reader/writer helpers; unit-test with
   `dash/_workspace_localtest.py`.
2. **Internal board** — port `task_tracker_prototype.html` into the admin console section + the
   team-only routes (`/w/<c>/admin/task*`). Wire create/edit/move/delete/subtasks/comments.
3. **Client Progress tab** — add the `progress` nav tab + route in `atrium.html`; render client-safe
   tasks read-only; add `/w/<c>/task-comment`. Port `client_progress_prototype.html` chrome.
4. **Comment loop** — connect client comment / request-changes ↔ team resolve (reuse content-comment
   plumbing + notifications via `notify.py`).
5. **Polish** — stage guards, Trash integration (`audit._trash`), activity feed entries (`_audit`),
   optional stage-nudge when all sub-tasks done.

---

## 10. Testing & validation

- `python tools/_validate_dash_js.py services/portal/dash/templates/atrium.html`
- `python tools/_validate_dash_js.py services/portal/dash/templates/admin_atrium.html`
- `python services/portal/dash/_workspace_localtest.py` (task helpers)
- `python services/portal/dash/_atrium_smoketest.py` (route + template render, stubs GCS)
- `python services/portal/dash/_auth_smoketest.py` (gating: client can read own Progress + comment;
  client CANNOT hit `/w/<c>/admin/task*`; internal board is `is_superadmin()`-only)
- Manual: client sees only `client_facing` tasks; no internal fields in the rendered HTML.

## 11. Acceptance criteria

- One data source (`ws["tasks"]`) drives both surfaces; no duplication/export.
- Internal board: Lead + Support, sub-tasks with owners, stage moves, comments, delete→Trash.
- Client Progress tab: read-only, client-safe only, client can Comment / Request changes; the change
  request surfaces on the internal board and is team-resolvable.
- Passes the JS gate + smoke tests; client workspace stays `.atrium`-scoped; no new infra.

## 12. Open decisions (TBD)

- **Assignee roster source** — admin `accounts` vs a dedicated team list (§3.3).
- **Internal board home** — admin-console section (recommended) vs per-client team tab (§5).
- **Stage guards** — port the launch/close guards from the matrix prototype, or keep moves free.
- **Matrix vs Kanban** for the internal board — Kanban (`task_tracker_prototype.html`) is the current
  direction; the matrix (`TASKTRACKER/Agora_Task_Tracker_Atrium.html`) remains an option.

---

## 13. Build prompts (copy-paste, one per phase)

Each prompt below is **self-contained** — paste it into a fresh Claude Code session at the repo root
and it executes exactly one phase of §9. Run them **in order** (each phase builds on the last), one
branch per phase via `tools/push-branch.ps1`, PR → CI → merge before starting the next. Where §12
left a decision open, the prompt bakes in the **recommended default** and says so — override it in
the prompt text if you decide differently.

> Shared preamble (every prompt assumes it): *Read `/CLAUDE.md` and
> `services/portal/dash/CLAUDE.md` first and follow their guardrails exactly — esprima-4.x-safe JS
> (no `?.`/`??`), no Jinja inside `<script>`, self-contained templates, no new infra, never leak
> internal fields to clients. The full product spec is `TASK_TRACKER_INTEGRATION.md`; the approved
> UI prototypes are `atrium/task_tracker_prototype.html` (internal board) and
> `atrium/client_progress_prototype.html` (client Progress tab). Do NOT deploy — stop after tests
> pass and the branch is pushed.*

### Prompt 1 — Schema + workspace.py helpers (§9.1)

```text
Implement Phase 1 of TASK_TRACKER_INTEGRATION.md (read it first, plus /CLAUDE.md and
services/portal/dash/CLAUDE.md): the ws["tasks"] schema and its workspace.py helpers. No routes or
templates in this phase.

In services/portal/dash/workspace.py, following the exact patterns of add_content/update_content/
delete_content (id generation, last-write-wins, local-fs backend support):
- Add a TASK_STAGES guard list: in_process | for_launch | launched | closed (keys are canonical,
  never rename — see spec §3.1).
- Add helpers: add_task, update_task, move_task_stage (appends a history entry {actor, field:"stage",
  old, new, at}), delete_task (returns the removed task dict so the route can trash it), 
  add_task_comment, resolve_task_comment (mirrors resolve-comment on content: resolving the last open
  kind:"changes" comment clears the changes state), add_subtask, set_subtask_done, set_subtask_owner.
- Task shape per spec §3.2: title, stage, department, lead_id, support_ids (must never contain
  lead_id — enforce in add_task/update_task), priority, labels, campaign, content_type, due_date,
  subtasks[] ({id,text,done,assignee_id}), comments[] (reuse the content-comment shape incl.
  kind:"changes" + resolved), history[], client_facing, client_note, deliverable_url,
  internal_notes, account_manager_id.
- Roster decision (spec §12, default): assignees reference admin accounts from store.py by email/id;
  add a small helper that lists the assignable team (accounts with role admin|superadmin).

Extend services/portal/dash/_workspace_localtest.py with tests for every helper: add/edit/move/
delete round-trip, stage guard rejects unknown stages, support_ids can never contain lead_id,
subtask done/owner updates, comment + request-changes + resolve flow, and that delete_task returns
the payload. Run: python services/portal/dash/_workspace_localtest.py — must pass. Also run
python services/portal/dash/_atrium_smoketest.py to prove nothing regressed.

Do not touch templates or main.py yet. Push a branch with tools/push-branch.ps1; do not deploy.
```

### Prompt 2 — Internal Task Board in the admin console (§9.2)

```text
Implement Phase 2 of TASK_TRACKER_INTEGRATION.md (read it, /CLAUDE.md,
services/portal/dash/CLAUDE.md; Phase 1 helpers are already merged): the team-only cross-client
Task Board.

Home (spec §12 default): a new "Delivery / Tasks" section in the operator console
services/portal/dash/templates/admin_atrium.html — add a nav item under a Delivery group in the
console rail. In main.admin_atrium(), while walking clients for the awaiting counts (the workspaces
are already loaded — keep it one pass, no extra reads), collect each ws["tasks"] tagged with its
client key + display name and pass the combined list to the template.

UI: port atrium/task_tracker_prototype.html into that section — stage columns (In Process / For
Launch / Launched / Closed with the spec §3.1 dot colours), drag-to-move, filters (client /
department / priority / person matching lead OR support), card face (labels, priority dot, due chip,
client, lead avatar + support stack, sub-task count, unassigned-sub-task warning), detail modal
(client-safe vs 🔒 internal split, Lead & Support chips, sub-tasks with per-item owner select +
add row + all-done stage nudge, comments incl. change-request styling, activity log), New/Edit form
(Lead select, Support picker, sub-task editor, labels, client-safe vs internal fields), and
delete→confirm. Keep the admin-console design tokens; inline JS esprima-safe; no Jinja in <script> —
serialize the task list into a data-* attribute or a JSON <script type="application/json"> block.

Routes (all gated is_superadmin(), mirroring /w/<c>/admin/* JSON POSTs in main.py):
POST /w/<c>/admin/task (op=add|edit), /w/<c>/admin/task/move, /w/<c>/admin/task/delete
(soft-delete via audit._trash so it lands in the console Bin, restorable), /w/<c>/admin/task/subtask
(op=add|toggle|assign), /w/<c>/admin/task/comment (op=add|resolve). Call _audit(...) on each
mutation. The board updates in place after each POST (no full reload).

Validate: python tools/_validate_dash_js.py services/portal/dash/templates/admin_atrium.html,
python services/portal/dash/_atrium_smoketest.py (extend it to cover the new routes + a render with
tasks present), python services/portal/dash/_auth_smoketest.py (extend: a client session must get
401/403/redirect on every /w/<c>/admin/task* route). Verify in the local preview (run_local.ps1):
create, edit, drag, sub-task toggle/assign, delete→Bin→restore. Push a branch; do not deploy.
```

### Prompt 3 — Client "Progress" tab (§9.3)

```text
Implement Phase 3 of TASK_TRACKER_INTEGRATION.md (read it, /CLAUDE.md,
services/portal/dash/CLAUDE.md; Phases 1–2 are merged): the client-facing read-only Progress tab.

In services/portal/dash/templates/atrium.html add a "Progress" nav tab (key: progress — key is
canonical, label can change) in the client workspace sidebar, grouped where it best fits the
existing nav (alongside Campaigns). Extend the existing GET /w/<c>/<tab> route in main.py to accept
"progress", gated authed()+can_open(<c>) exactly like the other client tabs.

Render ws["tasks"] SERVER-SIDE FILTERED to client_facing == true (never ship a client_facing:false
task, or any internal field, in the HTML — spec §7 mapping). Per card: campaign chip, title, content
type, progress bar (subtasks done/total, NO owner names), target date, stage tag using the CLIENT
labels (In progress / In review / Live / Completed — spec §3.1), "Changes requested" tag when an
open kind:"changes" comment exists, comment count. Detail view: client_note ("Update from your
team"), the step list (read-only checks), "View deliverable →" link, and the comment thread.

The ONLY client write: POST /w/<c>/task-comment (comment + request-changes), gated
authed()+can_open(<c>), mirroring the existing /w/<c>/comment content flow (a kind:"changes"
comment flags the task; resolving stays team-only, already built in Phase 1/2). No create/edit/
move/delete, no owners, no priority, no internal notes anywhere in the client HTML.

Theme: everything scoped under .atrium with the --ax-* tokens (client workspace design — decision
2026-07-10); match atrium/client_progress_prototype.html 1:1 (it already uses the real chrome,
client stage labels, and logos).

Validate: python tools/_validate_dash_js.py services/portal/dash/templates/atrium.html, the
_atrium_smoketest.py render for the progress tab (assert an internal-only marker string does NOT
appear in the client render), _auth_smoketest.py (client can GET progress + POST task-comment for
their own <c> only; other clients' keys are refused). Manual: local preview as a client login —
confirm only client_facing tasks appear and nothing is editable. Push a branch; do not deploy.
```

### Prompt 4 — Comment loop + notifications (§9.4)

```text
Implement Phase 4 of TASK_TRACKER_INTEGRATION.md (read it first; Phases 1–3 are merged): close the
client ↔ team loop on task comments.

- Internal board: a task with an open client change request shows a "Changes requested" flag on its
  card and a team-only Resolve control in its detail thread (reuses resolve_task_comment). Resolving
  the last open request clears the flag on BOTH surfaces.
- Client Progress tab: after the client posts a change request, the card shows "Changes requested"
  immediately (in-place update, no reload) until the team resolves it.
- Notifications via dash/notify.py (graceful/optional, exactly like content comments): client
  comment/change-request → team inbox (ATRIUM_TEAM_EMAIL); team comment/resolve on a client_facing
  task → the client's notify prefs. Every event also writes _audit(...).

Validate: extend _workspace_localtest.py for the full request→resolve round-trip; JS gate on both
templates; _atrium_smoketest.py; _auth_smoketest.py (resolve is is_superadmin()-only). Manual
click-through in the local preview with two windows (admin + client). Push a branch; do not deploy.
```

### Prompt 5 — Polish: guards, Bin, activity, nudge (§9.5)

```text
Implement Phase 5 of TASK_TRACKER_INTEGRATION.md (read it first; Phases 1–4 are merged): the
finishing rules.

- Stage guards (port the rules from TASKTRACKER/Agora_Task_Tracker_Atrium.html): block a move to
  closed while any sub-task is unchecked or any change request is open — return ok:false with a
  human message listing exactly what to resolve; the board shows it as a toast/modal. Keep moves
  otherwise free (spec §12 default: no billing gate in v1).
- Bin integration: task deletes already _trash(); confirm the console Bin lists them with kind
  "task" and Restore re-inserts via a workspace.insert_task helper (add it + tests if missing).
- Activity: every task mutation appears in the console Activity feed with a readable detail line.
- Nudge: when the last open sub-task on a task is checked, the internal detail view offers
  "All sub-tasks done — move to <next stage>?" (one click, still guard-checked).

Validate: full test suite — _workspace_localtest.py, both JS gates, _atrium_smoketest.py,
_auth_smoketest.py — plus a manual pass of §11 acceptance criteria end to end in the local preview.
Update /CLAUDE.md's Atrium section + services/portal/dash/CLAUDE.md with the new tasks contract
(one bullet each, same style as Website Health/Watcher), per the "Keep this file current" rule.
Push a branch; do not deploy.
```

---

## 14. Visual design spec — match the prototypes pixel-for-pixel

The two prototypes are the **canonical look**: the internal board is
`atrium/task_tracker_prototype.html` (admin-console theme); the client Progress tab is
`atrium/client_progress_prototype.html` (client-workspace theme). Everything below is lifted from
their CSS so the built templates read 1:1. **Two different design systems — never mix them:** the
internal board uses the admin-console tokens (system fonts, `--brand-*` / `--accent-*`); the client
tab uses the client-workspace tokens (`--ax-*`, Roboto) and every selector stays scoped under
`.atrium`.

### 14.1 Internal board — design tokens (admin console)

| Token | Value | Use |
|---|---|---|
| `--brand-500 / 600 / 700` | `#4fa84a` / `#3f8b3b` / `#346f31` | primary green: CTA fill, hovers, active-nav text |
| `--brand-300` / `--brand-200` / `--tint` | `#94cd91` / `#bee0bc` / `#eef6ed` | card-hover border / pill border / active-nav + "ok"+"Saved" bg |
| `--emerald` | `#2f7a3a` | logo-mark gradient stop |
| `--accent-600 / 500 / 100` | `#5a54dd` / `#6a6aea` / `#ececfb` | **purple = informational**: labels/pills, support avatars, counts, `.violet` button |
| `--ink` / `--body` / `--muted` | `#121212` / `#353535` / `#6b6f6c` | headings / body / secondary |
| `--canvas` / `--surface` / `--line` | `#f7f7f8` / `#ffffff` / `#e6e6e9` | page bg / cards+rail / hairlines |
| `--danger` / `--danger-bg` | `#c0453b` / `#fbeceb` | delete, change-request bubble, close-x hover |
| Priority dots | urgent `#c0453b` · high `#c77d16` · med `#2f6ecc` · low `#8b8f8c` | card `.pdot` (semantic, NOT the accent) |
| Due semantics | over `#c0453b` · soon `#c77d16` | overdue red / due-in-≤2-days amber |
| Stage dots | in_process `#2f6ecc` · for_launch `#c77d16` · launched `#4fa84a` · closed `#8b8f8c` | column-head + card-border-left dots |
| Shadows | card `0 1px 2px rgba(18,18,18,.04),0 10px 24px -14px rgba(18,18,18,.12)` · pop `0 24px 60px -28px rgba(18,18,18,.28)` · glow-sm `0 10px 30px -12px rgba(79,168,74,.4)` | resting card / hover+modal / green button |
| Radii | sm `.375rem` · md `.625rem` · lg `1rem` | inputs / buttons+cards / modal+list |
| Fonts | sans `'Lato',ui-sans-serif,system-ui,…` · display `'Archivo…','Lato',…` | body / headings+eyebrows (runtime can't web-load → system stack; character = weight + tracking) |

> **The two accents never mix:** green = primary action / done / "synced"; purple = informational
> (labels, counts, support people, the form's Save). Red is reserved for at-risk/overdue/destructive
> — which is exactly why the **In Process** stage dot is blue, not red.

### 14.2 Internal board — component specs

- **Shell**: rail `264px`, `--surface`, `1px --line` right border, sticky full-height; content padding
  `40px 44px 72px`. Nav item `13.5px/600`, radius md, hover `--canvas`, active `--tint` +
  `--brand-700` + weight 700; count badge purple pill, active badge green (`#dcefdb`).
- **Page head** (`.pagehead`, flex space-between): eyebrow `.75rem/600`, `.18em` tracking, uppercase,
  `--brand-600`; `h1.page` `30px/700` `-.02em`; lead `15px` `--muted` `max-width 68ch`. Right side =
  a **✓ Saved** green pill + the **+ New Service** green button (`11px 18px`, radius md, glow-sm
  shadow, `translateY(-1px)` hover).
- **Filters** (flex wrap, `gap 10px`): selects `9px 12px` `13px/600` radius md, focus ring
  `0 0 0 3.5px rgba(79,168,74,.16)`; "Clear filters" borderless muted text button.
- **Board** (horizontal flex, `gap 16px`, `overflow-x auto`); **column** fixed **300px**; column head =
  `9px` stage dot + uppercase `12.5px/700` display title + count pill; list `gap 11px`; drop target →
  `--tint` bg + `inset 0 0 0 2px --brand-300`.
- **Card** (`.tcard`): `--surface`, `1px --line`, radius lg, padding `14px`, `shadow-card`; hover
  `translateY(-3px)` + `shadow-pop` + `--brand-300`; dragging `opacity .55` + `rotate(1.5deg)`.
  Top→bottom: **label pills** (purple, uppercase `10px/700`, `999px`); **title** `14px/700` `--ink`;
  **meta** `12px` = priority dot + word + `· due` (over red / soon amber) + `· client` (muted);
  **foot** (top hairline) = lead avatar `sm` (24px, initials, white on `--brand-500`) + first name +
  overlapping support avatars `xs` (20px, purple, `-7px`, `2px --surface` ring) on the left; icons on
  the right — sub-task check `N/M`, unassigned `⚠ N` (amber), change-request `⚑ N` (red), comment
  bubble `N` (14px inline SVGs, `12px/700` muted).
- **Detail modal** (`max-width 920px`, radius lg, `shadow-pop`, `rise` 0.25s): head `18px 24px` with
  uppercase eyebrow (`Task #id · Stage`) + close-x; body is a **grid `1.15fr 1fr`, gap 26px**. Left:
  labels, `20px` title, description, a `1fr 1fr` fieldset (Client/Campaign/Type/Due), deliverable
  ghost button, client note, then the **🔒 Internal block** (dashed top divider, `.lock` purple):
  AM/Department/Priority fields + a **Lead & support** row of `person-chip`s (lead gets a green
  `LEAD` badge) + internal notes. Right: **Sub-tasks** (count + 7px green-gradient progress bar; rows
  = checkbox `--brand-500` + text + `mini-select` owner + xs avatar; add-row; a green **nudge**
  "✓ All sub-tasks done — move to <stage> →" when all done), **Comments** thread + add-row,
  **Activity** log (`12.5px` muted). Footer: Delete (ghost-danger, left) · spacer · Move-stage ·
  Edit · Close.
- **New / Edit form**: same shell; body `.form-grid` (`1fr 1fr`, `.full` spans both). Label `11px/700`
  uppercase muted; inputs `10px 12px` radius md, green focus ring. **Lead** = select; **Support** +
  **Labels** = pill toggles (checked → purple `--accent-100`/`500`); **Sub-tasks editor** = removable
  rows (text + owner). Primary action = **violet** Save.
- **Toast**: ink pill, bottom-center, `rise` in; `.ok` prefixes a green `✓`, `.err` bg `#9b2c2a`.

### 14.3 Client Progress tab — design tokens (client workspace, DISTINCT)

| Token | Value | Use |
|---|---|---|
| `--ax-green / -d / -bg` | `#4CAC4C` / `#3E8C3B` / `#EAF6E9` | client green: progress bars, "Live", read-only chip |
| `--ax-violet / -d / -bg` | `#2575FC` / `#1856C9` / `#E7F0FE` | client blue: campaign chips, "In review" |
| `--ax-ink / -charcoal / -sub / -line` | `#0C1022` / `#353535` / `#6B7280` / `#E9EAEE` | headings / body / secondary / hairline |
| `--ax-bad` | `#E5413E` | "Changes requested" tag + change-request bubble |
| radius / pill | **`5px`** / `999px` | client cards use the tighter 5px radius (not the console's 10–16px) |
| font | **Roboto** (web-loaded by `main._inject_head`) | the whole client workspace |
| Stage tones (client labels) | In progress `#2575FC` · In review `#F2820C` · Live `#4CAC4C` · Completed `#8A93A3` | summary dots, column heads, `.ax-pg-stage` pills |

### 14.4 Client Progress tab — component specs

- **Read-only chip** under the h1: green lock pill, "Read-only — but you can comment on any item."
  **Summary**: four tiles (`25px/900` count + dot + client stage label).
- **Board**: `repeat(4,1fr)` `gap 14px`, → 2-up ≤980px, 1-up ≤640px. Column head = stage dot +
  uppercase label + count pill.
- **Card** (`.ax-pg-card`, radius `5px`, shadow, hover lift): blue **campaign chip**, `14px/800` title,
  type sub, green **progress bar** (`done of total` + `%`), foot tag = **Changes requested** (red)
  *or* **Ready for your review** (blue) *or* **Target <date>** (amber ≤2 days) + a comment-count chip.
  **No owner names / priority / internal notes anywhere.**
- **Detail** (read-only modal): Type/Target kv, green "Update from your team" note, step list (check
  discs, not toggleable), "View deliverable →" green button, then the **comment thread + composer**
  (the ONE client write): client bubbles green-right, Agora bubbles blue-left, a change request is a
  full-width red-bordered bubble with a "⚑ Change request" flag (green "· Addressed" once resolved).
  Composer: textarea + **Request changes** (ghost) + **Comment** (blue).

### 14.5 Behaviour parity note (the one intended difference)

The prototypes are standalone (localStorage, update-in-place). The integration is server-backed: the
**console board posts + reloads** onto the Tasks pane (same as every console action; a
`?section=tasks` flash keeps you there), while the **client comment** posts via `fetch` and appends
the bubble in place (no reload). There is no Export/Import/localStorage — the server auto-saves to
`ws["tasks"]` (the "✓ Saved" pill reflects that) and deletes go to the console **Bin**, not a
separate Archived list.
