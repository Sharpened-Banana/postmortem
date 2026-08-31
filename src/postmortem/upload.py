"""Optional client for POSTing an analyzed run report to the public
postmortem site (a separate web service; see ``site/postmortem_site/``).

Stdlib-only (``urllib.request``, no ``requests`` dependency), matching
this project's other HTTP client (``raiderio.py``): narrow exception
handling, explicit timeouts, and a fetch function that degrades
gracefully rather than raising.

Every upload is authenticated with a small random per-install token,
generated on first use and cached in the local config directory (see
``appdirs.py``) -- there's no login flow; the token just lets the site
attribute runs from the same install without asking for an account.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import __version__
from . import appdirs

#: Identifies this tool to the server. Kept in sync with pyproject.toml's
#: ``version`` by reusing the package's own ``__version__`` (both are
#: hand-maintained; there's no build-time sync between them) rather than
#: hardcoding a second copy of the version number here.
USER_AGENT = f"postmortem/{__version__}"

TOKEN_FILENAME = "upload_token.json"


def token_path() -> Path:
    """Full path to the locally stored upload-token file."""
    return appdirs.config_dir() / TOKEN_FILENAME


def load_or_create_token() -> str:
    """Return this install's upload token, generating and persisting a
    new one on first use.

    Tolerant of a missing or corrupt token file -- our own local state,
    same "don't crash, don't lose the ability to keep working" bar as
    ``cache.py``/``desktop/config.py``'s settings file: a missing file,
    unreadable file, invalid JSON, or a JSON value with no usable
    ``"token"`` string all just fall through to generating a fresh
    token rather than raising.
    """
    path = token_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        payload = None
    if isinstance(payload, dict):
        token = payload.get("token")
        if isinstance(token, str) and token:
            return token

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"token": token}, fh)
    try:
        # Best-effort: owner-read/write-only. Not fully meaningful on
        # every platform (e.g. Windows ACLs), so never let this crash
        # the (already-succeeded) token creation.
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def upload_report(
    report: dict[str, Any],
    url: str,
    *,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST an analyzed run ``report`` to ``url``'s ``/api/runs`` endpoint.

    ``token`` defaults to this install's locally stored/auto-generated
    upload token (see ``load_or_create_token``) when not given
    explicitly.

    Never raises. On success (2xx), returns the parsed JSON response
    body. On an HTTP error response, the server (per the site's design)
    returns a JSON error body even for 4xx/5xx status codes -- that body
    is parsed and returned directly when possible (e.g. a 409 conflict
    or 429 rate-limit's own ``{"error": "..."}"``), falling back to a
    synthesized ``{"ok": False, "error": "HTTP <code>: <reason>"}`` when
    it doesn't parse as JSON. On a network-level failure (no
    connection, DNS failure, timeout, etc.) or an unparseable *success*
    response, returns ``{"ok": False, "error": "..."}`` as well -- every
    failure path is a plain dict, never an exception, so callers (e.g.
    ``cli.py``'s ``cmd_analyze``) can treat uploading as a best-effort
    step that never disrupts the rest of their work.
    """
    if token is None:
        token = load_or_create_token()

    endpoint = f"{url.rstrip('/')}/api/runs"
    payload = json.dumps(report).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Upload-Token": token,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc.reason)}
    except (ValueError, OSError) as exc:
        # A 2xx response whose body wasn't valid JSON/UTF-8, or some
        # other low-level I/O hiccup not already covered above.
        return {"ok": False, "error": str(exc)}
