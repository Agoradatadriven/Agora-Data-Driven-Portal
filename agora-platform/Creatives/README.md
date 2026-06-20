# Creatives — Agora brand kit

Brand assets for the portal, the client dashboards, and the **Agora Atrium** client workspace. Keep
everything **self-contained** (no external font/image/script references) — the portal and dashboards
run under a strict environment where remote asset fetches are blocked.

## Structure

- `logo.svg` — the **AGORA master logo**, shared across every client. In Atrium it is the co-brand
  mark in the **light** sidebar, so it must read on white. Replace this file with the final artwork
  (keep the filename) and everything that references it picks it up.
- `clients/<c>.svg` — a **per-client logo**, one per client key `<c>` (e.g. `clients/riverdance.svg`).
  Shown beside the AGORA mark in that client's workspace. Add a file here and it flows into the client.

## How assets reach the app

The deployed container only bundles `agora-platform/dash/`, so it cannot read `Creatives/` at runtime.
Instead `dash/seed_workspace.py` **reads these files and inlines them** into the client's
`workspace/<c>.json` (`brand.agora_logo`, `brand.client_logo`) — embedded, self-contained SVG. After
adding or replacing a logo, refresh an existing workspace's branding (nothing else is touched, and no
redeploy is needed — logos are read from the workspace JSON at render time):

```powershell
.\.venv\Scripts\python.exe agora-platform\dash\seed_workspace.py --rebrand <c>
```

Fallbacks keep things tidy: if `logo.svg` is missing, a built-in light-theme AGORA wordmark is used;
if a client logo is missing, an initials monogram is generated from the client's name.

## Asset guidelines

- **SVG preferred** (crisp, tiny). PNG/JPG also fine — it'll be inlined as a `data:` URI.
- **Self-contained:** no external `<image href>` / font URLs. For SVG text, outline it to paths or use
  the system font stack, so it renders identically everywhere (custom brand fonts won't load).
- **AGORA `logo.svg`:** legible on a **white** background; a landscape lockup (~4:1) suits the sidebar
  (rendered ~120px wide).
- **Client `clients/<c>.svg`:** a **square-ish mark/monogram** works best (rendered ~34px); a wide
  wordmark gets tiny.
- Atrium light-theme palette: green `#41B54A`, violet `#6F61E8`, ink `#16181D`. (The dark portal /
  dashboard chrome still uses `--ag-accent:#5b8cff` / `--ag-accent-2:#27d3a2`.)
