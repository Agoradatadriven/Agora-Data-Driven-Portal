# template -- DASH web service (Stage 3)

This is the public-facing web service for the `template` client dashboard. It is Stage 3 of
the three-stage data contract: `sql/*.sql` view columns -> `job/main.py` `data` dict keys ->
the `data.*` keys `dashboard.html` reads. Renaming a key in one stage breaks the next.

## Serving model -- private bucket + Flask password gate (SSO additive)

- The exported data JSON (`template.json`) lives in a **private** GCS bucket
  (`agora-data-driven-template-dash`) and is **never** public.
- This Flask service renders a login, holds a signed session cookie, and proxies the private
  object at `/data.json` **only** to an authenticated session. Unauthenticated requests to
  `/data.json` get a `401`, never the data.
- A valid **portal SSO cookie** (signed by `platform-dash`, scoped to `.agoradatadriven.com`)
  is trusted **additively** via `platform_sso.py`: a portal login also opens this dashboard.
  The dashboard's **own password always still works**, independent of SSO. `authed()` is
  fail-closed -- if the SSO check ever raises, it falls back to the password path.
- The org forbids public Cloud Run, so the service is deployed with `--no-invoker-iam-check`
  (never `--allow-unauthenticated`) and does its own password/SSO auth in-process.

## Routes

| Route       | Method | Behaviour |
|-------------|--------|-----------|
| `/`         | GET    | Authenticated -> `dashboard.html` (`Cache-Control: no-store`); else the login page. |
| `/login`    | POST   | Constant-time (`hmac.compare_digest`) check of the submitted password vs `DASH_PASSWORD`; sets `session["ok"]=True` on match, else re-renders the login with an error (401). |
| `/logout`   | GET    | Clears the session, redirects to `/`. |
| `/data.json`| GET    | Auth-gated proxy of the private `DATA_OBJECT` from `GCS_BUCKET`; `401` if not authenticated, else the JSON with `Cache-Control: no-store`. |
| `/healthz`  | GET    | `200 "ok"` (liveness). |

`dashboard.html` is baked into the image and read relative to `__file__`, so there is no
filesystem dependency at runtime. Its inline JS fetches `/data.json`, shows
"Loading dashboard..." until the fetch resolves, then renders the KPI cards, a revenue
sparkline, and the daily table.

## Environment

| Var | Source | Purpose |
|-----|--------|---------|
| `SESSION_SECRET` | secret `template-dash-session-key` | Flask session signing key (`app.secret_key`). |
| `DASH_PASSWORD`  | secret `template-dash-password` | The dashboard's own password. |
| `GCS_BUCKET`     | env (`agora-data-driven-template-dash`) | Private data bucket. |
| `DATA_OBJECT`    | env (`template.json`) | Private data object proxied at `/data.json`. |
| `SSO_SECRET` / `CLIENT_KEY` | wired later by `tools/enable_platform_sso.ps1` | Additive portal SSO trust (optional). |

## Deploy

Run **as yourself** (never Cloud Build from a laptop):

```powershell
.\deploy_dash_template.ps1
```

The script first runs the repo `.venv` `tools/_validate_dash_js.py` over `dashboard.html`
and **aborts on failure** -- a JS syntax error would otherwise strand the page forever on
"Loading dashboard...". It then builds the image (`template-dash`), and `gcloud run deploy`s
the service in `asia-southeast1` with SA `template-dash-web@...`, the env vars and secrets
above, and `--no-invoker-iam-check`.

After deploy: map `template.agoradatadriven.com` to the service, then wire SSO with
`tools\enable_platform_sso.ps1 -Keys template`. Record the live URL in `LIVE_URL.md`.
