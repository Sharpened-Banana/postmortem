"""CSP source expressions for the report pages' inline scripts.

Both report pages carry exactly one executable inline ``<script>`` (the
renderer) plus a ``type="application/json"`` block holding the data, which
a browser never executes. The code is static -- it is part of the template
literal, not built per report -- so its SHA-256 is a stable identity, and a
Content-Security-Policy can name that hash instead of allowing inline
script wholesale.

That distinction is the point. The public site's policy allowed
``'unsafe-inline'``, which is precisely what both of the stored
cross-site-scripting findings of 2026-09-11 relied on: the header was
being leaned on as the safety net for known-imperfect embedding while
granting the one permission that removed the net. Hashes cost nothing,
need no per-response plumbing through the renderers, and stop working the
moment the script changes -- which is a deploy-time failure, not a silent
weakening.
"""

from __future__ import annotations

import base64
import hashlib
import re

from .html import _TEMPLATE
from .index import _INDEX_TEMPLATE

#: An executable inline script: a <script> with no type, or one whose type
#: is a JavaScript MIME. A data block (type="application/json") is not
#: executable and needs no hash.
_SCRIPT_RE = re.compile(
    r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
    re.DOTALL | re.IGNORECASE,
)
_TYPE_RE = re.compile(r"""type\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
_EXECUTABLE_TYPES = {"text/javascript", "application/javascript", "module"}


def inline_script_hashes(html: str) -> list[str]:
    """``'sha256-...'`` sources for every executable inline script in
    ``html``, in document order, ready to drop into a script-src list."""
    out: list[str] = []
    for match in _SCRIPT_RE.finditer(html):
        type_match = _TYPE_RE.search(match.group("attrs") or "")
        if type_match and type_match.group(1).lower() not in _EXECUTABLE_TYPES:
            continue
        digest = hashlib.sha256(match.group("body").encode("utf-8")).digest()
        out.append(f"'sha256-{base64.b64encode(digest).decode('ascii')}'")
    return out


def report_page_script_hashes() -> list[str]:
    """Hashes for both report pages' inline scripts.

    Read off the templates rather than off a rendered page, so no sample
    report is needed and the embedded data can never influence the result.
    """
    seen: list[str] = []
    for template in (_TEMPLATE, _INDEX_TEMPLATE):
        for source in inline_script_hashes(template):
            if source not in seen:
                seen.append(source)
    return seen
