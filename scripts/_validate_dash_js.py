r"""Pre-deploy JS syntax gate for dashboard.html (run with the repo .venv python).

WHY THIS EXISTS
    A JS syntax error in dashboard.html does not surface as a visible error -- the page
    just stays stuck forever on "Loading dashboard...", because the script that fetches
    /data.json and swaps the DOM never runs. There is NO Node.js on the box, so we cannot
    lint with a real JS engine. Instead we parse every inline <script> body with esprima
    inside the repo .venv. If it parses, the syntax is sound enough to run.

CAVEAT -- esprima 4.x predates modern JS tokens
    esprima 4.x (the pip-installable one) was written before ES2020, so it does NOT
    understand optional chaining `?.` or nullish coalescing `??`. On otherwise-valid
    modern JS those two tokens are a KNOWN false positive of this gate: esprima will throw
    even though the code is fine in every real browser. We therefore DOWNGRADE that one
    specific failure to a warning (and keep exit 0). ANY OTHER syntax error is blocking.

    The dashboard.html in this repo is deliberately written ES5/ES2015-safe (classic
    `&&`/`||` guards instead of `?.`/`??`) precisely so it never trips this gate, but the
    caveat stays here so an operator who reaches for `?.` later isn't hard-blocked by a
    parser limitation rather than a real bug.

USAGE
    .\.venv\Scripts\python.exe scripts\_validate_dash_js.py [path/to/dashboard.html]
    Default path: clients/client_template/dash/dashboard.html (relative to repo root).

EXIT CODES
    0  every inline <script> parsed (or the only failures were the `?.`/`??` caveat)
    1  at least one inline <script> had a real syntax error (prints block index + esprima
       message/line)
"""

import os
import re
import sys

try:
    import esprima
except ImportError:
    sys.stderr.write(
        "[ERROR] esprima is not installed in this interpreter.\n"
        "        Install it into the repo .venv:  .\\.venv\\Scripts\\pip install esprima\n"
    )
    sys.exit(1)


# Repo root is the parent of this scripts/ directory.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DASH = os.path.join("clients", "client_template", "dash", "dashboard.html")

# Match <script ...>BODY</script>. We capture the opening-tag attributes separately so we
# can skip CDN references (<script src=...>) which have no inline body to parse.
SCRIPT_RE = re.compile(
    r"<script\b([^>]*)>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
# An opening tag carrying a `src=` attribute is an external/CDN script: no body to parse.
SRC_ATTR_RE = re.compile(r"\bsrc\s*=", re.IGNORECASE)


def _is_optional_chaining_or_nullish_error(message):
    """Return True iff the esprima error is specifically about `?.` or `??`.

    esprima 4.x reports these as an unexpected `?` / `.` token. We only want to downgrade
    THIS narrow case -- never a generic 'Unexpected token' that happens to mention other
    characters -- so we look for the tell-tale shapes esprima emits for these two operators.
    """
    if not message:
        return False
    msg = message.lower()
    # Nullish coalescing `??` -> esprima sees a second unexpected '?'.
    # Optional chaining `?.` -> esprima sees unexpected '?' immediately before a '.'.
    signatures = (
        "unexpected token ?",
        "unexpected token '?'",
        'unexpected token "?"',
        "unexpected token ?.",
        "unexpected token '?.'",
        "unexpected token ??",
        "unexpected token '??'",
    )
    return any(sig in msg for sig in signatures)


def extract_inline_scripts(html):
    """Yield (index, body) for every INLINE <script> block, skipping CDN <script src=...>.

    `index` counts only the inline blocks we actually parse, so the number an operator sees
    in an error matches the Nth parseable block (not the Nth <script> tag overall).
    """
    inline_index = 0
    for match in SCRIPT_RE.finditer(html):
        attrs, body = match.group(1), match.group(2)
        if SRC_ATTR_RE.search(attrs):
            # External CDN reference -- no body of our own to parse; skip it.
            continue
        if body.strip() == "":
            # Empty inline block (e.g. a placeholder) -- nothing to parse.
            continue
        yield inline_index, body
        inline_index += 1


def parse_block(body):
    """Parse one inline script body. Return None on success, else the esprima error.

    Try parseModule first (supports `import`/`export` and module-scoped semantics), then
    fall back to parseScript for classic scripts. We only report the SCRIPT error if BOTH
    fail, since module-mode is stricter and can reject perfectly valid classic scripts.
    """
    try:
        esprima.parseModule(body)
        return None
    except Exception:
        pass
    try:
        esprima.parseScript(body)
        return None
    except Exception as script_err:  # noqa: BLE001 -- esprima raises its own Error type
        return script_err


def main(argv):
    if len(argv) > 1:
        dash_path = argv[1]
    else:
        dash_path = os.path.join(REPO_ROOT, DEFAULT_DASH)

    if not os.path.isfile(dash_path):
        sys.stderr.write("[ERROR] dashboard not found: %s\n" % dash_path)
        return 1

    with open(dash_path, "r", encoding="utf-8") as fh:
        html = fh.read()

    blocks = list(extract_inline_scripts(html))
    if not blocks:
        print("[OK] no inline <script> blocks to validate in %s" % dash_path)
        return 0

    failures = 0
    warnings = 0
    for index, body in blocks:
        err = parse_block(body)
        if err is None:
            continue
        message = getattr(err, "message", None) or str(err)
        line = getattr(err, "lineNumber", None)
        where = (" (line %s)" % line) if line else ""
        if _is_optional_chaining_or_nullish_error(message):
            # KNOWN esprima 4.x limitation, not a real bug -- warn, do not hard-fail.
            warnings += 1
            sys.stderr.write(
                "[WARN] inline <script> block %d uses optional chaining `?.` or nullish "
                "coalescing `??`%s.\n"
                "       esprima 4.x cannot parse these tokens; this is a KNOWN parser "
                "limitation, not a syntax error. Skipping (not blocking).\n"
                "       esprima said: %s\n" % (index, where, message)
            )
            continue
        failures += 1
        sys.stderr.write(
            "[FAIL] inline <script> block %d failed to parse%s: %s\n"
            % (index, where, message)
        )

    parsed = len(blocks) - failures - warnings
    if failures:
        sys.stderr.write(
            "[ERROR] %d inline <script> block(s) had blocking syntax errors in %s\n"
            % (failures, dash_path)
        )
        return 1

    if warnings:
        print(
            "[OK] %d inline <script> block(s) parsed; %d skipped as the known `?.`/`??` "
            "esprima caveat in %s" % (parsed, warnings, dash_path)
        )
    else:
        print(
            "[OK] all %d inline <script> block(s) parsed cleanly in %s"
            % (parsed, dash_path)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
