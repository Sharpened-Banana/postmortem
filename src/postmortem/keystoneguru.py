"""Keystone.guru: list a profile's public routes and export them as MDT
strings -- so a user's routes can be pulled into Settings' per-dungeon
defaults in one click instead of pasted one at a time.

No API key involved. Keystone.guru's documented API is auth-gated (its
spec returns 401 anonymously), but the *website itself* fetches a
profile's public route list and each route's MDT export through two
plain AJAX endpoints that need no login -- confirmed live (2026-09-02)
by reading the site's own JS bundle (``custom-v15.22.0.js``) and then
calling both anonymously:

    GET /ajax/routes?user_id=<id>&draw=1&start=0&length=N&columns[0][data]=title&...
        -> DataTables JSON: {"data": [{"public_key", "title", "published",
           "dungeon": {"slug", "name", "mdt_supported", ...}, ...}],
           "recordsTotal": ...}
    GET /ajax/<public_key>/mdtExport?useCache=1
        -> {"mdt_string": "!~MDT2~...", "warnings": [...]}

``user_id`` is the number in a profile URL (``keystone.guru/profile/64246``).
Only routes the user has published are listed, which is exactly the set
they'd be able to share anyway. Everything is best-effort and stdlib
only (``urllib``), matching upload.py / raiderio.py: a network problem
raises :class:`KeystoneGuruError` with a message meant for the UI.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .net import https_context

BASE_URL = "https://keystone.guru"
_PAGE_SIZE = 25
_TIMEOUT_S = 15
# The site's own listing and export calls are XHR; sending the same
# header (plus a real-looking UA) is what makes them answer with JSON.
_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Postmortem desktop; +https://github.com/Sharpened-Banana/postmortem)",
}


class KeystoneGuruError(RuntimeError):
    """Anything that stops a sync: bad profile input, network failure,
    an unexpected response shape. The message is safe to show as-is."""


def parse_profile_id(text: str) -> int:
    """The numeric profile id from a profile URL or a bare number.

    Accepts ``https://keystone.guru/profile/64246``,
    ``keystone.guru/index.php/profile/64246/``, ``/profile/64246``, or just
    ``64246``. Anything else raises :class:`KeystoneGuruError`.
    """
    text = (text or "").strip()
    if not text:
        raise KeystoneGuruError("paste your Keystone.guru profile URL first")
    if text.isdigit():
        return int(text)
    m = re.search(r"/profile/(\d+)", text)
    if m:
        return int(m.group(1))
    raise KeystoneGuruError(
        "that doesn't look like a Keystone.guru profile URL -- it should "
        "look like https://keystone.guru/profile/12345"
    )


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers=_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S, context=https_context()) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise KeystoneGuruError(f"Keystone.guru answered HTTP {exc.code} for {url}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise KeystoneGuruError(f"could not reach Keystone.guru: {exc}") from None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise KeystoneGuruError("Keystone.guru returned something that isn't JSON") from None


def _listing_url(user_id: int, start: int, length: int) -> str:
    # The endpoint is a DataTables server-side source and validates that
    # the `columns` parameter is present ("columns parameter is required"
    # without it); one column is enough.
    params = {
        "draw": "1", "start": str(start), "length": str(length),
        "user_id": str(user_id),
        "columns[0][data]": "title", "columns[0][name]": "title",
        "columns[0][searchable]": "true", "columns[0][orderable]": "true",
        "columns[0][search][value]": "",
        "order[0][column]": "0", "order[0][dir]": "asc",
        "search[value]": "",
    }
    return f"{BASE_URL}/ajax/routes?{urllib.parse.urlencode(params)}"


def list_public_routes(user_id: int) -> list[dict[str, Any]]:
    """Every published route on a profile, as
    ``{"public_key", "title", "dungeon_slug", "dungeon_name", "mdt_supported"}``.
    Follows the listing's own paging until it runs out."""
    routes: list[dict[str, Any]] = []
    start = 0
    while True:
        payload = _get_json(_listing_url(user_id, start, _PAGE_SIZE))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise KeystoneGuruError("unexpected response from the Keystone.guru route list")
        rows = payload["data"]
        for row in rows:
            if not isinstance(row, dict) or not row.get("public_key"):
                continue
            dungeon = row.get("dungeon") if isinstance(row.get("dungeon"), dict) else {}
            slug = str(dungeon.get("slug") or "")
            routes.append({
                "public_key": str(row["public_key"]),
                "title": str(row.get("title") or ""),
                "published": bool(row.get("published", True)),
                "dungeon_slug": slug,
                # The listing's `name` is an i18n key; the slug reads fine.
                "dungeon_name": slug.replace("-", " ").title() if slug else None,
                "mdt_supported": bool(dungeon.get("mdt_supported", True)),
            })
        total = payload.get("recordsFiltered") or payload.get("recordsTotal") or 0
        start += len(rows)
        if not rows or start >= int(total):
            break
    return routes


def fetch_mdt_string(public_key: str) -> str:
    """The MDT export string for one route (what the site's "Export to
    MDT" button copies)."""
    key = re.sub(r"[^A-Za-z0-9]", "", public_key or "")
    if not key:
        raise KeystoneGuruError("missing route key")
    payload = _get_json(f"{BASE_URL}/ajax/{key}/mdtExport?useCache=1")
    text: Optional[str] = payload.get("mdt_string") if isinstance(payload, dict) else None
    if not text or not isinstance(text, str):
        raise KeystoneGuruError(f"Keystone.guru returned no MDT string for route {key}")
    return text.strip()
