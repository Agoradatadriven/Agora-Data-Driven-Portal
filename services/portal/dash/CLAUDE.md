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
- **Operator console (`/admin/atrium`, `admin_atrium.html`)** = a left **side panel** with panes:
  **Apps** (launcher: Atrium Admin · Skill Mastery · Website Editor deep-links) · Clients (cards + add)
  · Access requests · Accounts · Create account · **Activity** (audit feed) · **Trash** (restorable
  soft-deletes) · Profile. It IS the admin landing: `/` redirects a super-admin here and the legacy
  `/admin` + `/superadmin` routes now just redirect here too (their client-add / password-reveal
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
  (op add|fetch|refresh|meta|label|delete; fetch = MISSING-only batches of 8, page JS loops it; a
  rate-limit reports `blocked` and never marks videos failed) + `GET /w/<c>/watcher/video/<id>/<vid>`
  (full transcript behind the click-to-expand cards). UI: 3-across creator grid, collapsed to the 4
  newest videos, filter bar (search/platform/industry/type) + date sort. YouTube blocks datacenter
  IPs — for Cloud Run fetching create Secret `watcher-proxy-url` (mounted as `WATCHER_PROXY_URL`
  when present). Test: `python _watcher_localtest.py`.
- **`assistant_ai.py`** — the team-only Assistant tab: RAG chat over EVERY workspace source
  (watcher transcripts, intel, campaigns/content, metrics, calendar, conversations, health, plus
  the opt-in client dashboard export — grant via `enable_assistant_dash_data.ps1`). Pure-Python
  BM25 index stored as `workspace/assistant/<c>/index.json` (lazy rebuild on `fingerprint` change);
  answers via `intel_ai._call` (JSON-mode, parsed leniently) with cited sources.
  `POST /w/<c>/admin/assistant` (op ask|reindex). Dev: `VERTEX_ACCESS_TOKEN` env runs Vertex
  off-cloud. Test: `python _assistant_localtest.py`.
- **`intel_feed.py` / `intel_refresh.py`** — the DAILY Market Intelligence auto-refresh (opt-in,
  `INTEL_AUTO_ENABLED=1`). `intel_feed` parses Google News RSS + publisher feeds (keyless, stdlib
  `xml.etree` + lazy `requests`, degrades to `[]`); `intel_refresh.main()` is the Cloud Run **job**
  entry point — it reuses THIS image + the web SA to write `ws["intel"]` (auto entries only; hand-
  added/edited ones are preserved). Deploy: `deploy_intel_refresh.ps1`. Test: `_intel_feed_localtest.py`.
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
`python _audit_localtest.py`, `python _watcher_localtest.py`, and `python _slashid_creative_test.py`
from this dir.
**Preview:** `run_local.ps1` (or `preview/Preview Portal (admin).cmd` at repo root).
