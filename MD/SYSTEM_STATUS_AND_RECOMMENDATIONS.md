# Agora Atrium — System Status & Recommendations

> Prepared 2026-07-10, after the console-redesign session. Companion docs:
> `ATRIUM_ANALYSIS.md` (full senior-dev review) · `ATRIUM_CONSOLE_REDESIGN_PLAN.md` (the redesign plan).
> This file is the **actionable punch list**: what state the system is in right now, what must be
> finished, and what I recommend next — in priority order.

---

## 1. Where the system stands today (verified)

**Branch `redesign/console-home-hub` (uncommitted, nothing deployed):**

| Surface | State |
|---|---|
| Admin console (`admin_atrium.html`) | ✅ **Redesigned (Concept B)** — Home hub landing, grouped sidebar (Workspaces / People & access), merged Accounts page (Requests · People · Add new), account block at bottom, website theme (green `#4FA84A` + purple `#6a6aea`) |
| Client workspace (`atrium.html`) | ✅ **Original, untouched** — a chrome re-skin was built and then reverted by decision |
| Smoke tests (`_atrium_smoketest.py`, `_auth_smoketest.py`) | ✅ Two landing-page assertions updated to match the new hub |
| `.gitignore` | Modified **before** this session (adds `ATRIUM_ANALYSIS.md`) — pre-existing, left as-is |
| `ATRIUM_CONSOLE_REDESIGN_PLAN.md` | New planning doc (untracked) |

**Verification run (all green, 2026-07-10):**
- `_workspace_localtest` · `_accounts_localtest` · `_google_oauth_localtest` · `_atrium_smoketest` ·
  `_auth_smoketest` · `_audit_localtest` · `_slashid_creative_test` — **7/7 PASS**
- JS gate (`tools/_validate_dash_js.py`) on `admin_atrium.html` **and** `atrium.html` — **OK**
- Repo swept for duplicates/backup strays — **none found** (both templates exist exactly once)
- All routes/IDs/forms the JS + backend depend on confirmed intact (logo upload, notifications,
  impersonation, account CRUD, restore-from-Bin)

---

## 2. Finish the redesign (immediate, this week)

1. [x] **Commit the admin redesign** — done: landed on `main` (`d43a9e7` → merge `fc934bf`).
2. [x] **Push + PR** — done: `main` carries the redesign.
3. [ ] **Deploy**: `services/portal/dash/deploy_dash_platform.ps1` (manual build →
       `gcloud run deploy platform-dash --no-invoker-iam-check`). Never Cloud Build from a laptop.
4. [ ] **Click-through on production** after deploy: hub loads → Atrium Admin → each pane →
       an action redirect (e.g. reset password) lands on the right section — mirror the §8 checklist
       in the redesign plan.
5. [x] **Phase 4 — BUILT 2026-07-12:** "N awaiting approval" / "All caught up" chips on the client
       cards, attention-first sort, total on the hub card. No perf cost — `admin_atrium()` already
       loaded each workspace for the card logo, so the count is a free walk.
6. [x] **Phase 5 — BUILT 2026-07-12:** `brand.py` + `assets/brand.json`/`brand.md` + the front-door
       chrome (`login/signup/request_access/portal/profile` templates, impersonation banner) aligned
       to the website palette (green `#4FA84A` + purple `#6A6AEA`). The client workspace keeps its
       original palette by the 2026-07-10 decision. Ships with the same deploy as #3.

---

## 3. Security hardening (highest-priority system work)

From `ATRIUM_ANALYSIS.md` §6–7 — unchanged by the redesign, still open, still the most important:

1. [ ] **CSRF protection** on the dozens of state-changing POSTs (`/w/<c>/admin/*`,
       `/admin/accounts/*`, approvals, comments). SameSite=Lax is the only current mitigation — not
       enough for a tool with impersonation and account management.
       *Cheap fix:* an `itsdangerous`-signed token in the session + a hidden field/header check.
2. [ ] **Stop storing recoverable plaintext passwords** beside the hashes ("Reset & reveal").
       Move to one-time reveal links or forced-reset invites; if plaintext must stay, encrypt at rest
       with a KMS key.
3. [ ] **Verify Google `id_token` signatures** (JWKS) instead of decode + iss/aud/exp checks only.
       Defense-in-depth; small change in `google_oauth.py`.

## 4. Architecture debt (do before the CRM buildout)

4. [ ] **The JSON-blob datastore is the ceiling.** Last-write-wins whole-document rewrites mean two
       admins (or an admin + the daily intel job) silently clobber each other. **Recommendation:**
       migrate workspace state to **Firestore** (atomic field updates, real queries) and keep GCS for
       binaries — *the single highest-leverage change*. At minimum, add a per-client advisory lock
       before the intel job writes.
5. [ ] **Split `main.py` (~2,700 lines) into Flask blueprints** (auth / atrium-client / atrium-admin /
       console / proxy). Incremental, guarded by the existing smoke tests.
6. [ ] **`atrium.html` is ~4,800 lines / a third of a MB.** Split the inline JS into a few served
       static `.js` files (still no framework, still esprima-gated) to regain diffability. The
       admin console is fine at ~700 lines.

## 5. Quality & operations

7. [ ] **Deepen tests** around the logic most likely to regress silently: `workspace.py` mutations,
       calendar mirroring/done-overdue logic, Range streaming, doc rendering. Today's suite is
       route/smoke-level.
8. [ ] **Observability:** wire Cloud Error Reporting + an uptime check on `/healthz`. The audit feed
       covers user actions but nothing catches server errors today.
9. [ ] **Intel grounding ToS TODO** (flagged in CLAUDE.md): Google requires Search Suggestions to be
       shown to end-users; they currently render only in the admin trace panel.
10. [x] **Docs hygiene — DONE 2026-07-12:** `services/portal/dash/CLAUDE.md` + root `CLAUDE.md` now
        describe the Home-hub console (grouped rail, merged Accounts, attention chips) and the
        website palette; `assets/brand.md`/`brand.json` + READMEs updated to match.

## 6. Product roadmap (recommended order)

- **Double down on approvals** (the killer feature): email/Slack notification on new comments,
  per-piece deadlines, an "everything awaiting me" inbox for clients.
- **One-click branded PDF / scheduled email report** from the dashboard — table stakes for agency
  portals, clients love it.
- **Client-facing search** across content / intel / messages.
- **CRM slice done small** (contacts + notes + simple pipeline) — but **only after** the Firestore
  migration (#4); building it on the JSON blob will hit the no-queries wall immediately.
- **Design-language decision for the client page:** the client workspace keeps its original design
  (decided 2026-07-10). If it's ever revisited, the saved prototypes show the options; any change
  should be chrome-only and feature-preserving.

## 7. Suggested sequence

```
Week 1  : #1–4  ship the admin redesign (commit → PR → deploy → verify)
Week 1–2: §3    CSRF + password hygiene (small, high value)
Week 2+ : #5 Phase 4 chips · §5 #10 docs update · §5 #7 tests
Month   : §4 #4 Firestore migration (before any CRM work) · #5 blueprints
Ongoing : §6 product items, approvals first
```

**Bottom line:** the system is healthy and fully verified today (7/7 tests, JS gates, no strays,
no duplicate files). Ship the admin redesign, then spend the next cycle on CSRF + passwords, and
schedule the Firestore migration before any new surface area is added.
