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
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

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


# --- Dungeon timer static data (WP-C2) --------------------------------------
#
# Raider.io also publishes per-season "static data" (dungeon list, par
# times, affixes) at /api/v1/mythic-plus/static-data?expansion_id=N. We
# have no way to verify the *real* current shape of that response, nor a
# real "current" expansion_id, against this project's fictional dungeons
# (Murder Row and friends aren't real WoW content) -- so two things follow:
#
# 1. ``expansion_id`` is always caller-supplied (see --expansion-id in
#    cli.py). We never guess a "current" one and present it as fact.
# 2. The parser below is deliberately tolerant of schema drift: it looks
#    for a dungeon list under a couple of plausible top-level/nested keys
#    and a couple of plausible id/par-time field names, and simply skips
#    (never raises on) anything it doesn't recognize. An unrecognized
#    payload just yields an empty mapping, which callers treat the same as
#    "fetch failed" -- falling back to a bundled/override file.
#
# See data/timers.json's own "_comment" for the matching caveat on the
# bundled fallback file, and docs/avoidable_spells.example.json for the
# same "we ship the mechanism + a labeled example, not a verified
# database" scoping this project already applies elsewhere.

STATIC_DATA_API_URL = "https://raider.io/api/v1/mythic-plus/static-data"

# Candidate keys a dungeon list might be published under, and candidate
# field names for a dungeon's challenge_map_id and par time, in priority
# order. All of this is a best guess -- not verified against a real
# response for this project's fictional content.
_DUNGEON_LIST_KEYS = ("mythic_plus_dungeons", "dungeons")
_ID_KEYS = ("challenge_mode_id", "id", "map_challenge_mode_id", "map_id")
_MS_KEYS = ("par_time_ms", "time_limit_ms", "timer_ms", "keystone_timer_ms")
_SECONDS_KEYS = ("par_time", "time_limit", "timer_1", "timer_seconds", "keystone_timer")


def static_data_url(expansion_id: int) -> str:
    query = urllib.parse.urlencode({"expansion_id": expansion_id})
    return f"{STATIC_DATA_API_URL}?{query}"


def fetch_static_data(
    expansion_id: Optional[int],
    fetcher: Fetcher = _default_fetcher,
) -> Optional[dict]:
    """Fetch Raider.io's season static-data payload for ``expansion_id``.

    ``expansion_id=None`` skips the fetch entirely (returns ``None``
    without ever calling ``fetcher``) -- callers use this as "no live
    fetch requested, go straight to the bundled/override fallback"
    rather than a guessed default id.
    """
    if expansion_id is None:
        return None
    return fetcher(static_data_url(expansion_id))


def _iter_dungeon_entries(payload: Any) -> Iterator[dict]:
    """Yield every dict that looks like a per-dungeon entry, searching a
    few plausible locations in the payload. Never raises: anything not
    shaped as expected (wrong type at any level) is just skipped."""
    if not isinstance(payload, dict):
        return
    for key in _DUNGEON_LIST_KEYS:
        entries = payload.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    yield entry
    seasons = payload.get("seasons")
    if isinstance(seasons, list):
        for season in seasons:
            if not isinstance(season, dict):
                continue
            for key in _DUNGEON_LIST_KEYS:
                entries = season.get(key)
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, dict):
                            yield entry


def _extract_map_id(entry: dict) -> Optional[int]:
    for key in _ID_KEYS:
        if key in entry:
            try:
                return int(entry[key])
            except (TypeError, ValueError):
                continue
    return None


def _extract_par_ms(entry: dict) -> Optional[int]:
    for key in _MS_KEYS:
        if key in entry:
            try:
                ms = int(entry[key])
            except (TypeError, ValueError):
                continue
            if ms > 0:
                return ms
    for key in _SECONDS_KEYS:
        if key in entry:
            try:
                seconds = float(entry[key])
            except (TypeError, ValueError):
                continue
            if seconds > 0:
                return int(round(seconds * 1000))
    return None


def parse_static_timers(payload: Any) -> dict[int, int]:
    """Best-effort ``challenge_map_id -> par_ms`` mapping from a
    static-data payload of unverified real-world shape (see module notes
    above). Never raises: an unrecognized payload shape, or one with no
    usable entries, just yields an empty mapping rather than crashing --
    this is the "tolerate schema drift" requirement for WP-C2.
    """
    timers: dict[int, int] = {}
    for entry in _iter_dungeon_entries(payload):
        map_id = _extract_map_id(entry)
        if map_id is None:
            continue
        par_ms = _extract_par_ms(entry)
        if par_ms is None:
            continue
        timers[map_id] = par_ms
    return timers


def _default_fallback_timers_path() -> Path:
    """``data/timers.json`` at the repo root, resolved relative to this
    module's own file. This works when running from a checkout or an
    editable install (``pip install -e .`` -- this project's documented
    dev workflow); a non-editable ``pip install .`` wheel that ends up
    without the repo layout alongside the installed package just won't
    find a file here. That's tolerated the same as any other missing
    fallback (see load_fallback_timers) -- pass --timer-data explicitly
    in that kind of install instead.
    """
    return Path(__file__).resolve().parents[2] / "data" / "timers.json"


def load_fallback_timers(path: Optional[str | Path] = None) -> dict[int, int]:
    """Load a ``challenge_map_id -> par_ms`` mapping from a JSON file
    shaped like ``data/timers.json`` (top-level ``"timers"`` object of
    string map-id -> integer par_ms; see that file's own "_comment").

    ``path=None`` resolves the bundled default. Unlike an explicitly
    passed ``--avoidable-data``/``--dungeon-data`` path (a clear CLI
    error on failure -- see cli.py), this is a fallback data source: a
    missing file, invalid JSON, or unexpected shape all just yield ``{}``
    rather than raising, matching cache.py's "don't crash, don't lose the
    ability to keep working" bar for our own non-user-typed inputs.
    """
    p = Path(path) if path is not None else _default_fallback_timers_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("timers")
    if not isinstance(raw, dict):
        return {}
    timers: dict[int, int] = {}
    for key, value in raw.items():
        try:
            map_id = int(key)
            par_ms = int(value)
        except (TypeError, ValueError):
            continue
        if par_ms > 0:
            timers[map_id] = par_ms
    return timers


def resolve_timer_map(
    expansion_id: Optional[int] = None,
    fetcher: Fetcher = _default_fetcher,
    fallback_path: Optional[str | Path] = None,
) -> dict[int, int]:
    """``challenge_map_id -> par_ms``, preferring a live Raider.io
    static-data fetch and falling back to a bundled/override JSON file.

    The live fetch is only attempted when ``expansion_id`` is given (see
    fetch_static_data). The fallback (``load_fallback_timers``) is used
    whenever the live fetch wasn't attempted, failed (fetcher returned
    ``None``), or returned a payload that parsed to nothing usable --
    covering "offline", "no expansion_id configured", and "the live
    response didn't match any shape we recognize" the same way.
    """
    timers = parse_static_timers(fetch_static_data(expansion_id, fetcher=fetcher))
    if timers:
        return timers
    return load_fallback_timers(fallback_path)
