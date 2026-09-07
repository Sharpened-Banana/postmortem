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

import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import __version__
from . import appdirs
from .net import https_context

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


def site_base_url(url: str) -> str:
    """The site's origin from any URL a user is likely to paste as the
    "site URL": the home page, the ``/upload`` page, a ``/runs`` listing,
    or the ``/api/runs`` endpoint itself.

    Real bug (2026-09-02): Settings held ``https://<site>/upload`` -- the
    upload *page*, the natural thing to copy from the browser -- and
    every Watch Live upload went to ``<site>/upload/api/runs`` -> 404,
    quietly. A timed key was analyzed, written to history, and never
    reached the site. Any of those page paths now normalizes to the
    origin; a deliberately nested deployment (``https://host/postmortem``)
    still works, since only known page suffixes are stripped.
    """
    base = url.strip().rstrip("/")
    for _ in range(4):  # e.g. ".../upload/" or ".../api/runs" -> strip in turn
        for suffix in ("/api/runs", "/api", "/upload", "/runs", "/about", "/guide"):
            if base.lower().endswith(suffix):
                base = base[: -len(suffix)].rstrip("/")
                break
        else:
            break
    return base


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

    endpoint = f"{site_base_url(url)}/api/runs"
    # Never ship embedded dungeon map art to the site: it's Blizzard's
    # art read from the user's own MDT install for their own local report
    # (see mapart.py). Stripped here, at the one choke point every upload
    # path goes through, rather than trusting each caller to remember.
    from .mapart import strip_backgrounds
    payload = json.dumps(strip_backgrounds(report)).encode("utf-8")
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
        with urllib.request.urlopen(request, timeout=timeout, context=https_context()) as resp:
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


# -- device-code sign-in (phase 3 of ACCOUNTS_AND_PROGRESSION_PLAN.md) ------
#
# Links this install's own upload token (see load_or_create_token above)
# to a Postmortem account without the app ever touching Battle.net or a
# password: the site mints a short code, the app shows it and opens the
# confirmation page in the user's browser, and the app polls until the
# person confirms it there while already signed in. Same never-raises,
# always-a-plain-dict posture as upload_report -- every failure path
# (network error, site has accounts disabled, bad response) is
# ``{"ok": False, "error": "..."}`` (poll_device_link's non-ok shape is
# ``{"status": "error", "error": "..."}``, matching the site's own
# ``{"status": ...}`` success shape instead of adding a second key).


def _get_json(endpoint: str, *, headers: Optional[dict[str, str]] = None, timeout: float = 15.0) -> Any:
    request = urllib.request.Request(
        endpoint, headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout, context=https_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def start_device_link(
    url: str, *, token: Optional[str] = None, label: Optional[str] = None, timeout: float = 15.0,
) -> dict[str, Any]:
    """Ask ``url`` to start a device-link handshake for this install's
    upload token. On success: ``{"ok": True, "code", "poll_token",
    "verify_url", "expires_in"}`` -- show ``code``, open ``verify_url``
    in the user's browser, then hand ``poll_token`` to
    ``poll_device_link``. Never raises; every failure (network error,
    the site has accounts disabled, an unparseable response) is
    ``{"ok": False, "error": "..."}``."""
    if token is None:
        token = load_or_create_token()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    endpoint = f"{site_base_url(url)}/api/device/start"
    payload = json.dumps({"token_hash": token_hash, "label": label}).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=https_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc.reason)}
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def poll_device_link(url: str, poll_token: str, timeout: float = 15.0) -> dict[str, Any]:
    """Check the status of a code started with ``start_device_link``.
    Returns ``{"status": "pending"}``, ``{"status": "approved",
    "display_name": "..."}``, or ``{"status": "expired"}`` on success;
    ``{"status": "error", "error": "..."}`` on any request failure.
    Never raises -- meant to be called on a short timer until the status
    stops being ``"pending"``."""
    endpoint = f"{site_base_url(url)}/api/device/poll?" + urllib.parse.urlencode({"poll_token": poll_token})
    try:
        return _get_json(endpoint, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            if isinstance(body, dict) and "status" in body:
                return body
        except (ValueError, UnicodeDecodeError):
            pass
        return {"status": "error", "error": f"HTTP {exc.code}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"status": "error", "error": str(exc.reason)}
    except (ValueError, OSError) as exc:
        return {"status": "error", "error": str(exc)}


def whoami(url: str, *, token: Optional[str] = None, timeout: float = 15.0) -> dict[str, Any]:
    """Whether this install's upload token is linked to a Postmortem
    account. Returns ``{"linked": bool, "display_name": Optional[str]}``
    on success, ``{"linked": False, "error": "..."}`` on any request
    failure -- treated the same as "not linked" by callers, with the
    error available for a diagnostic message. Never raises."""
    if token is None:
        token = load_or_create_token()
    endpoint = f"{site_base_url(url)}/api/whoami"
    try:
        return _get_json(endpoint, headers={"X-Upload-Token": token}, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return {"linked": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"linked": False, "error": str(exc.reason)}
    except (ValueError, OSError) as exc:
        return {"linked": False, "error": str(exc)}
