# CLAUDE.md — services/portal/dash (the portal/CRM Flask app + Agora Atrium)

**Rules live in the repo-root [`/CLAUDE.md`](../../../CLAUDE.md)** — read it first; this file only
adds local context for this subtree. If they disagree, root wins.

You are in the **`platform-dash`** Cloud Run service: the portal/CRM front-door **and** Agora Atrium
(the co-branded client workspace). One self-contained Flask app, no build step.

- **`main.py`** — all routes (portal, Atrium client `/w/<c>/*`, admin `/w/<c>/admin/*` + the dark
  `/admin/atrium/*` console). `WORKSPACE_NAME` is the Atrium product-name constant.
- **`store.py`** — the registry (one private `platform.json`): clients **and** `accounts` (real
  email+password logins; role admin/client, status active/pending). `verify_portal_login` resolves
  super-admin env → account → legacy per-client hash → bootstrap. **`workspace.py`** — per-client
  Atrium state (`workspace/<c>.json`). Both import `google-cloud-storage` lazily and have a local-fs
  backend (`REGISTRY_LOCAL_DIR` / `WORKSPACE_LOCAL_DIR`) so they run off-cloud.
- **Sign-up + approval:** `GET/POST /signup` (Agora-branded `signup.html`) creates a **pending**
  client account; an admin approves it from `/admin/atrium` (`POST /admin/accounts/{approve,reject}`),
  which creates the client + blank workspace and activates the login. No public self-service access.
- **Google Sign-In (central; OPT-IN via `google_oauth.py`):** the portal is the ONE app that runs the
  OAuth flow (`GET /auth/google/login` -> Google -> `GET /auth/google/callback`), resolves the
  *verified* email (`_resolve_login_email` -> `store.resolve_google_login`), then establishes the SAME
  session + shared `ag_sso` cookie a password login mints -- so every dashboard AND the website editor
  trust a Google login identically. Authorization-code flow, confidential client, **no new dependency**
  (token exchange via `requests`; the id_token came over TLS so we decode it + re-check iss/aud/exp/
  email_verified, no JWKS). OFF unless `GOOGLE_OAUTH_CLIENT_ID`/`_SECRET` are set (login page hides the
  button; routes fall back to password). An **unknown** email is routed to `request_access.html` -> `POST
  /auth/request-access` files a **passwordless pending** account that lands in the console's Access
  requests tab. Redirect URI: `${PORTAL_BASE_URL}/auth/google/callback` (or `GOOGLE_OAUTH_REDIRECT_URI`).
- **Operator console (`/admin/atrium`, `admin_atrium.html`)** = a **Home hub + focused console**
  (Concept B, see `ATRIUM_CONSOLE_REDESIGN_PLAN.md`), styled to the website design system (green
  `#4FA84A` primary + purple `#6A6AEA` informational). A fresh visit lands on the branded **Home hub**
  (Agora logo + greeting + the suite as cards: Atrium Admin · Skill Mastery · Website Editor ·
  Sentinel); **Atrium Admin** enters the console, **← All apps** returns. The switch is client-side
  view state only (`data-view` divs; a `?section=`/flash redirect opens the console directly, so every
  deep-link and POST redirect still lands on its pane). Inside: the rail is ordered by frequency of
  use (2026-07-14 IA pass) — **Workspaces** (Clients) / **Delivery** (Task Board · Calendar) /
  **People & access** (**Accounts** — one pane with inner subtabs Requests · People · Add new) /
  **System** (Activity · Mailboxes · **Bin**, restorable soft-deletes; utility items render muted
  via `.nav-item.util`, and the Bin count uses the neutral `.count.quiet` — purple badges are
  reserved for ACTIONABLE counts) — and the operator account block at the bottom (opens
  Profile; themed sign-out confirm). Client cards carry an attention chip — purple **"N awaiting
  approval"** (count computed in `admin_atrium()` from each already-loaded workspace, cards needing
  attention sorted first; total shown on the hub's Atrium Admin card) or green **"All caught up"**.
  It IS the admin landing: `/` redirects a super-admin here and the legacy `/admin` + `/superadmin`
  routes now just redirect here too (their client-add / password-reveal
  functions live in the console). Account routes
  (`/admin/accounts/{create-client,create-admin,grant-google,set-password,reset-password,delete}` +
  `/admin/profile/password`) are gated `is_superadmin()`; **admin-account** creation/management +
  **granting a role** + **impersonation** are gated `is_root_admin()`. `POST /admin/accounts/grant-google`
  is the ONE 'give a Gmail access' action (used from Access-requests AND Create-account): assign to a
  **new client**, an **existing client key**, or a **role** (admin/superadmin) -> `store.upsert_google_account`
  (passwordless, upserts by email so it also activates a pending request in place).
- **Impersonation ("Act as user"):** `POST /admin/impersonate` lets THE super admin assume any active
  account's role + clients (real identity kept in `session["impersonator"]`); every page then carries a
  fixed **"Stop acting as"** banner injected by the `after_request` hook in `main.py` (so it reaches
  even the huge `atrium.html` without editing it). `GET|POST /admin/stop-impersonating` restores the
  real identity. Only `is_root_admin()` can START it (once acting-as you ARE that user, so the controls
  vanish). This is what "signing in as `info@` lets you act as any user" means.
- **Roles:** `client` < `admin` (clients `["*"]`) < `superadmin`. THE super admin is `SUPER_ADMIN_EMAIL`
  (default `info@agoradatadriven.com`, env-overridable) or any account with role `superadmin`; only they
  create/manage admin accounts, grant roles, impersonate, and can't be deleted. The no-password preview
  (`DEV_NOAUTH`) auto-signs in as `SUPER_ADMIN_EMAIL`.
- **`templates/*.html`** — big self-contained pages. Inline JS must be **esprima-4.x-safe** (no `?.`
  / `??`; classic `&&`/`||`). No Jinja inside `<script>` — JS reads state from the DOM.
- **`atrium_docs.py` / `feedback_ai.py`** — the opt-in Google-Doc → AI strategy feature (gated, degrades).
- **`atrium_health.py`** — the team-only Website Health tab: fetches the client's live site + detects
  installed marketing tags (GTM/GA4/pixels) by scanning the page HTML (no GTM API, infra-free, degrades).
- **`watcher.py`** — the team-only Watcher tab: paste a YouTube channel link, archive EVERY video's
  raw transcript. No YouTube API key: channel-page scrape → public `youtubei/v1/browse` playlist
  paging (classic renderer AND 2025+ lockupViewModel shapes; captures upload age →
  `published_estimate` ISO date) → `youtube-transcript-api` (pinned in requirements, lazy import).
  Channels are classified: `platform` / `industry` (auto-labeled via `intel_ai.classify_text`,
  hand-editable) / `kind` creator|competitor. Registry in `ws["watcher"]`; each channel's
  transcripts in its own `workspace/watcher/<c>/<id>.json` object. `POST /w/<c>/admin/watcher`
  (op add|fetch|safe_pull|refresh|meta|label|delete; fetch = MISSING-only batches of 8, page JS
  loops it; a rate-limit reports `blocked` and never marks videos failed) +
  `GET /w/<c>/watcher/video/<id>/<vid>` (full transcript behind the click-to-expand cards). UI:
  3-across creator grid, collapsed to the 4 newest videos, filter bar
  (search/platform/industry/type) + date sort. YouTube blocks datacenter IPs — for Cloud Run
  fetching create Secret `watcher-proxy-url` (mounted as `WATCHER_PROXY_URL` when present).
  **Safe pull** = the no-proxy path: `op=safe_pull` queues the channel in
  `ws["watcher"]["safe_pull"]` (helpers `workspace.queue/clear_watcher_safe_pull`); the operator
  machine's scheduled task (`install_safe_pull_task.ps1` → `safe_pull_agent.vbs`, 5-min tick,
  hidden) runs `safe_scrape_local.py --queue` — slow residential-IP scrape (12–20s pacing,
  5→60 min ladder, %TEMP% PID lock, syncs to the bucket every 5 transcripts, clears the queue
  entry per finished channel; no args = full sweep). Test: `python _watcher_localtest.py`.
- **`assistant_ai.py`** — the team-only Assistant tab: RAG chat over EVERY workspace source
  (watcher transcripts, intel, campaigns/content, metrics, calendar, conversations, health, plus
  the opt-in client dashboard export — grant via `enable_assistant_dash_data.ps1`). Pure-Python
  BM25 index stored as `workspace/assistant/<c>/index.json` (lazy rebuild on `fingerprint` change);
  answers via `intel_ai._call` (JSON-mode; `_parse_answer` parses leniently AND salvages
  nearly-JSON — `strict=False`, `raw_decode` past trailing junk, then a hand-scan of the
  `"answer"` string literal that survives stray characters, truncation at the token cap, and raw
  newlines — so the UI is never handed a raw JSON envelope) with cited sources. Bot bubbles render
  the model's markdown via `mdToHtml` in `atrium.html` (headings/bold/lists; HTML-escaped first,
  esprima-safe).
  `POST /w/<c>/admin/assistant` (op ask|settings|reindex). Dev: `VERTEX_ACCESS_TOKEN` env runs
  Vertex off-cloud. Test: `python _assistant_localtest.py`. UI: the team-only floating bubble
  (`ax-asfab` FAB + `ax-aspanel` pop-up in `atrium.html`, inside `.atrium` so the vars/font
  inherit; brand-green 72px since 2026-07-13) is the PRIMARY surface, available on every tab; the
  Assistant tab pane still exists but is no longer in the nav (reach `/w/<c>/assistant` by URL for
  the date-range + reindex controls) — both surfaces wired by ONE `wireAssistantChat`; the bubble
  hides on the Assistant tab via `.atrium[data-tab="assistant"]`. Each surface's conversation is
  persistent **chat history**: localStorage key `agora.aschat:/w/<c>:<log-id>` (last 40 turns,
  per-browser), replayed on load (greeting shows only when nothing is stored — it comes from the
  log's `data-greeting`), cleared by the "New chat" button on either surface; the saved turns also
  feed the model's multi-turn context (`history` field of op=ask, last 8). **Model choice:** `op=settings`
  saves `ws["assistant"]["model"]` ("" = automatic → intel model → deploy default; resolved by
  `main._assistant_model`); the dropdown renders via the shared `as_model_options()` macro (tab
  bar + the bubble's gear strip). **Detail (depth) control:** `op=settings` also saves
  `ws["assistant"]["depth"]` (quick|standard|deep, `assistant_ai.DEPTHS`, resolved by
  `main._assistant_depth`; `as_depth_options()` macro, same two surfaces — each dropdown posts
  only its own field so saving one never resets the other). Deep = the model plans extra BM25
  queries first (`plan_queries`), retrieval widens to 30 excerpts, provider thinking turns ON
  (`intel_ai._call(..., think=True)`: Gemini thinkingBudget 4096, DeepSeek
  `thinking:{type:enabled}`; quick/standard send the explicit fast path since DeepSeek V4 thinks
  by default server-side), and the prompt asks for a structured analysis. All depths may
  synthesize across excerpts (implicit disagreements count). **Spend tally:** `intel_ai` provider calls fill an optional
  `usage_out` dict (DeepSeek `usage`, Vertex `usageMetadata` incl. thinking tokens);
  `intel_ai.PRICING`/`cost_of` price it, `workspace.add_assistant_usage` accumulates
  `ws["assistant"]["usage"]`, and the cost pill (`ax-ascost`, seeded from data-* attrs, updated
  from each ask's `usage`/`totals`) shows session + all-time + by-model. **Client rename:**
  `POST /admin/atrium/<c>/rename` (superadmin) updates the registry name
  (`store.set_client_name`) AND the workspace `display_name` — display-only, the key/resources
  never change; the console cards have a Rename button (prompt-driven).
- **`mailroom.py` / `mail_refresh.py`** -- the team-only Mail tab: pull + archive + AI-summarize
  each client's email correspondence. Mailboxes connect ONCE in the console (Mailboxes pane;
  `POST /admin/mail` op add|delete|test, root-only): kind **dwd** = our Workspace domain via the
  Gmail API + domain-wide delegation, keyless signJwt as the `mail-sync` SA
  (`enable_atrium_mail.ps1` one-time; deploys auto-set `MAIL_DWD_SA` when the SA exists), or kind
  **imap** = any other Google account via an app password (stdlib imaplib, Gmail `X-GM-RAW`, the
  All-Mail folder -- so both connectors run the SAME Gmail query and normalize to the same thread
  shape). Per client: `ws["mail"]["contacts"]` drives the query (only client mail is pulled);
  thread archives are their own objects `workspace/mail/<c>/<key>.json`; the small index + digest +
  sync stamps live in `ws["mail"]`; the global registry is `workspace/mail/_mailboxes.json`
  (`public_mailboxes` strips passwords for templates). Machine mail is dropped per message
  (`is_automated`: noreply/bounce senders, Auto-Submitted, Precedence bulk/junk/auto_reply;
  List-Unsubscribe alone NEVER counts -- Google-Groups-safe; the query also excludes the
  Promotions/Social categories). `thread_stats`/`stats_line` compute `awaiting_reply` + average
  AGORA reply hours per thread (connected-mailbox addresses + dwd domains count as agency) -->
  the tab's stats strip, per-row chips, the digest's REPLIES judgement, and the Assistant's
  `mail:responsiveness` snapshot chunk. `summarize_thread` returns TWO voices in one call: the
  internal summary (blunt, reply-quality included) + a `client_summary` that is mirrored onto the
  client-visible Communications Email Summary feed (`workspace.upsert_email_summary`, stable
  `mail_<key>` id, updated in place; thread delete retracts it). `build_digest(..., stats=...)`
  writes STATUS/NEEDS ACTION/RECENT/REPLIES; both reuse
  `intel_ai._call` (spend -> the Assistant tally); the Assistant's `build_chunks` indexes the
  archive (`mail_threads`). Routes: `POST /w/<c>/admin/mail` (op contacts|sync|digest|delete) +
  `GET /w/<c>/mail/thread/<key>`, gated `is_superadmin()`; nav = the Insights group. The hourly
  job `mail-refresh` reuses THIS image (gated `MAIL_SYNC_ENABLED=1`; deploy
  `deploy_mail_refresh.ps1`, rerun after any mailroom/mail_refresh change). Test:
  `python _mail_localtest.py`.
- **`intel_feed.py` / `intel_refresh.py`** — the DAILY Market Intelligence auto-refresh (opt-in,
  `INTEL_AUTO_ENABLED=1`). `intel_feed` parses Google News RSS + publisher feeds (keyless, stdlib
  `xml.etree` + lazy `requests`, degrades to `[]`); `intel_refresh.main()` is the Cloud Run **job**
  entry point — it reuses THIS image + the web SA to write `ws["intel"]` (auto entries only; hand-
  added/edited ones are preserved). Deploy: `deploy_intel_refresh.ps1`. Test: `_intel_feed_localtest.py`.
- **Task tracker (Delivery board + client Progress tab):** `ws["tasks"]` per client, helpers in
  `workspace.py` (stages `in_process|for_launch|launched|closed` — keys canonical; lead +
  `support_ids` never overlap; `move_task_stage` blocks `closed` while sub-tasks/change-requests
  are open). Work is TWO-LEVEL: `maintasks[]` (named groups, each with an `assignee_id` + its own
  `subs[]` of owner-carrying sub-tasks); legacy flat `subtasks[]` migrates in place via
  `normalize_task` (called by `_find_task`) and `task_subtasks()` flattens for counts/guards.
  Tasks also carry `start_date` + `due_date` (LAUNCH date — UI says "Launch date"), an
  internal-only `service_charge`, a boolean **`on_hold`** + internal `hold_reason`
  (`workspace.set_task_hold`, ongoing = not held; `POST /w/<c>/admin/task/hold`; a held
  client-facing task shows the client a plain "Paused", reason never crosses), and ONE label
  auto-derived from the department (`main.TASK_DEPT_LABEL` — no label picker; the form's name field
  is LABELED "Campaign" but stores as `title`). Support people are Edit-only (`has_support` guard,
  so op=add never clears). Team board = the console's Delivery → Task Board pane in
  `admin_atrium.html` (tasks collected in `admin_atrium()` from the workspaces it already loads;
  columns sort Urgent-first, active-before-held, then launch date; filter+sort persist in
  localStorage `agora.tkprefs`; overlays server-rendered into `#tk-store`, forms post
  `redirect=console` + `back_task`/`back_tab` so the overlay REOPENS on the same tab after the
  reload). The detail overlay is TABBED: persistent glance chips (priority/hold/start/launch/charge/progress) above
  Details | Tasks | Comments panels (`data-tktab`); the New/Edit form's optional fields live in a
  collapsible `<details class="tk-extra">`. Team routes
  `POST /w/<c>/admin/task{,/move,/delete,/maintask,/subtask,/comment}` (`is_superadmin()`;
  `/maintask` op=add|assign|rename|delete — rename = the overlay's edit-in-place title input
  (`.tk-main-rename`); `/subtask` op=add takes `maintask_id`; delete →
  Bin `kind:"task"` → `workspace.insert_task` on restore). Overlay forms carry
  `back_task`/`back_tab` and `_task_reply` forwards them as `?task=<c>:<id>&tab=` so the console
  script REOPENS the same detail overlay on the same tab after the redirect (params scrubbed via
  replaceState; the Delete form deliberately carries none). The filter bar has a client-side
  **sort selector** (`#tk-f-sort` Priority / Launch date / Client, reordering cards via
  `data-priority/data-due/data-cname`; the default matches the server order). Client side: the `progress` tab renders
  `main._progress_tasks(ws)` (client_facing + client-safe fields ONLY — owners/priority/charge/
  internal notes never reach the client HTML; the breakdown arrives as owner-less **phases**;
  the modal shows a Started → Going live timeline; cards say "Launching <date>" / "Live"; columns
  sort by soonest launch; client stage labels In progress / In review / Live / Completed); the one
  client write is `POST /w/<c>/task-comment` (comment / request-changes; resolve is team-only).
  Notifications: `notify.client_task_commented/client_task_changes/
  team_task_commented/team_task_resolved`. **Delivery Calendar** = a 2nd Delivery nav pane
  (`data-section/pane="calendar"`) in `admin_atrium.html`: a month grid built CLIENT-SIDE from a
  hidden `#cal-store` of `.cal-ev` nodes (one per service WITH a `due_date`, server-rendered from
  `task_cols`), plotting each service on its **launch date**, discipline-tinted, ⏸ for on-hold;
  prev/next/today + a client filter; clicking an event reuses the Task Board's `tkOpen` to open the
  SAME detail overlay (undated services simply don't appear). New services always start In Process
  (no stage picker on the New form; add route hardcodes `in_process`). `_task_fields_from_form`
  patches dates/charge/support ONLY when the form carried them (a partial POST can't wipe them).
  Spec: `/TASK_TRACKER_INTEGRATION.md`; tests live in
  `_workspace_localtest.py` (helpers) + `_atrium_smoketest.py` (routes, gating, no-leak render, calendar).
- **`audit.py`** — super-admin activity feed + restorable Trash; ONE private `audit.json` in the
  registry bucket (no new infra). `main.py` calls `_audit()`/`_trash()` from the mutation/delete
  routes; the console **Activity**/**Trash** tabs read it; deletes are restorable for 30 days (lazy
  auto-purge). Off-cloud test: `python _audit_localtest.py`.
- **`brand.py`** — bundled palette + AGORA mark (the container can't read repo-root `assets/`).
- **Google Tag Manager (site-wide, opt-in):** the `_inject_gtm` `after_request` hook in `main.py`
  injects the GTM container (`<head>` loader + `<body>` `<noscript>`) into **every** portal HTML page
  when env `GTM_CONTAINER_ID` is set — unset = no tag (so local preview stays untracked). GA4 is
  configured INSIDE the container in the GTM UI. The container ID ships from `deploy_dash_platform.ps1`
  (`$GTM_CONTAINER_ID`); reverse-proxied client dashboards (`/d/<c>/`) are skipped.

**Deploy:** `deploy_dash_platform.ps1` (build → `gcloud run deploy platform-dash --no-invoker-iam-check`).
It mounts the Google sign-in secrets (`google-oauth-client-id` / `google-oauth-client-secret`) ONLY if
they exist, so a default deploy stays unaffected (button off) until you create them + grant the web SA
`secretmanager.secretAccessor` on each. Register the redirect URI
`https://portal.agoradatadriven.com/auth/google/callback` on the OAuth client.
**Test (off-cloud, what CI runs):** `python _workspace_localtest.py`, `python _accounts_localtest.py`,
`python _google_oauth_localtest.py`, `python _atrium_smoketest.py`, `python _auth_smoketest.py`,
`python _audit_localtest.py`, `python _watcher_localtest.py`, `python _slashid_creative_test.py`, and
`python _mail_localtest.py` from this dir.
**Preview:** `run_local.ps1` (or `preview/Preview Portal (admin).cmd` at repo root).
