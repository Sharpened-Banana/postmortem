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
from typing import Any, Callable, Iterable, Iterator, Optional

API_URL = "https://raider.io/api/v1/characters/profile"
# mythic_plus_recent_runs (WP-C3) is requested alongside the existing
# fields purely additively -- one more comma-separated value in the same
# `fields` query param the character-profile endpoint already accepts,
# not a second HTTP call. See the "Official run matching" section below
# for why this field (rather than a separate /mythic-plus/runs endpoint)
# was chosen, and the same "we can't verify this against a real
# response" caveat that applies to it.
FIELDS = "mythic_plus_scores_by_season:current,mythic_plus_best_runs,mythic_plus_recent_runs"

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

    For each player whose profile is found, also attempts to match this
    analyzed run against that player's Raider.io "recent runs" (WP-C3,
    see the section below) and attaches ``raiderio_run`` on a match. The
    local run's own ``challenge_map_id``/``keystone_level``/completion
    time are read from ``report["run"]`` (``RunSegment.summary()``'s
    shape) rather than added as new parameters -- that dict already
    carries everything needed. A run with no completion time (abandoned/
    incomplete -- no ``end_ts``) or a ``report`` with no ``"run"`` key at
    all just skips run-matching entirely; no error, no crash.

    Returns the number of players enriched.
    """
    enriched = 0
    run = report.get("run") or {}
    challenge_map_id = run.get("challenge_map_id")
    keystone_level = run.get("keystone_level")
    completion_ts = run.get("end_ts")
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

        if completion_ts is not None:
            match = match_run(
                parse_recent_runs(profile),
                challenge_map_id=challenge_map_id,
                keystone_level=keystone_level,
                completion_ts=completion_ts,
            )
            if match is not None:
                player["raiderio_run"] = {
                    "score": match.get("score"),
                    "url": match.get("url"),
                    "level": match.get("keystone_level"),
                    "dungeon": match.get("dungeon"),
                }
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


# --- Official run matching (WP-C3) -------------------------------------------
#
# The plan doc floats two possible sources for a player's "recent runs":
# a dedicated `/api/v1/mythic-plus/runs` endpoint, or the character
# profile's `mythic_plus_recent_runs` field. We go with the latter:
#
# 1. It's one more value in the `fields` query param `fetch_character`
#    already sends -- see the FIELDS constant above -- so matching adds
#    *zero* new HTTP calls per player, not a second one to a different
#    endpoint.
# 2. It rides WP-C1's disk cache for free: cache.py wraps whatever
#    fetcher is passed to fetch_character/enrich_report, keyed off the
#    request URL, so a cached profile fetch already carries this data
#    with no new cache filename or invalidation logic needed.
# 3. `mythic_plus_scores_by_season`/`mythic_plus_best_runs` (already used
#    above) and `mythic_plus_recent_runs` are long-standing, stable,
#    well-known field names on the real public character-profile API --
#    unlike WP-C2's static-data endpoint (which serves this project's
#    entirely fictional dungeons and has no real-world shape to check
#    against), guessing that the same endpoint also accepts one more
#    plausible field name in its existing `fields` param is a much more
#    conservative bet than guessing at a whole second endpoint's
#    existence, path, and payload shape.
#
# That said: we have *not* verified the real shape of a
# `mythic_plus_recent_runs` entry against a live response, and this
# project's dungeons aren't real WoW content anyway -- so, same posture
# as WP-C2's static-data parser: ``parse_recent_runs`` below tries a
# short list of plausible field names per value, in priority order, and
# silently skips (never raises on) anything it doesn't recognize. An
# entry missing what's needed to attempt a match (map id, level, or a
# parseable completion timestamp) is dropped rather than guessed at.
#
# The matching logic itself (``match_run``) is the well-specified part
# of this WP -- exact map id + keystone level, completion time within
# window_s seconds, closest-in-time tiebreak -- and doesn't depend on
# any of the above guesswork being correct.

# Reuse WP-C2's map-id candidate keys: challenge_map_id means the same
# thing whether it's labeling a static-data dungeon entry or a specific
# completed run.
_RUN_MAP_ID_KEYS = _ID_KEYS
_RUN_LEVEL_KEYS = ("mythic_level", "keystone_level", "level")
_RUN_COMPLETED_TS_KEYS = (
    "completed_at", "completed_timestamp", "clear_time", "finished_at",
    "keystone_time", "clear_at",
)
_RUN_SCORE_KEYS = ("score", "mythic_rating", "rating")
_RUN_URL_KEYS = ("url",)
_RUN_DUNGEON_KEYS = ("dungeon", "name", "short_name")

# Candidate top-level keys the recent-runs list might be published
# under in a character profile payload.
_RECENT_RUNS_KEYS = ("mythic_plus_recent_runs", "recent_runs")


def _iter_recent_run_entries(profile: Any) -> Iterator[dict]:
    """Yield every dict that looks like a recent-run entry from a
    character profile payload. Never raises: a missing/renamed field, a
    non-list value, or non-dict entries are all just skipped."""
    if not isinstance(profile, dict):
        return
    for key in _RECENT_RUNS_KEYS:
        entries = profile.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    yield entry


def _extract_int_field(entry: dict, keys: tuple[str, ...]) -> Optional[int]:
    for key in keys:
        if key in entry:
            try:
                return int(entry[key])
            except (TypeError, ValueError):
                continue
    return None


def _extract_float_field(entry: dict, keys: tuple[str, ...]) -> Optional[float]:
    for key in keys:
        if key in entry:
            try:
                return float(entry[key])
            except (TypeError, ValueError):
                continue
    return None


def _extract_str_field(entry: dict, keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_timestamp(value: Any) -> Optional[float]:
    """Best-effort epoch-seconds from a timestamp field of unknown shape:
    a unix seconds/milliseconds number (or numeric string), or an
    ISO-8601 string (``...Z`` accepted). Returns None for anything
    unparseable -- never raises."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts / 1000.0 if ts > 10_000_000_000 else ts
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _parse_timestamp(float(text))
        except ValueError:
            pass
        try:
            from datetime import datetime
            iso = text[:-1] + "+00:00" if text.endswith("Z") else text
            return datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return None
    return None


def parse_recent_runs(profile: Any) -> list[dict[str, Any]]:
    """Best-effort list of candidate run records extracted from a
    character profile's recent-runs field (see module notes above).

    Each candidate dict carries ``challenge_map_id``, ``keystone_level``,
    ``completed_ts`` (epoch seconds), and best-effort ``score``/``url``/
    ``dungeon``. An entry missing a map id, level, or parseable
    completion timestamp -- the fields required to even attempt a match
    -- is dropped. Never raises: an unrecognized/malformed payload
    (missing fields, wrong types, not a list at all) just yields fewer
    or no candidates.
    """
    out: list[dict[str, Any]] = []
    for entry in _iter_recent_run_entries(profile):
        map_id = _extract_int_field(entry, _RUN_MAP_ID_KEYS)
        level = _extract_int_field(entry, _RUN_LEVEL_KEYS)
        completed_ts: Optional[float] = None
        for key in _RUN_COMPLETED_TS_KEYS:
            if key in entry:
                completed_ts = _parse_timestamp(entry[key])
                if completed_ts is not None:
                    break
        if map_id is None or level is None or completed_ts is None:
            continue
        out.append({
            "challenge_map_id": map_id,
            "keystone_level": level,
            "completed_ts": completed_ts,
            "score": _extract_float_field(entry, _RUN_SCORE_KEYS),
            "url": _extract_str_field(entry, _RUN_URL_KEYS),
            "dungeon": _extract_str_field(entry, _RUN_DUNGEON_KEYS),
        })
    return out


def match_run(
    candidates: Iterable[dict[str, Any]],
    *,
    challenge_map_id: Optional[int],
    keystone_level: Optional[int],
    completion_ts: Optional[float],
    window_s: float = 600,
) -> Optional[dict[str, Any]]:
    """Pick the candidate run record matching a local run.

    A candidate qualifies when its ``challenge_map_id`` and
    ``keystone_level`` match exactly and its ``completed_ts`` is within
    ``window_s`` seconds of ``completion_ts`` (either direction). When
    more than one candidate qualifies, the closest in time wins.

    Returns ``None`` -- never raises -- when there's nothing to match
    against (``challenge_map_id``/``keystone_level``/``completion_ts``
    missing, as for an incomplete/abandoned local run with no completion
    time) or when no candidate qualifies.
    """
    if challenge_map_id is None or keystone_level is None or completion_ts is None:
        return None
    best: Optional[dict[str, Any]] = None
    best_delta: Optional[float] = None
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        if cand.get("challenge_map_id") != challenge_map_id:
            continue
        if cand.get("keystone_level") != keystone_level:
            continue
        cts = cand.get("completed_ts")
        if not isinstance(cts, (int, float)):
            continue
        delta = abs(cts - completion_ts)
        if delta > window_s:
            continue
        if best is None or delta < best_delta:
            best = cand
            best_delta = delta
    return best
