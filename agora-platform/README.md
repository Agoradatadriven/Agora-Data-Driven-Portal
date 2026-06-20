# `agora-platform/` — the portal / CRM front-door

This is the **front-door** for Agora Data Driven: a single Cloud Run service, `platform-dash`,
served at **portal.agoradatadriven.com**. It is a reverse proxy plus a single login over every
per-client dashboard, and the home of the lightweight CRM. One project, one region
(`asia-southeast1`, Singapore), one shared Artifact Registry repo (`agora`).

## Plain English

A client (or a team member) logs in **once** at `portal.agoradatadriven.com`. From there they see
only the dashboards assigned to them, and they open each one **through the portal** — the portal
reverse-proxies `<c>.agoradatadriven.com` behind that single login. They never see a separate login
per dashboard. Internally each dashboard still has its own password gate; the portal login is
trusted **additively** on top of it (see SSO below), never as a replacement.

## What it is

- **Reverse proxy + single login.** `platform-dash` authenticates the user once, then proxies each
  client's dashboard under `/d/<c>/`. The per-client dashboards remain independent Cloud Run
  services (`<c>-dash`); the portal is the unified entry point in front of them.
- **The registry is ONE private JSON — no database.** All portal state (which clients exist, their
  display names, who may see what, and the CRM records described below) lives in a single private
  GCS object, `agora-data-driven-platform-dash/platform.json`. There is **no database**. The portal
  reads and writes that one object via its runtime service account
  (`platform-dash-web@agora-data-driven.iam.gserviceaccount.com`, which holds `objectAdmin` on the
  registry bucket). The bucket is private (uniform bucket-level access, no public grant) and is read
  only behind the portal login. `dash/seed_registry.py` writes the initial `platform.json`.

## How SSO works

The portal **mints** a signed cookie on a successful login, scoped to `.agoradatadriven.com` (note
the leading dot). Because of that leading dot the cookie is presented to **every**
`<c>.agoradatadriven.com` dashboard. Each dashboard **verifies** that cookie with the shared
vendored module `platform_sso.py` (HMAC-SHA256 over a small JSON payload), using the shared signing
key stored in Secret Manager as **`platform-sso-key`**.

- The signing key is created during the portal standup (`deploy_platform.ps1`) — the portal is the
  party that *signs*, so the key is born here.
- Dashboards are wired to *trust* that cookie **additively** by `scripts/enable_platform_sso.ps1`,
  which grants each dashboard's runtime SA read access to `platform-sso-key`, mounts it as the
  `SSO_SECRET` env var, and tells the dashboard its own `CLIENT_KEY`. A dashboard accepts the cookie
  only if the signature verifies, it has not expired, and its `CLIENT_KEY` is in the cookie's
  allowed-client list (or the cookie grants `*`, i.e. super-admin / all clients).
- **Additive, never a replacement.** Each dashboard's own password **always still works**. SSO is
  fail-safe by design: on a raw `*.run.app` host the `.agoradatadriven.com` cookie is never sent, so
  SSO is silently inert there and the password gate is the only path in. SSO only fires once a
  dashboard is served on its `<c>.agoradatadriven.com` custom domain.

The cookie name, domain, TTL, and the fail-closed verifier all live in the canonical
`platform_sso.py`, which is vendored byte-identically into `agora-platform/dash/` (the minting side)
and into each `clients/client_<c>/dash/` (the verifying side).

## The super-admin console

`scripts/enable_super_admin.ps1` grants the portal front-door god-mode so it can act as an operator
console straight from the browser instead of a laptop. It:

- creates a bootstrap **super-admin password** (`platform-super-admin-password`), mounted on the
  portal as `SUPER_ADMIN_PW`, and sets `REGION` so the console can address Cloud Run resources;
- grants the portal web SA project-level `roles/run.developer` so it can redeploy/rotate dashboards;
- per deployed dashboard, grants the web SA `secretVersionAdder` on that client's
  `<c>-dash-password` (to rotate the client password) and `serviceAccountUser` on the dashboard's
  runtime SA (to redeploy it).

Run it once during standup; it is safe to re-run (re-adding an existing IAM binding is a no-op, and
an already-present secret is left untouched).

## The CRM growth path

The same private `platform.json` registry is where the CRM grows. Alongside the dashboard
assignments it carries:

- **client records** — the businesses Agora works with (key, display name, contacts, status);
- **notes** — free-text history against a client;
- **tasks** — follow-ups / to-dos against a client.

Because it is one JSON object behind the portal login (no database to stand up), the CRM extends by
adding fields to the registry and UI to the portal — not by introducing new infrastructure.

## Deploying

Two scripts, both **run as yourself** from the repo root (never Cloud Build from a laptop — the
Cloud Build SA cannot `iam.serviceAccounts.actAs` our runtime SAs, so a cloudbuild-driven deploy
fails; build the image as yourself, then `gcloud run deploy`).

| Script | What it does | When to run |
|--------|--------------|-------------|
| `deploy_platform.ps1` | **One-shot, idempotent standup.** Enables APIs; ensures the `agora` AR repo + the private `agora-data-driven-platform-dash` bucket; creates the `platform-dash-web@` SA and its IAM (`objectAdmin` on the registry bucket, `secretAccessor`); creates the two secrets `platform-dash-session-key` and the shared `platform-sso-key`; builds + deploys `platform-dash` with `COOKIE_DOMAIN=.agoradatadriven.com` and the secrets, using `--no-invoker-iam-check`; then seeds `platform.json` via the repo `.venv` python on `dash/seed_registry.py`. | Once at standup; re-run to converge. |
| `dash/deploy_dash_platform.ps1` | **Redeploy the `platform-dash` service only** — build a fresh image and create-or-update the service (env + secrets, `--no-invoker-iam-check`). Does not touch IAM/bucket/secrets. | Every code/template change after standup. |

```powershell
# from the repo root
.\agora-platform\deploy_platform.ps1                 # full standup / converge
.\agora-platform\dash\deploy_dash_platform.ps1       # fast redeploy of the portal only
```

After standup: map **portal.agoradatadriven.com** onto the `platform-dash` service, then run
`scripts\enable_super_admin.ps1` and `scripts\enable_platform_sso.ps1 -Keys "template"`.

### Org policy: no public Cloud Run

Domain Restricted Sharing rejects `--allow-unauthenticated`. The portal is therefore deployed with
`--no-invoker-iam-check`; the Flask app does its **own** password / SSO auth in-process, and the
private registry JSON is only ever read behind the portal login.

## A note on volatile values

The **live URL** of the deployed service (the `*.run.app` URL, and the custom domain mapping status
for `portal.agoradatadriven.com`) is volatile operational state — it lives **here**, not in
`CLAUDE.md`. `CLAUDE.md` is reserved for fixed facts (project, region, the data contract, the deploy
procedure, guardrails); anything that changes when you redeploy or remap a domain belongs in this
README or in your own operator notes.

## See also

- `scripts/README.md` — the operator convenience scripts, including `enable_platform_sso.ps1` and
  `enable_super_admin.ps1`.
- `dash/platform_sso.py` — the canonical SSO signer/verifier (vendored byte-identically into each
  dashboard).
