"""Agora Data Driven brand kit, bundled into the deployed container.

This is the ONE place the platform's runtime keeps the AGORA mark and the official brand palette,
so the portal, login, and Atrium all render the SAME logo and the SAME colours without any runtime
read of the assets/ folder (the deployed image only bundles dash/, never assets/).

Relationship to assets/ (the operator-facing brand kit):
  * assets/brand.json     -- machine-readable brand board; the COLOURS below mirror it.
  * assets/logo.svg        -- the master AGORA artwork; the SAME mark as AGORA_LOGO_LIGHT (the file
                                 is pretty-printed, the constant is one line -- they render identically).
                                 seed_workspace.py prefers that file (so an operator can drop in final
                                 vector art) and falls back to AGORA_LOGO_LIGHT here when it is absent.
  If you replace assets/logo.svg with new artwork, update AGORA_LOGO_LIGHT here too so the portal
  and login chrome stay in step with the Atrium sidebar.

The logos are self-contained SVG (no external font/image refs): the wordmark uses the system font
stack, which the brand-kit guidelines explicitly allow, so it renders identically everywhere.
"""

import base64
import os

# System font stack -- a self-contained stand-in for the brand fonts (After Display / Lato), which
# cannot be web-loaded in the locked-down runtime. Double-quoted so the inner 'Segoe UI' stays literal.
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

# --- Official brand palette (mirrors assets/brand.json) -------------------------------------
# Standardized 2026-07 on the WEBSITE design system (website/src/styles/global.css) so the whole
# customer-facing suite -- site, login, portal, console -- reads as one brand (see
# ATRIUM_CONSOLE_REDESIGN_PLAN.md Phase 5).
GREEN = "#4FA84A"          # Data Green -- primary CTA / positive (website brand-500)
GREEN_DARK = "#3F8B3B"     # deeper green -- text on light, hovers, accents (website brand-600)
GREEN_TINT = "#EEF6ED"     # soft green wash -- tints / chips (website tint)
PURPLE = "#6A6AEA"         # Accent Purple -- informational accent, dots, light tints (website accent-500)
PURPLE_DEEP = "#5A54DD"    # deeper violet -- solid fills with white text, hovers (website accent-600)
PURPLE_TINT = "#ECECFB"    # soft violet wash (website accent-100)
GRAPHITE = "#000000"       # Graphite Black
INK = "#121212"            # near-black ink for bold type (website ink)
CHARCOAL = "#353535"       # Charcoal Text -- body copy
SOFT_GREY = "#E6E6E9"      # Soft Grey -- hairlines / surfaces (website line)
CANVAS = "#F7F7F8"         # app canvas (off-white, website canvas)


def _logo(ink, sub):
    """Build the master AGORA lockup (mountain mark + wordmark) in the given ink/sub-ink colours."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="150" height="40" viewBox="0 0 150 40" '
        'role="img" aria-label="AGORA Data Driven">'
        '<g fill="none" stroke="%s" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M3 37 L19 4 L35 37" stroke-width="1.8"/>'
        '<path d="M12 37 L24 12" stroke-width="1.1" opacity="0.5"/>'
        '<path d="M11.5 24 L26.5 24" stroke-width="1.6"/>'
        '</g>'
        '<text x="48" y="24.5" font-family="%s" font-size="21" font-weight="500" '
        'letter-spacing="3.2" fill="%s">AGORA</text>'
        '<text x="49.5" y="35" font-family="%s" font-size="7.3" font-weight="600" '
        'letter-spacing="4.2" fill="%s">DATA DRIVEN</text>'
        '</svg>'
    ) % (ink, _FONT, ink, _FONT, sub)


def _bundled_png_logo(filename, w, h, disp_h=32):
    """Wrap a bundled PNG (dash/assets/<filename>) as a self-contained data-URI SVG, or None.

    The REAL Agora artwork ships as a PNG (not vector), so we inline it as a data URI -- still
    self-contained (no external ref). The viewBox preserves aspect, so CSS height controls the size.
    """
    path = os.path.join(os.path.dirname(__file__), "assets", filename)
    try:
        with open(path, "rb") as fh:
            uri = "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return None
    disp_w = round(w * disp_h / h)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="%d" height="%d" viewBox="0 0 %d %d" role="img" aria-label="AGORA Data Driven">'
        '<image href="%s" xlink:href="%s" width="%d" height="%d"/></svg>'
    ) % (disp_w, disp_h, w, h, uri, uri, w, h)


# Master logo for LIGHT backgrounds (Atrium sidebar, portal header, login card). Prefer the REAL
# Agora artwork bundled at dash/assets/agora_logo.png (420x101); fall back to the monochrome line-art
# lockup if it is absent. assets/logo.svg mirrors this same data-URI wrapper for seed_workspace.
AGORA_LOGO_LIGHT = _bundled_png_logo("agora_logo.png", 420, 101) or _logo(INK, CHARCOAL)

# Reversed logo for DARK backgrounds (the chrome injected over proxied dashboards).
AGORA_LOGO_DARK = _logo("#FFFFFF", "#C7CBD6")

# Compact horizontal lockup: the mark + AGORA wordmark only (no "DATA DRIVEN" subline).
AGORA_LOGO_HORIZONTAL = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="132" height="40" viewBox="0 0 132 40" '
    'role="img" aria-label="AGORA">'
    '<g fill="none" stroke="%s" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3 37 L19 4 L35 37" stroke-width="1.8"/>'
    '<path d="M12 37 L24 12" stroke-width="1.1" opacity="0.5"/>'
    '<path d="M11.5 24 L26.5 24" stroke-width="1.6"/>'
    '</g>'
    '<text x="48" y="29" font-family="%s" font-size="22" font-weight="500" '
    'letter-spacing="3.4" fill="%s">AGORA</text>'
    '</svg>'
) % (INK, _FONT, INK)

# Icon mark only (square) -- the layered peak. Used as the favicon and compact contexts.
AGORA_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 38 40" '
    'role="img" aria-label="AGORA">'
    '<g fill="none" stroke="#121212" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3 37 L19 4 L35 37" stroke-width="1.9"/>'
    '<path d="M12 37 L24 12" stroke-width="1.2" opacity="0.5"/>'
    '<path d="M11.5 24 L26.5 24" stroke-width="1.7"/>'
    '</g>'
    '</svg>'
)

# Green line-art peak -- the FALLBACK favicon, used only when the real PNG below is absent.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 38 40">'
    '<g fill="none" stroke="#4FA84A" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3 37 L19 4 L35 37" stroke-width="2.4"/>'
    '<path d="M12 37 L24 12" stroke-width="1.6" opacity="0.55"/>'
    '<path d="M11.5 24 L26.5 24" stroke-width="2.2"/>'
    '</g>'
    '</svg>'
)


def _favicon_from_png(filename, w, h, viewbox):
    """Crop the bundled logo PNG to its square MARK for a tab-sized favicon (data-URI SVG).

    A favicon must be square, but the real artwork is the wide horizontal lockup -- so we embed the
    PNG and use an SVG viewBox to show ONLY the left mark (measured at columns 5..113 of the 420x101
    art; the AGORA wordmark begins at column 127). Returns None if the PNG is absent, so the caller
    falls back to the line-art peak above.
    """
    path = os.path.join(os.path.dirname(__file__), "assets", filename)
    try:
        with open(path, "rb") as fh:
            uri = "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return None
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'viewBox="%s" role="img" aria-label="Agora">'
        '<image href="%s" xlink:href="%s" width="%d" height="%d"/></svg>'
    ) % (viewbox, uri, uri, w, h)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# The browser-tab icon: the square "Agora Web Logo" monogram (assets/agora_web_logo.png). Falls
# back to the cropped horizontal mark, then the green line-art peak, if a PNG is absent.
FAVICON_DATA_URI = (
    _favicon_from_png("agora_web_logo.png", 256, 256, "0 0 256 256")
    or _favicon_from_png("agora_logo.png", 420, 101, "0 -8 118 118")
    or "data:image/svg+xml;base64," + base64.b64encode(_FAVICON_SVG.encode("utf-8")).decode("ascii")
)


def monogram(display_name):
    """A tasteful initials monogram (rounded square, light theme) -- the client-logo fallback.

    Used when a client has no assets/clients/<c>.svg of their own, so a workspace always renders
    something on-brand rather than an empty box.
    """
    words = [w for w in (display_name or "").split() if w]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    size = 13 if len(initials) > 1 else 15
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="34" height="34" viewBox="0 0 34 34" '
        'role="img" aria-label="%s">'
        '<rect x="1" y="1" width="32" height="32" rx="9" fill="%s" stroke="%s" stroke-width="1.5"/>'
        '<text x="17" y="22" text-anchor="middle" font-family="%s" font-size="%d" '
        'font-weight="800" fill="%s">%s</text></svg>'
    ) % (display_name or "client", GREEN_TINT, GREEN, _FONT, size, GREEN_DARK, initials)
