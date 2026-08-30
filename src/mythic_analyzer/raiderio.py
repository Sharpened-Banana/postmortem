"""Optional Raider.io enrichment for run reports.

Adds each player's current Mythic+ score and season-best info to the
report via the public Raider.io API (https://raider.io/api). Entirely
optional and failure-tolerant: no network, an unknown realm, or a
renamed character just leaves that player un-enriched with a note.

The combat log writes names as "Name-RealmNameNoSpaces"; Raider.io wants
a realm slug ("tarren-mill"). The slug is reconstructed heuristically by
splitting the camel-cased realm on case and letter/digit boundaries —
correct for the vast majority of realms, and a miss only costs the
enrichment for that one player.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

API_URL = "https://raider.io/api/v1/characters/profile"
FIELDS = "mythic_plus_scores_by_season:current,mythic_plus_best_runs"

Fetcher = Callable[[str], Optional[dict]]


def realm_slug(realm: str) -> str:
    """Best-effort slug from a combat-log realm name ("TarrenMill", "Area52")."""
    realm = realm.replace("'", "").replace(" ", "-").replace("_", "-")
    # split CamelCase and letter<->digit boundaries with hyphens
    realm = re.sub(r"(?<=[a-z])(?=[A-Z])", "-", realm)
    realm = re.sub(r"(?<=[A-Za-z])(?=\d)", "-", realm)
    realm = re.sub(r"(?<=\d)(?=[A-Za-z])", "-", realm)
    return realm.lower()


def _default_fetcher(url: str) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def fetch_character(
    region: str,
    realm: str,
    name: str,
    fetcher: Fetcher = _default_fetcher,
) -> Optional[dict[str, Any]]:
    query = urllib.parse.urlencode({
        "region": region,
        "realm": realm,
        "name": name,
        "fields": FIELDS,
    })
    return fetcher(f"{API_URL}?{query}")


def _summarize(profile: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "profile_url": profile.get("profile_url"),
        "class": profile.get("class"),
        "active_spec": profile.get("active_spec_name"),
    }
    seasons = profile.get("mythic_plus_scores_by_season") or []
    if seasons:
        scores = seasons[0].get("scores") or {}
        out["score"] = scores.get("all")
    best = profile.get("mythic_plus_best_runs") or []
    if best:
        top = max(best, key=lambda r: r.get("mythic_level") or 0)
        out["season_best"] = {
            "dungeon": top.get("dungeon"),
            "level": top.get("mythic_level"),
            "clear_time_ms": top.get("clear_time_ms"),
            "url": top.get("url"),
        }
    return out


def enrich_report(
    report: dict[str, Any],
    region: str,
    fetcher: Fetcher = _default_fetcher,
) -> int:
    """Attach a ``raiderio`` block to every resolvable player in-place.

    Returns the number of players enriched.
    """
    enriched = 0
    for player in report.get("players", []):
        full_name = player.get("name") or ""
        if "-" not in full_name:
            continue
        char_name, _, realm = full_name.partition("-")
        profile = fetch_character(region, realm_slug(realm), char_name,
                                  fetcher=fetcher)
        if profile is None or "name" not in profile:
            player["raiderio"] = {"error": "lookup failed"}
            continue
        player["raiderio"] = _summarize(profile)
        enriched += 1
    report["raiderio"] = {
        "region": region,
        "enriched_players": enriched,
        "source": "https://raider.io",
    }
    return enriched
