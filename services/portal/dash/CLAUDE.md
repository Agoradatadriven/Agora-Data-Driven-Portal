# CLAUDE.md — services/portal/dash (the portal/CRM Flask app + Agora Atrium)

**Rules live in the repo-root [`/CLAUDE.md`](../../../CLAUDE.md)** — read it first; this file only
adds local context for this subtree. If they disagree, root wins.

You are in the **`platform-dash`** Cloud Run service: the portal/CRM front-door **and** Agora Atrium
(the co-branded client workspace). One self-contained Flask app, no build step.

- **`main.py`** — all routes (portal, Atrium client `/w/<c>/*`, admin `/w/<c>/admin/*` + the dark
  `/admin/atrium/*` console). `WORKSPACE_NAME` is the Atrium product-name constant.
- **`store.py`** — the registry (one private `platform.json`); **`workspace.py`** — per-client
  Atrium state (`workspace/<c>.json`). Both import `google-cloud-storage` lazily and have a local-fs
  backend (`REGISTRY_LOCAL_DIR` / `WORKSPACE_LOCAL_DIR`) so they run off-cloud.
- **`templates/*.html`** — big self-contained pages. Inline JS must be **esprima-4.x-safe** (no `?.`
  / `??`; classic `&&`/`||`). No Jinja inside `<script>` — JS reads state from the DOM.
- **`atrium_docs.py` / `feedback_ai.py`** — the opt-in Google-Doc → AI strategy feature (gated, degrades).
- **`brand.py`** — bundled palette + AGORA mark (the container can't read repo-root `assets/`).

**Deploy:** `deploy_dash_platform.ps1` (build → `gcloud run deploy platform-dash --no-invoker-iam-check`).
**Test (off-cloud, what CI runs):** `python _workspace_localtest.py` and `python _atrium_smoketest.py`
from this dir. **Preview:** `run_local.ps1` (or `preview/Preview Portal (admin).cmd` at repo root).
