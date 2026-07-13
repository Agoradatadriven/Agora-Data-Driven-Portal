# CLAUDE.md — Agora Data Driven (canonical agent fast-path)

This is the file Claude Code auto-loads. It is the single source of truth for fixed facts, the
data contract, the deploy procedure, and the guardrails. The pointer at `.claude/CLAUDE.md` defers
to this file; if they ever disagree, **this file wins — update both so they agree again.**

Per-area `CLAUDE.md` files (in `services/portal/dash/`, `clients/client_template/`, `services/ingest/`,
`tools/`) give Claude local context for a subtree and **defer to this root file** for the rules — so
every developer's Claude follows the same contract without re-reading the whole repo.

## Overview

Agora Data Driven is a marketing agency that self-hosts password-gated client marketing dashboards
on Google Cloud Platform, fronted by a client portal that is growing into a full CRM.

- **One repeatable pattern, many clients.** Every client is fully derived from a short key `<c>`
  (see the derivation rule below). One GCP project, one region, one shared Artifact Registry repo.
- **`template` is the worked example.** `clients/client_template/` is the canonical pattern every
  new client copies — three SQL views, an export job, and a dashboard web service.
- **The portal/CRM front-door** (`services/portal/`, served at `portal.agoradatadriven.com`) is a
  reverse proxy + single login over all dashboards, with a registry stored as one private JSON in
  GCS. It is designed to grow into a CRM (see the `# CRM:` markers in `services/portal/dash/main.py`).
  **Agora Atrium** — the co-branded client workspace — is built into this same `platform-dash`
  service (see the Agora Atrium section below).
- **Windsor.ai is the only data source.** Connector loaders in `services/ingest/` land
  source data into the shared `raw_windsor` BigQuery dataset; per-client SQL views read from there.

## Fixed facts (use literally — never invent alternatives)

| Fact | Value |
|------|-------|
| GCP project | `agora-data-driven` |
| Region | `asia-southeast1` (Singapore) — **everything lives here, one region, never another** |
| Artifact Registry repo | `agora` |
| Shared raw dataset | `raw_windsor` (the only raw layer; written by Windsor connectors) |
| Portal host | `portal.agoradatadriven.com` |
| Client dashboards | `<c>.agoradatadriven.com` |
| SSO cookie scope | `.agoradatadriven.com` (leading dot) |
| Local dev | Windows + PowerShell; repo venv python at `.\.venv\Scripts\python.exe` |

`PROJECT_NUMBER` is **never hardcoded** — resolve it at runtime:
`gcloud projects describe agora-data-driven --format='value(projectNumber)'`.

**Per-client derivation rule** (derive, never re-type) for a key `<c>`: dataset `client_<c>`,
bucket `agora-data-driven-<c>-dash`, export job `<c>-export`, web service `<c>-dash`, job SA
`<c>-dash-job@agora-data-driven.iam.gserviceaccount.com`, web SA `<c>-dash-web@…`, password secret
`<c>-dash-password`, session secret `<c>-dash-session-key`, subdomain `<c>.agoradatadriven.com`,
data object `<c>.json` + freshness sidecar `_freshness.json` in the client's bucket.

## Repo layout

```
ROOT/
├── services/                — every deployable Cloud Run service / job
│   ├── portal/              — portal/CRM front-door + Agora Atrium (Cloud Run service `platform-dash`)
│   │   ├── dash/            — the Flask app (main.py, workspace.py, store.py, templates/, …)
│   │   └── deploy.ps1       — one-shot portal standup (formerly deploy_platform.ps1)
│   ├── ingest/              — Windsor connector loaders (ga4, google_ads, meta, tradedesk, reddit,
│   │                          hubspot, fields) that write raw_windsor.* — scheduled API pulls
│   └── status-dashboard/    — meta freshness monitor over every client (no dataset/views)
├── clients/                 — one folder per client; client_template/ is the worked pattern
│   └── client_template/       sql/ · job/ · dash/ · deploy scripts · README
├── assets/                  — brand kit: logo set, brand.json/brand.md, clients/<c>.svg
├── tools/                   — operator tooling: setup.ps1, start_day.ps1, deploy_ingest_jobs.ps1,
│                              enable_platform_sso.ps1, enable_super_admin.ps1, _validate_dash_js.py,
│                              push-branch.ps1, merge-branches.ps1
├── preview/                 — double-click local-preview launchers (admin / client-login)
├── docs/                    — deeper docs; docs/dev-workflow.md = the branch → PR → CI → merge flow
└── CLAUDE.md · README.md · ONBOARDING.md
```

`tools/_validate_dash_js.py` is the shared pre-deploy JS gate; `assets/` is the brand kit the seed
inlines into each workspace (the deployed container only bundles `dash/`, so logos are embedded).

## Dashboard edits

Each dashboard is **one big self-contained `dash/dashboard.html`** (no build step, no external JS).
Grep for the metric or label you want to change and edit in place. Theme colors are CSS custom
properties in `:root` (the `--ag-*` palette). Inline JS must stay **esprima-4.x-safe**: no optional
chaining `?.` and no nullish coalescing `??` (the pre-deploy gate `tools/_validate_dash_js.py`
parses it with esprima, which predates those tokens). Use classic `&&`/`||` guards.

## Agora Atrium (client workspace in the portal)

Atrium is the co-branded client workspace built **into** `platform-dash` — **additive**, reusing the
existing session auth, bucket, and runtime SA. **No new infra/IAM/bucket/secret/service** — except for
a few deliberately **opt-in** features that stay dormant and infra-free unless an operator enables them:
the Google-Doc → AI strategy feature, large-creative signed uploads, and the daily Market-Intelligence
auto-refresh (see those bullets below). Product name is one constant:
`WORKSPACE_NAME` in `services/portal/dash/main.py`.

- **State = one private JSON per client (no database):** `workspace/<c>.json` in the **registry
  bucket** `agora-data-driven-platform-dash`. `dash/workspace.py` is the only reader/writer
  (last-write-wins, mirrors `store.py`); it imports `google-cloud-storage` lazily and supports a
  local-fs backend via `WORKSPACE_LOCAL_DIR` (+ `WORKSPACE_BUCKET`/`WORKSPACE_PREFIX`) so it is
  testable off-cloud. Shape: `metrics`, `today`, `split`, `series`, `activity`, `campaigns[]`
  (`strategy`/`ai_summary`/`strategy_doc` + `content[]` with status `awaiting|approved|changes`,
  `client_note`, an optional publish `date`, threaded `comments[]` (each `id`/`sender`/`body`/`kind`;
  a `kind:"changes"` comment is a "Request changes" comment that flips status and carries `resolved`),
  and optional uploaded-creative `image_object`/`image_mime`),
  `calendar[]`, `conversations[]` (`client`/`agora` messages), `intel`
  (`business_research[]`/`media_buying[]`, each entry `heading`/`title`/`body`/`source`/`link`/`date`)
  for the Market Intelligence tab, per-user `notify` prefs,
  and `website_health` (`url`/`notes`/`last_check`) for the team-only Website Health tab.
- **Website Health is a TEAM-ONLY tab (admins see it, THE super admin edits):** an extra nav tab +
  pane rendered ONLY for `is_superadmin()` (never shown to clients — the nav, the pane, AND the
  `/w/<c>/website-health` route all gate on it; a client hitting the URL is bounced to Dashboard).
  Editing (set URL, run check, notes) is gated `is_root_admin()` via `_atrium_root_json_gate` and the
  `can_edit_health` template flag, so a non-root admin gets a READ-ONLY view ("the admin can just see
  it"). `dash/atrium_health.py` (pure, infra-free) fetches the client's live site server-side and
  reports reachability/errors + the marketing tags installed on the page (GTM containers, GA4, UA,
  Google Ads, Meta/TikTok/LinkedIn/Hotjar/Clarity… — detected by scanning the returned HTML, NOT the
  GTM API, so no new infra/credentials; deeper in-container introspection would need the GTM API and
  stays out of scope). It degrades gracefully (a dead site is recorded in the result, never a 500).
  Routes: `POST /w/<c>/admin/website-health/{save,check}` (root-only). State lives under
  `ws["website_health"]` via `workspace.set_website_url`/`set_website_notes`/`save_website_check`.
- **Watcher is a TEAM-ONLY tab (creator/competitor transcript archive):** paste a channel link and
  Watcher lists EVERY video, then pulls each video's raw transcript (AI summaries are a later step).
  Rendered/gated exactly like Website Health (`ATRIUM_TEAM_TABS`, never shown to clients), but
  editing is any-admin (`is_superadmin()`), not root-only. `dash/watcher.py` does the fetching with
  NO YouTube API key: channel page scrape → the public web `youtubei/v1/browse` endpoint pages the
  uploads playlist (handles BOTH the classic `playlistVideoRenderer` and the 2025+ `lockupViewModel`
  shapes, and captures each video's relative upload age → `published_text` +
  `watcher.published_estimate` ISO date) → `youtube-transcript-api` (pinned in dash requirements,
  imported LAZILY so tests/CI run without it) per video. **Classification:** each channel carries
  `platform` (youtube-only today; the field exists so other source types can join), `industry`
  (auto-labeled on add from the video titles via `intel_ai.classify_text` — the intel brain's
  default model — and hand-editable), and `kind` creator|competitor. State: the small channel
  registry lives in `ws["watcher"]["channels"]` (counts + classification only); each channel's full
  archive is its OWN object `workspace/watcher/<c>/<channel_id>.json` (transcripts run to MBs —
  same posture as creatives). Routes: `POST /w/<c>/admin/watcher` (`op`
  add|fetch|refresh|meta|label|delete — fetch pulls MISSING transcripts in short batches of 8 and
  the page JS loops it with a progress bar; **a YouTube rate-limit stops the batch and reports
  `blocked` WITHOUT marking any video failed**, so the next fetch resumes exactly where it stopped;
  refresh also backfills upload dates; meta hand-edits industry/kind; label re-runs the AI label)
  and `GET /w/<c>/watcher/video/<channel_id>/<video_id>` (the click-to-expand full transcript; the
  page itself only inlines previews). **UI = a filterable creator grid:** three creator cards per
  row (collapsed = classification chips + the 4 most recent videos; expand = the full uniform video
  grid + per-channel title search), with a top bar filtering by creator search / platform /
  industry / creator-vs-competition and sorting by newest upload / recently added / name. Every
  failure degrades to a friendly message (`ok:false`); permanent no-transcript videos are recorded
  and skipped. ⚠️ YouTube blocks datacenter IPs, so Cloud Run fetches usually need the OPT-IN
  egress proxy: create Secret `watcher-proxy-url` (full proxy URL, e.g. Webshare rotating
  residential) and redeploy — `deploy_dash_platform.ps1` mounts it as `WATCHER_PROXY_URL` only when
  it exists. Off-cloud test: `dash/_watcher_localtest.py` (in CI; stubs GCS + the YouTube fetchers).
- **Assistant is a TEAM-ONLY tab (RAG chat over the WHOLE workspace):** grounded Q&A across every
  source the portal holds for a client — campaigns + content (incl. comments), workspace metrics,
  Market Intelligence, the calendar, client conversations, website health, every Watcher
  transcript, and (opt-in) the client's dashboard `<c>.json` KPI export. `dash/assistant_ai.py`:
  `build_chunks` flattens the sources, `build_index` stores a pure-Python BM25 index as ONE private
  object `workspace/assistant/<c>/index.json` (rebuilt lazily via `fingerprint` whenever data
  moves; no vector DB, no new deps), `ask` retrieves top chunks (optionally date-ranged — dated
  sources only) and answers with the intel brain's provider plumbing (`intel_ai._call`, the
  client's configured model or the default; prompts for `{"answer": ...}` JSON, parsed leniently).
  Answers cite sources; the UI shows them as chips. Routes: `POST /w/<c>/admin/assistant` (`op`
  ask|reindex, gated `is_superadmin()`); tab gated like the other team tabs. The dashboard-data
  source needs a one-time grant: `services/portal/dash/enable_assistant_dash_data.ps1` gives the
  portal SA objectViewer on each client dash bucket (run 2026-07-12; re-run for new clients) —
  without it that source is silently skipped. `VERTEX_ACCESS_TOKEN` env (dev-only) lets the same
  Vertex code paths run off-cloud with a `gcloud auth print-access-token` token. Off-cloud test:
  `dash/_assistant_localtest.py` (in CI). The Watcher tab also gained a Looker-style upload-date
  range control (presets + custom from/to) that filters videos and creators client-side.
  The same chat is ALSO a **floating bubble** (team-only FAB bottom-right, Mastery-Engine style)
  reachable from every tab: one `wireAssistantChat` wiring in `atrium.html` serves both surfaces
  (the tab keeps the date-range + reindex controls), an open conversation survives client-side tab
  switches, and the bubble hides on the Assistant tab itself (CSS on the root's `data-tab`, which
  `showTab` keeps current).
- **Content with a date mirrors onto the Content Calendar (linked event):** when an admin gives a
  content piece a `date` (in the add/edit-content form), `workspace.add_content`/`update_content`
  mirror it into `calendar[]` as a linked event carrying `content_id` + `tab` (paid→`leadgen`,
  organic→`organic`); the piece is the source of truth (editing date/title/channel OVERWRITES the
  event, clearing the date or deleting the piece removes it), while the calendar keeps its own
  mark-as-done `status`. The calendar day-popup shows linked events with a "Lead Generation /
  Organic Content" source tag and a **→** arrow that jumps to the piece on its tab. Done/colour
  logic (`atrium_view._event_done`/`_event_overdue`): a content-linked event is green only once
  **explicitly marked done**, **red (overdue)** if past its date and unmarked, green-ahead if a
  future date is already done; **plain** (non-content) calendar events keep the original
  green-forward rule (past ⇒ done). The JS in `atrium.html` (day-popup + month-history grid) mirrors
  this exact logic.
- **Uploaded creatives = separate private objects (NOT inline in the JSON):** an admin-uploaded
  creative (image OR video) is stored as its own object `workspace/creatives/<c>/<content_id>` in the
  **same registry bucket** (keeps the rewrite-in-full workspace JSON small) and is served ONLY through
  the authed proxy `GET /w/<c>/creative/<content_id>` (mirrors the `/data.json` posture — never made
  public). The serve route honors HTTP **Range** (a `Range` request → `206` windowed stream, 8 MiB
  cap, for video seeking; no range → `200` **chunked** full stream with NO `Content-Length`, since
  Cloud Run caps fixed-length responses at ~32 MiB but streams chunked ones unbounded). `workspace.py`
  streams via `blob.open("rb")` (one seekable download), never loading the whole object into memory.
- **Attached documents preview in place (no download required):** a per-piece attachment (the
  `images[]` row, served at `GET /w/<c>/creative/<content_id>/<image_id>`) that is a document renders
  a clean file-type icon (PDF/DOC/XLS/CSV/PPT/TXT band, color per format — `doc_icon` macro) that
  opens a scrollable doc lightbox with a transparent download button. The
  serve route is **inline by default** (so a PDF previews in an `<iframe>`); `?dl=1` forces an
  attachment download with the original filename. PDFs preview natively; Word/Excel/PowerPoint/CSV/
  text are rendered to scrollable HTML by `dash/atrium_docview.py` (**stdlib only** — `zipfile` +
  `ElementTree`, no CDN/no new deps) served at `GET /w/<c>/docview/<content_id>/<image_id>`; an
  unsupported/corrupt file degrades to a friendly "download to view" page. Classification is by
  filename extension AND mime, so an empty-mime upload no longer renders as a broken `<img>`.
- **Large creatives bypass the ~32 MiB request cap via a SIGNED URL (opt-in infra):** small files
  still POST through the app (`/w/<c>/admin/upload-creative`); files >30 MiB upload **directly to GCS**.
  The browser asks `POST /w/<c>/admin/creative-upload-url` for a V4 signed PUT URL
  (`workspace.signed_upload_url`, **keyless** — signs via the IAM signBlob API using a cloud-platform-
  scoped runtime-SA token; storage-scoped tokens fail with `ACCESS_TOKEN_SCOPE_INSUFFICIENT`), `PUT`s
  the file straight to the bucket, then `POST /w/<c>/admin/creative-confirm` records it. ⚠️ Needs
  one-time infra (run `services/portal/dash/enable_atrium_uploads.ps1`, idempotent): the
  `iamcredentials` API on, the runtime SA granted `roles/iam.serviceAccountTokenCreator` **on itself**,
  and CORS on the registry bucket. If signing is unavailable the route returns `ok:false` and the UI
  falls back to the in-app POST path (so a default deploy still serves ≤30 MiB uploads with no infra).
- **In-workspace admin editing = the team edits the REAL `/w/<c>/` in place.** When `is_superadmin()`
  opens a workspace, the SAME client UI renders extra edit affordances (`{% if is_superadmin %}` +
  `data-admin="1"`), posting JSON to `/w/<c>/admin/*`: `strategy`, `strategy-doc`, `generate-summary`,
  `summary`, `campaign`, `delete-campaign`, `content`, `edit-content`, `delete-content`,
  `content-comment`, `delete-comment` (delete any thread comment, on paid AND organic), `add-images`,
  `remove-image`, `upload-creative`, `creative-upload-url`,
  `creative-confirm`, `remove-creative`, `metrics`, `calendar`, `intel` (add/edit/delete a Market
  Intelligence briefing entry — `op`+`section`), `reply`. This in-place surface is the
  ONLY editing path — the old per-client `/admin/atrium/<c>` console page (and its
  password/campaign/content/conversation/reply/metrics POSTs) has been removed. **Clients** approve in place (`/approve`) and
  post threaded `/w/<c>/comment`s; "Request changes" now lives IN the comment thread as a
  `kind:"changes"` comment (light-red, flagged) that flips status to `changes`. Raising a change
  request is a CLIENT power; **resolving it is TEAM-ONLY** — the **Resolve** button (`/resolve-comment`,
  gated `is_superadmin()`) renders only for the team, and resolving the last open one returns the piece
  to `awaiting`. All of it updates in place (no reload), so the organic dropdown stays open.
- **Clients can set their OWN logo from inside the workspace:** the side-panel crest is a hover-to-upload
  control — hovering reveals a "Change logo" overlay; clicking opens a file picker that POSTs to
  `/w/<c>/logo` (client-facing, gated `authed()`+`can_open(<c>)`, image-only ≤512 KB). It is the
  client-facing twin of the team console's `/admin/atrium/<c>/logo`: the image is embedded INLINE as a
  `brand.client_logo` `<img>` data-URI (same posture as seeded logos — no new infra/object), and the
  crest swaps in place on success.
- **Market Intelligence is a CLIENT-VISIBLE, TEAM-CURATED tab (the weekly briefing):** a `/w/<c>/intel`
  nav tab + pane every client sees, holding two fixed sections — **Business Research** (competitor +
  industry news) and **Media Buying News** (Google/Meta/Instagram updates). State is one key
  `ws["intel"]` = `{business_research[], media_buying[]}`, each a list of entries (newest first)
  `{id, heading, title, body, source, link, date}`. `workspace.add_intel_entry`/`update_intel_entry`/
  `delete_intel_entry` are the only writers (`workspace.INTEL_SECTIONS` is the valid-section guard).
  The team writes/edits/deletes entries IN PLACE via `POST /w/<c>/admin/intel` (`op` add|edit|delete +
  `section`, gated `is_superadmin()`); clients read only. `atrium_view.intel_sections(ws)` decorates
  the two lists with their display label/lede/icon for the template. No new infra (one more workspace
  JSON key, mirrors Client Communications).
  - **Daily AI auto-refresh — GROUNDED web research (an AI 'brain', LIVE):** a Cloud Run job
    `intel-refresh` (`dash/intel_refresh.py`, Cloud Scheduler `intel-refresh-daily` 07:00 SGT) runs
    **grounded research** (`intel_ai.research`): the selected **Vertex Gemini** model, with the live
    **Google Search grounding** tool (`tools:[{googleSearch:{}}]`), PLANS the angles that matter to
    THIS client → SEARCHES the whole web → CURATES the strongest items, each with a REAL source URL
    (from `groundingMetadata.groundingChunks[].web.uri`) and a **`relevance`** ("why this matters for
    <client>") line. Same engine as Gemini chat — broad + on-topic, NOT a Google-News re-rank.
    `research` returns `(entries, error)`; **NO fallback** — a failure shows the reason and adds
    nothing. **Grounding is Gemini-only** (`intel_ai.model_supports_grounding`): a non-Gemini model
    (DeepSeek) reports "can't do live web research — pick a Gemini model" and adds nothing. (The old
    retrieve-then-curate `intel_ai.curate` + `intel_feed` RSS scrape is LEGACY — kept as a helper +
    for tests, no longer wired into the refresh.) Vertex Gemini (`gemini-2.5-flash`/`-pro`) is
    GCP-billed via the runtime SA's metadata token, gated `VERTEX_GEMINI_ENABLED=1` +
    `VERTEX_PROJECT`/`VERTEX_LOCATION` (grounding works at `global`); ⚠️ grounded search bills extra
    per prompt, and we do NOT set `responseMimeType` (JSON mode is unreliable with the search tool —
    we prompt for JSON and parse leniently). Per-client config `ws["intel_ai"]` = `{model,
    business_prompt, media_prompt, window, count, show_thinking}` (admin-set in the **AI Research
    Brain** panel; `intel_ai.window_of`/`count_of`/`window_label` validate the recency `7d…12m` +
    target 1–25; keywords `ws["intel_topics"]` are SEEDS the model expands, not literal queries).
    **Business Research is keyed ENTIRELY off `ws["intel_topics"]` with NO fallback** — no keywords ⇒
    empty section + "set keywords" reason, never filler. **Media Buying News** is universal (runs for
    every client). The two sections research **concurrently** (writes stay serial). Intel entries gain
    a `relevance` field (`workspace._INTEL_FIELDS`), rendered as "Why this matters for <client>" under
    each summary. **`show_thinking`** (admin toggle, default off) captures the model's reasoning + the
    **search plan** (`groundingMetadata.webSearchQueries`) + grounded **sources** + Google **Search
    Suggestions** (`searchEntryPoint.renderedContent`) + raw output into `ws["intel_ai"]["last_trace"]`
    (per section), shown in the panel — a debugging aid; it enables Gemini `includeThoughts` (slower).
    ⚠️ Google's grounding ToS asks that Search Suggestions be shown to end-users; currently rendered
    only in the admin trace panel (client-facing display is a TODO). Each run is **ADDITIVE**:
    `workspace.add_auto_intel` de-dupes new stories and APPENDS them (list grows, never wiped;
    plain-auto capped 60/section, manual + favourited always kept). Team edits via `POST
    /w/<c>/admin/intel` ops: `ai_settings` (model/prompts/window/count/show_thinking), `topics`,
    `suggest` (the panel's "Write these for me" — `intel_ai.suggest_config` AI-drafts the keywords +
    both focus prompts from what the workspace knows about the client — campaigns/website/watcher
    industries via `main._intel_client_context` — grounded on a live Google lookup when the model is
    Gemini; returns the drafts WITHOUT saving, the panel fills the fields for review + Save), `refresh-now`,
    `bulk` (mass delete / favourite — favourite stars + pins), plus add/edit/delete. **Gated:** the
    job no-ops unless `INTEL_AUTO_ENABLED=1`; it REUSES the platform-dash image + web SA. New infra:
    the scheduler job (impersonates the **web SA**, not the cloudscheduler service agent — owners
    can't actAs that agent) + `roles/aiplatform.user` on the web SA + the optional `DEEPSEEK_API_KEY`
    secret. Redeploy `services/portal/dash/deploy_intel_refresh.ps1` (`-Disable` OFF, `-Run` fires
    now; **rerun after any `intel_feed`/`intel_refresh`/`intel_ai` change** — image-pinned) AND
    `deploy_dash_platform.ps1` (the web service's Refresh-now runs `refresh_client` in-process).
    Off-cloud tests: `dash/_intel_feed_localtest.py` + `dash/_intel_ai_localtest.py` (inject fetchers).
- **Routes (all behind existing session auth):** client `GET /w/<c>/` + `/w/<c>/<tab>` (overview,
  dashboard, leadgen, organic, calendar, conversations, intel, settings) gated `authed()`+`can_open(<c>)`;
  client POSTs `/w/<c>/{approve,request-changes,save-note,comment,send-message,save-notify,logo}` +
  creative GET above; team-only POSTs `/w/<c>/resolve-comment` + `/w/<c>/admin/*` gated `is_superadmin()`. The team console
  (`GET /admin/atrium`, gated `is_superadmin()`) is a **Home hub + focused console** (Concept B —
  `ATRIUM_CONSOLE_REDESIGN_PLAN.md`): a fresh visit lands on the branded hub (suite cards: Atrium
  Admin · Skill Mastery · Website Editor · Sentinel); **Atrium Admin** opens the console — grouped
  rail *Workspaces* (Clients · Activity · Bin) / *People & access* (Accounts with subtabs Requests ·
  People · Add new) — all client-side view state, so `?section=`/flash redirects still land on the
  right pane. The Clients pane shows one card per client (the worked-example `template` client is
  filtered out) with an attention chip (purple **"N awaiting approval"**, attention-first sort, or
  green **"All caught up"**). **Clicking a card opens that
  client's workspace `/w/<c>/` directly** (where all editing happens in place). Each card also carries
  an **Upload logo** control (POST `/admin/atrium/<c>/logo` — embeds the image inline as a
  `brand.client_logo` `<img>` data-URI, ≤512 KB; same posture as seeded logos) and a confirmed
  **Delete** control (POST `/admin/atrium/<c>/delete` — `store.remove_client` +
  `workspace.delete_workspace`). **Add a new client** (`POST /admin/atrium/new`) asks ONLY for a
  display name (key auto-derives, password auto-generates) and on success redirects STRAIGHT to the
  new client's blank `/w/<c>/`. The
  portal landing (`/`) shows **Open dashboard** per client; the workspace `/w/<c>/` stays reachable
  directly and from the console.
- **Auth foundation (central Google sign-in + impersonation):** the portal is the ONE app that runs
  Google OAuth (`google_oauth.py`, `/auth/google/{login,callback}`) and, on a verified email, mints
  the SAME session + shared `ag_sso` cookie as a password login — so the website editor and every
  dashboard trust a Google login identically. OPT-IN: off unless `GOOGLE_OAUTH_CLIENT_ID`/`_SECRET`
  are set. An unknown email files a **passwordless pending request** (`/auth/request-access`) an admin
  approves in the console's Access-requests tab via `POST /admin/accounts/grant-google` (assign to a
  new/existing client OR a role). `/admin/atrium` IS the admin landing (`/` redirects here; the legacy
  `/admin` + `/superadmin` pages now just redirect here too). THE super admin (`info@…` / role
  `superadmin`) can **act as any user** (`/admin/impersonate`; a site-wide "Stop acting as" banner is
  injected by the `after_request` hook). Full details + OAuth/secret setup: `services/portal/dash/CLAUDE.md`.
- **Strategy doc → AI strategy (optional, opt-in):** an admin attaches a Google Doc to a campaign and
  clicks "Generate strategy". `dash/atrium_docs.py` reads it (public-export fetch by default, or the
  **Google Drive API** when `ATRIUM_DOCS_ENABLED=1`) and `feedback_ai.summarize_strategy_sections`
  (Claude `claude-opus-4-8`, the existing `FEEDBACK_AI_ENABLED`+`ANTHROPIC_API_KEY` gate) writes the
  three **What / Why / What-next** strategy sections; they stay hand-editable. Every step degrades
  gracefully (no AI → doc excerpt in "What happened"; unreadable doc → ok:false with share guidance;
  no doc → empty, the admin types it). ⚠️ The Drive-API path is a **deliberate, opt-in deviation** from
  "no new infra": it needs the Docs/Drive API on + `google-api-python-client` in `requirements.txt` +
  the doc shared with the runtime SA. **A default deploy stays infra-free.**
- **Notifications are optional & graceful** (`dash/notify.py`, mirrors `feedback_ai.py`): default
  records an activity entry + logs to stdout; real email only when **both** `ATRIUM_EMAIL_ENABLED=1`
  and `ATRIUM_EMAIL_API_KEY` (Secret-Manager) are set, SDK imported lazily. **No provider key
  committed.** Team inbox `ATRIUM_TEAM_EMAIL` (default `info@agoradatadriven.com`).
- **Super-admin audit feed + restorable Trash (`dash/audit.py`):** ONE new private JSON
  `audit.json` in the SAME registry bucket (no new bucket/service/IAM — mirrors `store.py`: GCS
  default, local-fs via `REGISTRY_LOCAL_DIR`). Two lists: **`activity[]`** — every admin/client
  action across all workspaces (`{ts,client,actor,role,action,detail}`, capped 500, newest first),
  written by a one-line `_audit(client, action, detail)` call from each mutation route in `main.py`
  and surfaced in the super-admin console's **Activity** tab (each `_audit` also fires
  `notify.activity_alert`, an OPTIONAL email reusing the dormant transport). **`trash[]`** — major
  deletions (content, campaign, personal calendar event, whole client) are soft-deleted: the delete
  route stashes the removed payload via `_trash(...)` before deleting, and the **Trash** tab lists
  them with a **Restore** button (`POST /admin/atrium/restore` → `workspace.insert_content`/
  `insert_campaign`/`insert_calendar_event` or `store.restore_client` + `save_workspace`). Entries
  older than **30 days** are purged automatically whenever the trash is read/written (lazy purge —
  the no-infra equivalent of a scheduled job, since the app is request-driven). Both lists are
  best-effort (swallow storage errors) so logging/trashing can never break the action.
- **Theme/JS:** the official brand **light** theme, standardized 2026-07 on the WEBSITE design system —
  Data Green `#4FA84A` + Accent Purple `#6A6AEA` (deep companion `#5A54DD` for white-text fills), on a
  white canvas with bold black type; green = primary action, purple = informational. The whole
  front-door (login, portal, team console) shares it (`dash/brand.py` + `assets/brand.json` are the
  palette source); the Atrium **client workspace** keeps its original design by decision (2026-07-10),
  scoping every selector under `.atrium` so it stays self-contained. The logo is `ws.brand.agora_logo`
  (seeded) in Atrium and `dash/brand.py`
  elsewhere. Inline JS is esprima-4.x-safe and reads state from the DOM (no Jinja in any script block).
- **Ships via the SAME deploy as the portal:** `services/portal/dash/deploy_dash_platform.ps1` (build
  as yourself → `gcloud run deploy platform-dash --no-invoker-iam-check`). Validate templates with
  `tools/_validate_dash_js.py` first. Seed the demo once:
  `.\.venv\Scripts\python.exe services\portal\dash\seed_workspace.py` (idempotent; writes
  `workspace/riverdance.json`, refuses to clobber). Local tests: `dash/_workspace_localtest.py`
  (data) and `dash/_atrium_smoketest.py` (full route+template, stubs GCS).
- **Local preview (no-password, for devs):** double-click `preview/Preview Portal (admin).cmd` — or run
  `services/portal/dash/run_local.ps1`. It serves the whole front-door at `http://localhost:8080` from
  an isolated `.venv-portal` + throwaway `.local_portal_data` (never the real bucket/ADC), seeds demo
  clients (`dash/seed_local.py`), and auto-signs-in as super-admin so there is NO login and every
  workspace is editable in place. `preview/Preview Portal (client login).cmd` shows the real login on
  `:8081`. The no-auth is `PORTAL_DEV_NOAUTH=1`, honored by a `before_request` hook in `main.py`
  **only when `PORTAL_SECURE_COOKIES=0`** — so it can never activate in the https deploy.

## The data contract (three stages, matched BY NAME)

```
sql/*.sql  (view column)  ->  job/main.py  (assembled `data` dict key)  ->  dash/dashboard.html  (data.* key)
```

Adding a metric is usually three edits, one per stage. **Renaming a key in one stage breaks the
next** — the names must match exactly. For `template` the chain is: `kpi_overview` /
`daily_performance` columns → `data["kpis"].*` / `data["daily"][].*` → `data.kpis.*` / `data.daily`.

## Redeploy after an edit — MANUAL, never cloudbuild from a laptop

Deploys are manual: build the image as yourself, then deploy. A laptop must **never** trigger Cloud
Build to deploy, because the Cloud Build SA cannot `iam.serviceAccounts.actAs` the runtime SA
(`gcloud builds submit --tag` to build an image is fine; it is the *deploy-as-the-runtime-SA* step
that fails). Use the per-stage scripts (all resolve paths from `$PSScriptRoot`, all idempotent):

- **View/SQL change** → `clients/client_template/sql/deploy_views_template.ps1`
  (reapplies views via `create_views.py`, then re-runs the export job with `FORCE_REBUILD=1`).
- **Job / data-assembly change** → `clients/client_template/job/deploy_job_template.ps1`
  (build image → `gcloud run jobs deploy template-export` → execute with `FORCE_REBUILD=1`).
- **Dashboard / web change** → `clients/client_template/dash/deploy_dash_template.ps1`
  (validate JS → build → `gcloud run deploy template-dash … --no-invoker-iam-check`).
- **Full standup of a new client** → copy `client_template`, then `deploy_template.ps1`.
- **Portal / Atrium change** → `services/portal/dash/deploy_dash_platform.ps1` (fast redeploy) or
  `services/portal/deploy.ps1` (full standup). **Ingest jobs** → `tools/deploy_ingest_jobs.ps1`.
  **Status dashboard** → `services/status-dashboard/deploy_status.ps1`.

`FORCE_REBUILD=1` is mandatory for view-only / code / seed changes: they do **not** advance the
upstream watermark, so without it the freshness gate no-ops and keeps serving stale JSON.

Org policy (Domain Restricted Sharing) rejects `--allow-unauthenticated`; all web services deploy
with `--no-invoker-iam-check` and do their own password/SSO auth in-process.

## Team workflow (branch → PR → CI → merge)

Multiple developers (each with their own Claude Code) work in parallel. To keep merges clean, follow
**`docs/dev-workflow.md`**: each machine pushes to its own branch with `tools/push-branch.ps1`, opens a
PR (CI runs the gates in `.github/workflows/ci.yml` — esprima JS gate, `py_compile`, the off-cloud
Atrium tests), and only green PRs merge to `main`.

**The release SOP is agent-driven:** a developer drops `tools/merge-branches.ps1` into Claude Code and
asks it to merge + deploy. The script runs the whole pipeline to live — fetch → `integration/merge`
off `origin/main` → run the CI tests → **land on `main`** → **auto-detect which services changed and
deploy each** (the path → deploy-script mapping lives in the script's `Resolve-DeployPlan`) → prune the
merged branches. It STOPS only where judgment is needed — a real merge conflict or a red test — and
hands off to the agent (see the AGENT RUNBOOK header in the script); the agent resolves it and re-runs.
`-DryRun` previews the land+deploy plan without changing anything; `-NoPush`/`-NoDeploy` recover the
review-first behavior; `-DeleteMerged` is the standalone prune. **Note:** enabling GitHub branch
protection on `main` (PR-required, per `docs/dev-workflow.md` step 5) would block this direct-to-main
land — keep protection off, or run with `-NoPush` and merge via PR, if you turn it on.

## Freshness contract (binding)

1. **Self-gating on a tick.** Each client export job (and the status dashboard) runs on its Cloud
   Scheduler tick (`*/10 * * * *` for exports, `*/15` for status) but only rebuilds when the shared
   `raw_windsor` mirror tables it reads advanced past a stored watermark. The Windsor ingest jobs
   are NOT self-gating — they are scheduled API pulls that WRITE `raw_windsor`.
2. **The watermark is a sidecar in the client's OWN bucket** — a `_freshness.json` object in
   `agora-data-driven-<c>-dash`. There is no separate freshness store and no database.
3. **Probe the BASE/MIRROR tables the views read — never watermark a VIEW.** A view has no
   last-modified time; watermark the `raw_windsor` mirror/base tables the views select from.
4. **`is_stale(observed, watermark)` returns True** if any observed upstream timestamp is newer than
   the watermark OR a probed key is absent. An **empty** observation set returns **False**, so a
   broken/empty probe never burns a rebuild.
5. **Write the watermark only AFTER a successful data upload.** `FORCE_REBUILD=1` bypasses the gate.

`freshness.py` signature (vendored identically into every export job):

```python
probe_bq_last_modified(bq, tables, location)       # __TABLES__.last_modified_time, keyed "dataset.table"
read_watermark(bucket, object_name)                # GCS JSON sidecar -> dict
write_watermark(bucket, object_name, observed)     # GCS JSON sidecar <- dict
is_stale(observed, watermark)                       # True if anything advanced or a key is missing
```

## Never

- **Never commit secrets.** Keys, `.p8`/`.pem`, `*credentials*.json`, `.env` are gitignored — keep
  it that way. Write secret material via UTF-8 (no BOM, no trailing newline) temp files.
- **Never make the data JSON public.** It is served only through the authenticated `/data.json`
  proxy. Buckets stay private.
- **Never edit views in the BigQuery console.** Views are code: edit `sql/*.sql` and reapply with
  `create_views.py`. The console is not the source of truth.
- **Never deploy via Cloud Build from a laptop**, and never use `--allow-unauthenticated`.

## Keep this file current

Updating docs is part of finishing a task — if a change alters the contract, the layout, or the
deploy steps, update this file (and the `.claude/CLAUDE.md` pointer) in the same change. **Volatile
status** (live URLs, dates, per-client deploy state) belongs in a README, never in CLAUDE.md.
