# Service Templates — task-board update instructions

**Status:** ✅ **BUILT & GREEN** in the real platform (verified 2026-07-20 — `_workspace_localtest.py`
+ `_atrium_smoketest.py` pass, `admin_atrium.html` JS gate clean). This file is now the reference
for how it works, not a pending brief. Live in: [`service_templates.py`](services/portal/dash/service_templates.py)
(recipe book + `build_maintasks`), `_task_template_seed()` in
[`main.py`](services/portal/dash/main.py) (op=add only), the New-Service form + pickers in
[`admin_atrium.html`](services/portal/dash/templates/admin_atrium.html).
**Design reference (approved):** [`service_template_prototype.html`](service_template_prototype.html) — a
self-contained click-through of the exact behaviour below. When in doubt, match the prototype.
**Read first:** the root [`CLAUDE.md`](CLAUDE.md) (§ Task tracker) and
[`TASK_TRACKER_INTEGRATION.md`](TASK_TRACKER_INTEGRATION.md) — this file extends them; those rules win.

---

## 1. The goal in one line

When the team adds a service on the Task Board, **the service type builds its own task list** —
main tasks + sub-tasks, each with a "done when" test — instead of being typed from memory. Nothing
important is forgotten and "finished" stops being an opinion.

Today the New-service form has a free-text name and empty `maintasks[]`; the operator types every
main task and sub-task by hand. This update adds a **service-type recipe book** that pre-fills them.

---

## 2. What the operator sees (form flow)

1. **Client** → **Department** → **Service type** (new dropdown, filtered by department).
2. Picking a service type **auto-fills the preview** with that recipe's main tasks + sub-tasks.
3. Per-service **tweaks** (e.g. "How many emails?", "Platform") multiply/adjust the recipe.
4. **Create** → the card lands In Process with the generated `maintasks[]` already on it.

### Acquisition is a special case (the main ask)

Acquisition has **ONE service type: `Google / Meta Campaign`**. On it:

- **Campaign is a TYPED field, not a dropdown** — campaigns have unique names, so the operator types
  it. (Other departments may keep a dropdown; this is driven by a `campType:"text"` flag on the recipe.)
- The **automatic launch tasks** are always added — the generic work every Google/Meta ad campaign
  needs (audience research → structure plan → audience build → ad-set config → load creatives →
  compliance check → tracking confirmation → launch → post-launch QA). See the
  `google_meta_campaign` recipe in the prototype for the exact list.
- Under the service type sits an **"Ad production tasks" picker** — a dropdown + **Add** button that
  drops in a creative task group. Choose as many as needed:
  - **Video Ad Production** (qty = how many videos)
  - **Static Ad Production** (qty = how many statics)
  - **Carousel Ad Production** (qty = how many carousels)
  Each added set becomes its **own main-task group** appended on top of the automatic launch tasks,
  with the per-unit steps multiplied by the quantity (3 videos → "Video 1 — draft edit", "Video 2 —
  …", "Video 3 — …"). Removing a set removes its group. See `AD_PRODUCTION` / `AD_ORDER` in the prototype.

---

## 3. Data model

The recipe is a **build-time template**, not stored state. It expands into the existing
`maintasks[]` shape at create time — **no new workspace JSON keys are required for the tasks
themselves**. Store only enough to remember what was chosen:

- Add `service_type` (recipe key, e.g. `"google_meta_campaign"`) to the task record.
- For Acquisition, persist the chosen ad-production sets so an edit can re-derive, e.g.
  `ad_production: [{type:"video", qty:3}, {type:"static", qty:5}]`.
- Campaign name still stores in the existing `title` field (the form field is labelled "Campaign"
  but writes `title`, per the current convention — do **not** add a separate title field).
- The department-derived label is unchanged (`main.TASK_DEPT_LABEL`: Acquisition→Paid Media,
  Lifecycle→Organic, rest→Website). No manual label picker.

Each generated sub-task carries a **`dod`** ("done when") string. Decide during the build whether
`dod` becomes a first-class field on the sub-task or is folded into its text — **confirm this with
the team before building** (it is the one real schema question here).

---

## 4. Where it goes in the real code

| Concern | Real location |
|---|---|
| Recipe book (`T`, `AD_PRODUCTION`, `AD_ORDER`) | a new server module, e.g. `dash/service_templates.py` (the prototype comments already name it) |
| Build function `build_maintasks(key, params, added)` | `service_templates.py`; mirrors the prototype's `buildMaintasks` — expand fixed groups, multiply `{n}` steps by qty, append chosen ad-production groups |
| Write the generated tasks onto the card | `workspace.py` — reuse `add_task` + the main-task/sub-task helpers (`set_task_maintasks` style; add one if it doesn't exist) |
| New-service form (service-type dropdown, typed campaign, ad-production picker, live preview) | `templates/admin_atrium.html` (Delivery → Task Board New/Edit overlay) |
| Route wiring (`service_type`, `ad_production[]` params) | the existing `POST /w/<c>/admin/task` add path in `main.py` |
| Sandbox to prototype in first | `task_board_sandbox.html` — port there, get sign-off, then to the console |

Departments in the real platform are **slugs** (`acquisition`/`lifecycle`/`data`/`development`/`bidbrain`)
and people ids are **emails** — the prototype's friendly names are placeholders.

---

## 5. Guardrails (do not violate)

- **No new infra / IAM / bucket / secret / service.** This is additive to `platform-dash`.
- **Inline JS must be esprima-4.x-safe:** no `?.`, no `??`; classic `&&`/`||`. No Jinja inside any
  `<script>` — read state from the DOM. (Pre-deploy gate: `tools/_validate_dash_js.py`.)
- **The close guard already does the work:** a move to `closed` is blocked while any sub-task is
  open. Generated sub-tasks and revisions inherit this for free — don't add a second guard.
- **Client-safe leakage:** the client Progress tab shows generated work as owner-less **phases**;
  owners/priority/charge/`dod` internals must never reach the client HTML (same posture as
  `_progress_tasks`). Verify with the no-leak smoketest.
- **Match the approved prototypes** for board/overlay styling, not generic console patterns.

---

## 6. Definition of done — DONE

- [x] Recipe book + `build_maintasks` in `service_templates.py`, unit-tested.
- [x] New-service form: service-type dropdown, per-service tweaks, live preview.
- [x] Acquisition: typed campaign field + automatic launch tasks + ad-production picker (video/
      static/carousel, each with qty), appending main-task groups.
- [x] Create writes generated `maintasks[]` (`_task_template_seed` on **op=add only**; edits never
      regenerate). Each seeded sub-task carries a `dod` ("done when"), team-only.
- [x] Tests: `_workspace_localtest.py` (build/expand + persistence) and `_atrium_smoketest.py`
      (route seed + no-leak render) pass. `admin_atrium.html` JS gate clean.
- [x] Root `CLAUDE.md` + `services/portal/dash/CLAUDE.md` task-tracker bullets updated.
- [ ] (Process-only, not a functional gap) also mirror into `task_board_sandbox.html` if the team
      wants the sandbox to stay in parity — the real console already has it.

**Remaining:** deploy — `services/portal/dash/deploy_dash_platform.ps1` — when the team is ready to
ship it to `platform-dash`.

---

## 7. Try-it (prototype)

Open [`service_template_prototype.html`](service_template_prototype.html) in a browser:
**Acquisition → Google / Meta Campaign → type a campaign name → add "Video Ad Production", set it
to 3.** Watch the preview build the automatic launch tasks plus a "Video ad production" group with
3 numbered video units. Add a card, open it, tick steps, add a revision, then try to close it — the
open sub-task blocks the close exactly as the real board will.
