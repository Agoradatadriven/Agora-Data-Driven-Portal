"""Wrap a raster logo (PNG/JPG) into a self-contained Creatives/clients/<key>.svg.

The Atrium seed reads ONLY Creatives/clients/<key>.svg and inlines it into the client's workspace
(brand.client_logo, rendered ~34px beside the AGORA mark). Client logos almost always arrive as
PNG/JPG, so this downscales the raster (a 34px mark does not need a 900 KB image) and embeds it as a
data: URI inside a tiny SVG wrapper -- self-contained, no external refs, exactly what the seed reads.

    python logo_to_svg.py <key> <path-to-image>      # -> Creatives/clients/<key>.svg

True vectorisation of a photographic/raster logo is not realistic; this preserves the artwork at a
sensible size. If a designer later provides a real vector SVG, drop that in directly instead.
"""

import base64
import io
import os
import sys

from PIL import Image

_CREATIVES_CLIENTS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "Creatives", "clients")
)
MAX_EDGE = 256  # longest side in px -- ample for a 34px sidebar mark even on hi-dpi screens


def convert(key, image_path, max_edge=MAX_EDGE):
    """Write Creatives/clients/<key>.svg wrapping `image_path` (downscaled). Returns (path, (w, h))."""
    img = Image.open(image_path).convert("RGBA")  # RGBA preserves logo transparency
    img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    w, h = img.size

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img" aria-label="%s logo">'
        '<image width="%d" height="%d" preserveAspectRatio="xMidYMid meet" '
        'href="data:image/png;base64,%s"/></svg>'
    ) % (w, h, w, h, key, w, h, b64)

    os.makedirs(_CREATIVES_CLIENTS, exist_ok=True)
    out = os.path.join(_CREATIVES_CLIENTS, "%s.svg" % key)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(svg)
    return out, (w, h)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    key = argv[0].strip().lower()
    out, (w, h) = convert(key, argv[1])
    kb = os.path.getsize(out) / 1024.0
    print("[logo_to_svg] wrote %s  (%dx%d, %.0f KB)" % (out, w, h, kb))
    print("[logo_to_svg] now run: seed_workspace.py --rebrand %s  to pull it into the workspace." % key)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
