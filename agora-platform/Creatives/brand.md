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

| Token | Hex | Role |
|-------|-----|------|
| Data Green | `#4FAB4A` | primary / CTA / positive |
| Green (dark) | `#347A30` | green text on light, hovers |
| Green (tint) | `#EAF6E9` | soft green wash, chips |
| Accent Purple | `#9484FB` | subtle accent, dots, light tints |
| Purple (deep) | `#5C4BD0` | solid violet fills with white text, hovers |
| Purple (tint) | `#F1EFFE` | soft violet wash |
| Graphite Black | `#000000` | graphite black |
| Ink | `#1A1B1E` | near-black bold type / the logo |
| Charcoal Text | `#353535` | body copy |
| Soft Grey | `#EEEEEE` | hairlines / surfaces |
| Canvas | `#F6F7F9` | app canvas (off-white) |

> Solid green and the deep purple carry white text; the pale **Accent Purple `#9484FB`** is for
> dots, hairlines, and tints only — it is too light for white text on it.

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
