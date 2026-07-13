# Client Communications — features & setup

This tab (`/w/<c>/conversations`, labelled **Client Communications**) is the running record of every
conversation with a client: emails, Upwork chats, and meetings. This doc lists what it does and how to
turn each piece on. It is the practical companion to the contract notes innnn
[`CLAUDE.md`](./CLAUDE.md) → *feedback_ai* and the root [`/CLAUDE.md`](../../../CLAUDE.md) Atrium section.

> **Nothing here needs new infrastructure to run.** The panels, badges, week filter, and the manual
> "add summary" flow all work on a default deploy. Only the optional **"Summarize with AI"** button
> needs a model wired (see §4).

---

## 1. Two panels: Email (+ Upwork) and Meeting

- **Email Summary** panel holds two channels merged into one date-sorted list: **email** entries and
  **Upwork chat** entries. Each card carries a small colour badge (violet **Email**, teal **Upwork**)
  so they never get mixed up.
- **Meeting Summary** panel holds meeting entries.
- Every entry stores a title, an optional "who was involved" line, the summary text, and an optional
  date. Data lives in the per-client workspace JSON as three lists: `email_summaries[]`,
  `meeting_summaries[]`, `upwork_summaries[]` (no database, no new bucket).

**How to use (team/super-admin):** open a panel's **+ Add**, pick the **Type** (Email / Upwork chat),
fill in title + summary (+ optional date), then **Add summary**. Clients see the saved cards read-only.

Minimum to save: a title **or** a summary. Everything else is optional.

---

## 2. Filter by week

A **Show** dropdown at the top filters both panels at once: **All time · This week · Last week ·
Last 4 weeks**. Calendar weeks start Monday (local time). Dateless entries show only under "All time".
Purely client-side — no reload, no server round-trip.

Nothing to enable. Always on.

---

## 3. "Summarize with AI" — one saved recap per week (Gemini)

On the Email panel's add form, **Summarize with AI**:

1. You paste a raw Upwork/email chat into the summary box.
2. Gemini splits it into **one recap per calendar week** and **saves each as its own dated card**
   (dated to that week's Monday, titled e.g. `Upwork chat (week of June 2)`).
3. The page reloads so each week appears as a separate card, filterable by the **Show** menu (§2).

The output is written to read like a person wrote it: **no em/en dashes, no asterisks, no bullets, no
markdown** (guaranteed by a post-processing sanitizer, `feedback_ai._humanize`, on top of the prompt).
Undated "June 2" text is anchored to the current year.

If Gemini is not wired, the button degrades gracefully to *"write it by hand"* — you just type the
summary and click **Add summary**. Nothing breaks.

---

## 4. How to fully integrate the AI button

Gemini is reached two ways; the code tries them **in this order**:

| Mode | When it's used | What to set |
|------|----------------|-------------|
| **Gemini API key** | Local dev / the preview | `GEMINI_API_KEY` env var (or a `.gemini_key` file) |
| **Vertex AI** (no key) | The deployed portal | `VERTEX_GEMINI_ENABLED=1` + runtime SA with `roles/aiplatform.user` |

### 4a. Local / preview (`run_local.ps1`)

1. Get a Gemini key from <https://aistudio.google.com/apikey>.
2. Save it as a file named `.gemini_key` at the **repo root** (`atrium/.gemini_key`) — just the key,
   nothing else. It is gitignored (`*.key`), so it is never committed.
3. Run `services/portal/dash/run_local.ps1` (or the double-click preview). On boot it prints
   **`Gemini ENABLED`**, loads the key, and the button drafts real summaries.

> The separate **"Generate strategy"** feature (campaign Google-Doc → strategy) uses **Claude**, not
> Gemini. To try that locally, drop an Anthropic key in `.anthropic_key` the same way.

### 4b. Production (`deploy_dash_platform.ps1`)

**Nothing extra is needed** — the deploy already enables Vertex Gemini:

- it runs `gcloud services enable aiplatform.googleapis.com`,
- grants the web SA `roles/aiplatform.user`,
- sets `VERTEX_GEMINI_ENABLED=1`, `VERTEX_PROJECT`, `VERTEX_LOCATION=global`.

So the button uses Vertex (GCP-billed, **no API key**, one invoice) in the deployed portal out of the
box. `gemini-2.5-flash` is the model. Deploy with:

```powershell
services\portal\dash\deploy_dash_platform.ps1
```

### 4c. (Optional) the Claude "Generate strategy" feature in production

Separate from the summarize button. To enable it, create the Anthropic secret once; the deploy then
mounts it and flips the flag automatically:

```bash
echo -n "sk-ant-YOUR-KEY" | gcloud secrets create ANTHROPIC_API_KEY --data-file=- --project=agora-data-driven
```

---

## 5. Environment variables & secrets (summary)

| Name | Purpose | Where | Required? |
|------|---------|-------|-----------|
| `GEMINI_API_KEY` | Summarize button (Gemini Developer API) | local `.gemini_key` / env | Local only |
| `VERTEX_GEMINI_ENABLED` `VERTEX_PROJECT` `VERTEX_LOCATION` | Summarize button (Vertex) | set by the deploy | Prod (auto) |
| `ANTHROPIC_API_KEY` + `FEEDBACK_AI_ENABLED=1` | "Generate strategy" (Claude) | Secret Manager / `.anthropic_key` | Optional |

Keys pasted anywhere public should be **rotated** in their consoles after testing.

---

## 6. Where the code lives

| File | Responsibility |
|------|----------------|
| `templates/atrium.html` | The Client Communications pane (two panels, badges, week filter, add forms, JS) |
| `atrium_view.py` | `communications(ws)` merge + `comms_inbox` / `comms_meetings` split for the two panels |
| `workspace.py` | `add_email_summary` / `add_meeting_summary` / `add_upwork_summary` + `_COMM_KINDS` add/edit/delete |
| `main.py` | Routes: `/admin/communication` (add/edit/delete), `/admin/summarize-conversation` (single draft), `/admin/summarize-weekly` (split + save per week) |
| `feedback_ai.py` | Gemini calls (`_gemini_generate`, `summarize_conversation`, `summarize_conversation_weekly`), the `_humanize` sanitizer, and the Claude strategy summarizer |
| `run_local.ps1` | Loads `.gemini_key` / `.anthropic_key` for local testing |
| `deploy_dash_platform.ps1` | Enables Vertex Gemini + mounts the optional Anthropic secret |

### Tests (run from `services/portal/dash/`)

```bash
python _atrium_smoketest.py      # routes + template + humanize/weekly checks (stubs GCS)
python _workspace_localtest.py   # workspace data layer
python ..\..\..\tools\_validate_dash_js.py templates\atrium.html   # inline-JS gate
```

Why is there no live AI in the tests? The AI calls hit an external model, so the tests only assert
the **graceful-degrade** path (unconfigured → `ok:false` / `[]`) and the pure helpers (`_humanize`,
`_parse_week_items`, `_week_label`). Verify the real summaries by running the preview with a key.
