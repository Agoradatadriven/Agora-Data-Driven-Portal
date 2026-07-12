# Atrium Team Console — Redesign Implementation Plan (Concept B: Home Hub)

> **STATUS (2026-07-12): Phases 1–5 BUILT.** Phases 1–3 (hub + rail + merged Accounts) are merged to
> `main` (`d43a9e7` → `fc934bf`). Phases 4 (awaiting-approval chips) and 5 (brand alignment to the
> website palette across `brand.py`, `assets/brand.json`, and the login/portal/signup/request-access/
> profile chrome + impersonation banner) were built 2026-07-12 in the working tree.
> A client-page (`atrium.html`) chrome re-skin was prototyped and built, then **reverted by
> decision** — the client page (and its family: dashboard_view, recap, docview) stays on its original
> design. Remaining: commit → PR → CI → deploy. See `SYSTEM_STATUS_AND_RECOMMENDATIONS.md`.

> Turning the flat 8-item admin console into a **Home hub + focused console**, styled to the
> Agora **website** design system. Prepared 2026-07-10. Target file:
> `services/portal/dash/templates/admin_atrium.html` (+ one small optional change in `main.py`).
>
> **Prototype reference (approved):** Concept B — Home hub. Apps is the *front door*, not a sidebar
> button; the console opens on a branded suite screen, then you click **Atrium Admin** to work.

---

## 1. Goal

Make the **suite the highlight** and the **console calm**:

1. On open, land on a branded **Home hub** — real Agora logo + a greeting + the three products as
   large cards (Atrium Admin, Skill Mastery, Website Editor).
2. Clicking **Atrium Admin** enters the console; **← All apps** (top of the rail) returns to the hub.
3. Inside the console: grouped nav (**Workspaces** / **People & access**), the three account panes
   merged into one **Accounts** page with inner tabs, and the operator account moved to the bottom.
4. Restyle everything to the **website theme** (`website/src/styles/global.css`): white/airy, bold
   black type, green = primary/CTA, purple = informational accent.

**Non-goals:** no change to any route's behaviour, permission gate, form action, or data model. This
is presentation + client-side view state only (with one optional, additive backend count in Phase 4).

---

## 2. Decision needed before build (one)

**Brand accent — the app and the website currently disagree:**

| | Green | Accent |
|---|---|---|
| Website (`global.css`) | `#4FA84A` | **purple** `#6a6aea` |
| Atrium app (`brand.py`) | `#4CAC4C` | **blue** `#2575FC` |

The prototypes use the **website** palette (purple). Recommendation: **standardize the console on the
website palette** so the customer-facing suite feels like one brand. It's low-risk — implemented as
CSS custom properties, so it's a one-block change and fully reversible. (Optional follow-up: align
`brand.py` too, so the portal/login chrome matches — Phase 5.)

- [x] **DECIDED (2026-07-10): website purple** — green `#4FA84A` + purple `#6a6aea`, ported from
  `website/src/styles/global.css`. Phase 5 (aligning `brand.py`/`brand.json` so portal + login chrome
  match) is now recommended, not just optional.

Everything below is written to be accent-agnostic (driven by `--accent-*` tokens), so this choice is a
single edit either way.

---

## 3. Design principles (from the website system)

- **Tokens only** — port the `@theme` values from `global.css` into a `:root` block: brand green
  scale, `--accent-*`, `--ink #121212`, `--body #353535`, `--muted`, `--canvas #f7f7f8`,
  `--surface #fff`, `--line`, `--tint #eef6ed`, card/pop shadows, radii.
- **Type** — display/eyebrow use the display stack, body uses the sans stack. NOTE: the runtime can't
  web-load Archivo/Lato (locked container — see `brand.py`), so we use the **same system-font stack**
  `brand.py` already uses. Character comes from weight, tracking, and the uppercase eyebrows.
- **Green = primary action, purple = informational** (status chips, badges) — never mix the two.
- **Motion**: a calm, held/slow green aurora on the hub only; the console stays still. Respect
  `prefers-reduced-motion`.
- **Real assets**: the Agora logo is already in context via `brand.py` (`agora_logo`); client card
  logos come from `assets/clients/<c>.svg` (already wired as `c.logo`).

---

## 4. Architecture — how the hub + console coexist

Keep the **single self-contained template** and **no build step**. Introduce a top-level **view
switch** in `admin_atrium.html`, entirely client-side (DOM-driven, esprima-safe):

```
<body data-initial-view="…">
  <div class="view" data-view="hub">      …home hub…            </div>
  <div class="view" data-view="console">  …existing rail + panes… </div>
```

- JS shows one `.view` at a time. **Atrium Admin** card → `showView('console')`; **← All apps** →
  `showView('hub')`.
- **Initial view logic** (so action-redirects don't dump the user on the hub):
  - If the server passed a `section` (i.e. after any `/admin/accounts/*` or `/admin/atrium/*` POST
    redirect via `_atrium_redirect_list(..., section=…)`) **or** a `msg` — open **console** on that
    section.
  - Otherwise (a fresh visit to `/admin/atrium`) — open the **hub**.
  - This needs **no route change**: `main.py` already passes `initial_section` and `msg`; we read
    them from `data-*` attributes on `<body>` (no Jinja inside `<script>`).

This preserves every existing deep-link and redirect while making the hub the default landing.

---

## 5. Work breakdown

### Phase 0 — Branch + baseline (safety)
- [ ] New branch via `tools/push-branch.ps1` (never work on `main` directly — see `docs/dev-workflow.md`).
- [ ] Run the local preview to capture the "before": `services/portal/dash/run_local.ps1`.
- [ ] Confirm the accent decision (§2).

### Phase 1 — Design tokens + CSS foundation
- [ ] Replace the `:root` palette in `admin_atrium.html` `<style>` with the website tokens
      (accent-agnostic via `--accent-*`).
- [ ] Add component styles used by the new layout: `.view`, hub (`.hub`, `.aurora/.blob`, `.suite`,
      `.app*`), rail additions (`.side-top`, `.home-link`, `.nav-group`, `.acct-btn`), `.subtabs/.sub`,
      `.chip`. (All already authored in the approved prototype — port verbatim.)
- [ ] Keep the existing responsive rules; extend for the hub + collapsed rail.

### Phase 2 — Home hub view
- [ ] Wrap the current console markup in `<div class="view" data-view="console">`.
- [ ] Add `<div class="view" data-view="hub">` above it: header (real `agora_logo` + account chip),
      eyebrow greeting, and the **suite** of three cards:
  - **Atrium Admin** → `data-open-console` (enters the console). Shows `{{ clients|length }} workspaces`
    and (Phase 4) the awaiting-approval flag.
  - **Skill Mastery** → `{{ skill_mastery_url }}` (existing context var).
  - **Website Editor** → `{{ website_editor_url }}` (existing context var).
- [ ] Add the view-switch JS + initial-view logic (reads `data-initial-view` / `data-initial-section`
      / `data-has-msg` from `<body>`; no Jinja in the script).

### Phase 3 — Rail restructure + merge account panes
- [ ] **Remove** the old flat nav (Apps, Profile, and the three account items) and the big launcher.
- [ ] **Rail top**: `← All apps` (`data-go-hub`) + the real Agora logo.
- [ ] **Grouped nav**: *Workspaces* → Clients · Activity · Bin (keep the `trash|length` count);
      *People & access* → **Accounts** (carry the `pending|length` count here).
- [ ] **Bottom account block**: avatar + `{{ profile.name }}` + role, opening the **Profile** pane;
      keep **Sign out** (with its existing `data-signout` confirm).
- [ ] **Merge panes**: fold the current `requests`, `accounts`, and `create` panes into **one**
      `data-pane="accounts"` section with an inner tab strip **Requests · People · Add new**
      (`.subtab`/`.sub`). Move the existing markup verbatim — the `assign_select()` macro, every form
      `action`, every hidden input, and every confirm hook (`data-reject-request`, `data-actas`,
      `data-reset`, `data-delete-account`) stay **unchanged**.
- [ ] **Section→subtab mapping** in JS so redirects still land correctly:
      `requests → Accounts/Requests`, `create → Accounts/Add new`, `accounts → Accounts/People`;
      `profile → Profile pane`, `trash → Bin`, `clients → Clients`.
- [ ] Keep `is_root_admin` gating exactly as-is (admin-account management + role grants + impersonation).

### Phase 4 — attention chips on client cards — ✅ BUILT 2026-07-12
- [x] In `main.py` `admin_atrium()`: for each client with a workspace, compute the count of content
      pieces with `status == "awaiting"` (mirror the `selectattr('status','equalto','awaiting')` logic
      already in `atrium.html`) and pass it on each `client` dict. The route ALREADY loads each
      workspace for the card logo, so the count is a free walk — the perf note below is moot.
- [x] Render a purple **"N awaiting approval"** chip (purple = informational) or a green
      **"All caught up"** chip on each card; sort clients so those needing attention come first
      (stable sort — ties keep registry order). The hub's Atrium Admin card shows the total as an
      `.app-flag` next to the workspace count.
- [x] ⚠️ **Perf note (no DB):** resolved — no extra reads; the workspace JSON was already loaded per
      client for the logo.

### Phase 5 — brand alignment + polish — ✅ BUILT 2026-07-12
- [x] Standardized on the website accent: `brand.py` palette + `assets/brand.json` (+ `assets/brand.md`)
      now carry the website tokens (green `#4FA84A`/`#3F8B3B`/`#EEF6ED`, purple `#6A6AEA`/`#5A54DD`/
      `#ECECFB`, ink `#121212`), keeping `AGORA_LOGO_*` artwork untouched. Because the login/portal
      chrome hardcodes its palette per template, the swap was applied to `login.html`, `signup.html`,
      `request_access.html`, `portal.html`, `profile.html` and the impersonation banner + injected
      chrome in `main.py` (hex AND rgba() forms). The client workspace family (`atrium.html`,
      `dashboard_view.html`, `recap.html`, `atrium_docview.py`) keeps its original palette by decision.
- [x] Empty states ("No clients yet", "No pending requests"), keyboard focus states (`:focus-visible`)
      — already present in the built console.

---

## 6. Files touched

| File | Change | Risk |
|------|--------|------|
| `services/portal/dash/templates/admin_atrium.html` | The whole redesign (tokens, hub view, rail, merged Accounts, JS) | Medium — big diff, but presentational; guarded by smoke tests |
| `services/portal/dash/main.py` | **Only if Phase 4**: per-client awaiting count in `admin_atrium()` | Low — additive read |
| `services/portal/dash/brand.py` + `assets/brand.json` | **Only if Phase 5**: accent alignment | Low |

No new routes, no new files, no new infra, no new dependencies.

---

## 7. Guardrails (must follow — from root `CLAUDE.md`)

- **esprima-4.x-safe JS**: no optional chaining `?.`, no nullish `??`; classic `&&`/`||`. The
  pre-deploy gate `tools/_validate_dash_js.py` parses it.
- **No Jinja inside `<script>`** — JS reads all state from `data-*` attributes on the DOM.
- One self-contained HTML file, **no build step, no external JS/CSS/fonts**.
- Private-by-default posture unchanged; no route/permission edits.

---

## 8. Testing & validation

- [ ] `python tools/_validate_dash_js.py` (JS gate) — must pass.
- [ ] `python services/portal/dash/_atrium_smoketest.py` (route + template render, stubs GCS).
- [ ] `python services/portal/dash/_auth_smoketest.py` (auth/gating intact).
- [ ] **Manual click-through** in local preview (`run_local.ps1`):
  - Fresh load → **hub** shows, real logo + 3 cards.
  - Click **Atrium Admin** → console (Clients). Click **← All apps** → hub.
  - Nav groups switch panes; **Accounts** inner tabs (Requests/People/Add) work.
  - Do an action that redirects (e.g. create client, reset password) → lands back in the **console**
    on the right section/subtab, not the hub.
  - Client cards open `/w/<c>/`; logo upload + delete confirms still fire.
  - Responsive: rail collapses to a top strip on narrow widths; hub reflows.
  - Impersonation banner still injects (unaffected).

## 9. Deploy

- [ ] PR → CI green (`.github/workflows/ci.yml` runs the JS gate + off-cloud Atrium tests).
- [ ] Merge to `main`, then `services/portal/dash/deploy_dash_platform.ps1`
      (build as yourself → `gcloud run deploy platform-dash --no-invoker-iam-check`). Manual, never
      Cloud Build from a laptop; never `--allow-unauthenticated`.

## 10. Rollback

Single-file change → revert the `admin_atrium.html` commit (and the small `main.py` / `brand.py` diffs
if Phases 4–5 shipped) and redeploy. No data migration, so rollback is instant and safe.

---

## 11. Acceptance criteria

- Opening `/admin/atrium` shows the **Home hub** with the real Agora logo and three product cards.
- **Atrium Admin** enters the console; **← All apps** returns; deep-links/redirects open the console
  on the correct section.
- The three account areas live under **one Accounts page** with working inner tabs.
- Every existing action, gate, and confirm behaves exactly as before.
- Styling matches the website system in the chosen accent; passes the JS gate + smoke tests.

---

## 12. Sequencing (suggested)

1. **Phase 1–3 together** = the visible redesign (hub + rail + merged Accounts). One PR.
2. **Phase 4** (attention chips) = fast-follow PR.
3. **Phase 5** (brand alignment) = optional, separate PR.

**Next action:** confirm the accent (§2), then I start Phase 1–3 on a branch and show it to you in the
local preview before any PR.
