# Agora Data Driven — brand board

> Clean, analytical, performance-focused marketing identity.
> **Design formula:** white space + bold black type + green CTA + subtle purple accent.

`brand.json` is the machine-readable copy of this board (the platform's runtime palette in
`dash/brand.py` mirrors it). Keep the two in step.

## Logo

The master lockup is a **monochrome graphite** mountain mark + `AGORA` wordmark + `DATA DRIVEN`
subline. Green and purple are **UI accent colours — they are not part of the logo lockup.**

| Variation | File | Use |
|-----------|------|-----|
| Master (primary) | `logo.svg` | light backgrounds — Atrium sidebar, portal header, login |
| Reversed | `logo-reversed.svg` | dark backgrounds (white on black) |
| Horizontal / compact | `logo-horizontal.svg` | tight lockups — mark + `AGORA` only |
| Icon mark | `logo-icon.svg` | favicon, square/compact contexts |

The shipped SVGs use the **system font stack** for the wordmark (a self-contained stand-in for the
brand fonts, which cannot be web-loaded in the locked-down runtime). Replace `logo.svg` with the
final vector export (keep the filename and the ~150×40 landscape proportion) and re-run the rebrand
step — and update `AGORA_LOGO_LIGHT` in `dash/brand.py` so the portal/login chrome matches.

## Colours

Standardized 2026-07 on the **website design system** (`website/src/styles/global.css`) so the whole
customer-facing suite — site, login, portal, console — reads as one brand.

| Token | Hex | Role |
|-------|-----|------|
| Data Green | `#4FA84A` | primary / CTA / positive (website brand-500) |
| Green (dark) | `#3F8B3B` | green text on light, hovers (website brand-600) |
| Green (tint) | `#EEF6ED` | soft green wash, chips (website tint) |
| Accent Purple | `#6A6AEA` | informational accent, dots, light tints (website accent-500) |
| Purple (deep) | `#5A54DD` | solid violet fills with white text, hovers (website accent-600) |
| Purple (tint) | `#ECECFB` | soft violet wash (website accent-100) |
| Graphite Black | `#000000` | graphite black |
| Ink | `#121212` | near-black bold type / the logo (website ink) |
| Charcoal Text | `#353535` | body copy |
| Soft Grey | `#E6E6E9` | hairlines / surfaces (website line) |
| Canvas | `#F7F7F8` | app canvas (off-white, website canvas) |

> **Green = primary action, purple = informational** (status chips, badges) — never mix the two.
> Solid green and the deep purple carry white text; the mid **Accent Purple `#6A6AEA`** is for
> dots, chips, and tints.

## Fonts

- **After Display Bold** — headlines, section titles, case-study titles.
- **Lato Regular** — body copy, navigation, insights, reports.
- **Fallback stack** (shipped): `-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif`.

## Voice

Confident, clear, analytical, helpful, action-oriented — a performance marketing agency that turns
data into action.

## How assets reach the app

See `README.md` in this folder. In short: the deployed container only bundles `dash/`, so the
runtime mark lives in `dash/brand.py`; `dash/seed_workspace.py` inlines `logo.svg` /
`clients/<c>.svg` into each `workspace/<c>.json` for the Atrium sidebar.
