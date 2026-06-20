# status -- DASH web service (fleet freshness monitor)

This is the **status dashboard**: a META dashboard that monitors the freshness of **all**
clients in one place. Unlike a client dashboard, it has **no dataset and no SQL views of its
own** -- there is no three-stage data contract here. It only renders the precomputed
`status.json` that the status **job** writes by rolling up every client's `_freshness.json`
watermark and exported `<c>.json`.

## What it shows

A per-client freshness table built from `data.clients[]`, with one row per client:

| Column | Meaning |
|--------|---------|
| `client` | The client key (e.g. `template`). |
| `last_updated` | When that client's export last rebuilt its data JSON. |
| `data_through` | The newest upstream timestamp covered by that client's data. |
| `last_json_update` | When the client's `<c>.json` object in GCS was last written. |
| `lag` | How stale the client is, rendered in minutes (under an hour) or hours. |
| `status` | A pill: **fresh** (Agora `--ag-accent-2`) or **stale** (Agora `--ag-danger`). |

Above the table is a small summary (clients monitored / fresh / stale). The page shows
"Loading..." until `fetch('/data.json')` resolves, then swaps in the table. Anything not
explicitly reported as `fresh` is treated as stale, so an unknown/missing state surfaces as a
problem rather than silently reading green.

## Serving model -- private bucket + Flask password gate (NO SSO)

- `status.json` lives in a **private** GCS bucket (`agora-data-driven-status-dash`) and is
  **never** public.
- This Flask service renders a login, holds a signed session cookie, and proxies the private
  object at `/data.json` **only** to an authenticated session. Unauthenticated requests to
  `/data.json` get a `401`, never the data.
- This is the **same** private-bucket serving pattern as a client dash, but **password-gated
  only** -- the status dashboard is an internal operator tool, so there is **no** additive
  portal SSO here and it does **not** import `platform_sso.py`.
- The org forbids public Cloud Run, so the service is deployed with `--no-invoker-iam-check`
  (never `--allow-unauthenticated`) and does its own password auth in-process.

## Routes

| Route        | Method | Behaviour |
|--------------|--------|-----------|
| `/`          | GET    | Authenticated -> `dashboard.html` (`Cache-Control: no-store`); else the login page. |
| `/login`     | POST   | Constant-time (`hmac.compare_digest`) check of the submitted password vs `DASH_PASSWORD`; sets `session["ok"]=True` on match, else re-renders the login with an error (401). |
| `/logout`    | GET    | Clears the session, redirects to `/`. |
| `/data.json` | GET    | Auth-gated proxy of the private `DATA_OBJECT` from `GCS_BUCKET`; `401` if not authenticated, else the JSON with `Cache-Control: no-store`. |
| `/healthz`   | GET    | `200 "ok"` (liveness). |

`dashboard.html` is baked into the image and read relative to `__file__`, so there is no
filesystem dependency at runtime. Its inline JS is written ES5/ES2015-safe (classic `&&`/`||`
guards, no `?.`/`??`) so it passes the pre-deploy esprima JS gate.

## Environment

| Var | Source | Purpose |
|-----|--------|---------|
| `SESSION_SECRET` | secret `status-dash-session-key` | Flask session signing key (`app.secret_key`). |
| `DASH_PASSWORD`  | secret `status-dash-password` | The dashboard's own password. |
| `GCS_BUCKET`     | env (`agora-data-driven-status-dash`) | Private status bucket. |
| `DATA_OBJECT`    | env (`status.json`) | Private status object proxied at `/data.json`. |

## Deploy

Run **as yourself** (never Cloud Build from a laptop):

```powershell
.\deploy_dash_status.ps1
```

The script first runs the repo `.venv` `scripts/_validate_dash_js.py` over `dashboard.html`
and **aborts on failure** -- a JS syntax error would otherwise strand the page forever on
"Loading...". It then builds the image (`status-dash`) and `gcloud run deploy`s the service in
`asia-southeast1` with SA `status-dash-web@...`, the env vars and secrets above, and
`--no-invoker-iam-check`.
