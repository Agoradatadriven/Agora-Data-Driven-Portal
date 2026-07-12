# assets/ — Agora brand kit

Brand assets for the portal, the client dashboards, and the **Agora Atrium** client workspace. Keep
everything **self-contained** (no external font/image/script references) — the portal and dashboards
run under a strict environment where remote asset fetches are blocked.

## Structure

- `brand.md` / `brand.json` — the **brand board**: colors, fonts, logo variations, and guidelines.
  `brand.json` is the machine-readable source for theming the portal later.
- `logo.svg` — the **AGORA master logo** (primary lockup), shared across every client. In Atrium it
  is the co-brand mark in the **light** sidebar, so it must read on white. Replace with final artwork
  (keep the filename) and everything that references it picks it up.
- `logo-horizontal.svg`, `logo-icon.svg`, `logo-reversed.svg` — the other brand-board variations
  (compact lockup, icon mark only, white-on-black).
- `clients/<c>.svg` — a **per-client logo**, one per client key `<c>` (e.g. `clients/riverdance.svg`).
  Shown beside the AGORA mark in that client's workspace. Add a file here and it flows into the client.

> ⚠️ The shipped logo SVGs are **faithful recreations** from the brand-board image — wordmark/colours
> faithful (system-font stand-in for the After/Lato brand fonts), **mountain mark approximated**. The
> runtime mirrors them as `AGORA_LOGO_LIGHT` in `dash/brand.py` (so the portal/login chrome render the
> same mark without reading this folder). Replace `logo.svg` / `logo-icon.svg` with the real vector
> export when available, and update `brand.py` to match (see `brand.md`).

## How assets reach the app

The deployed container only bundles `services/portal/dash/`, so it cannot read `assets/` at runtime.
Instead `dash/seed_workspace.py` **reads these files and inlines them** into the client's
`workspace/<c>.json` (`brand.agora_logo`, `brand.client_logo`) — embedded, self-contained SVG. After
adding or replacing a logo, refresh an existing workspace's branding (nothing else is touched, and no
redeploy is needed — logos are read from the workspace JSON at render time):

```powershell
.\.venv\Scripts\python.exe services\portal\dash\seed_workspace.py --rebrand <c>
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
- **Official brand palette** (from `brand.json`, standardized 2026-07 on the website design system):
  Data Green `#4FA84A`, Accent Purple `#6A6AEA`, Graphite Black `#000000`, Charcoal Text `#353535`,
  Soft Grey `#E6E6E9`. The portal, login, and team console use this palette — a light canvas with
  bold black type, green = primary/CTA, purple = informational accent; the deeper companion `#5A54DD`
  carries white text where the mid Accent Purple cannot. The Atrium **client workspace** keeps its
  original palette by decision (2026-07-10).
