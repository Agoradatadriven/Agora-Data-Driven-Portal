# Creatives

This folder holds **Agora Data Driven** brand creative assets — primarily logos and wordmarks used
across the portal and the client dashboards.

The assets here are **placeholders for now**. Replace them with the final brand artwork when it is
ready; keep the same filenames so the templates that reference them do not need to change.

## Contents

- `logo.svg` — placeholder Agora wordmark. Self-contained SVG (no external references), using the
  brand accent color `#5b8cff`. Safe to inline or `<img>`-embed.

## Notes

- Keep assets **self-contained** (no external font/image/script references). The portal and
  dashboards run under a strict environment where remote asset fetches are blocked.
- The brand palette lives in the templates as CSS custom properties:
  `--ag-accent:#5b8cff`, `--ag-accent-2:#27d3a2`, `--ag-bg:#0b1020`, `--ag-ink:#eaf0ff`.
