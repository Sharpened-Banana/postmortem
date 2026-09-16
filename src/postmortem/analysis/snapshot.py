"""Snapshot: a role-focused postmortem of the window around a keybind press.

The addon side (see docs/SNAPSHOT.md, section 1) marks the moment by
toggling combat logging off/on N times within ~1.5 s, which makes WoW
write N timestamped ``COMBAT_LOG_VERSION`` header lines in a row. N is
the presser's role (2 = healer, 3 = tank, 4+ = anything else). Roles are
unique inside a key, so the role alone says whose snapshot it is --
nothing here needs the local character's name.

This module is the analyzer half of that contract:

- :func:`find_markers` turns header clusters in a run's events into
  :class:`Marker` objects.
- :func:`build_snapshot` slices the run to ``[marker - before, marker +
  after]`` and re-runs the ordinary pull detection + stats on the slice,
  so every existing per-player number is window-limited for free, then
  adds what the run report doesn't have: per-second series (hp, damage
  taken, healing, mana), a healer- or tank-specific focus section, and
  the deaths / close calls / biggest hits inside +/-10 s of the press.

Everything degrades rather than fails: a window with no healer reports
``focus_player: None`` and says so, a tank whose mitigation spells aren't
in gamedata.ACTIVE_MITIGATION gets "no mitigation data", a log without
advanced logging has empty hp/mana series.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional

from ..combatlog.events import (
    Event,
    advanced_info,
    is_group_player,
    parse_damage,
    parse_heal,
    spell_info,
)
from ..combatlog.segmenter import RunSegment
from ..mdt.dungeon_data import DungeonDataStore
from .avoidable import AvoidableData
from .dispels import DispelData
from .gamedata import (
    ACTIVE_MITIGATION,
    DEFENSIVES,
    EXTERNALS,
    HEALER_COOLDOWNS,
    spec_info,
)
from .interruptibility import InterruptibilityData
from .pulls import DEFAULT_PULL_GAP_S, detect_pulls
from .run_analyzer import (
    _cc_summary,
    _enemy_cast_summary,
    _relativize,
    death_entries,
    player_entries,
)
from .stats import compute_stats
from .stealable import StealableData

#: Consecutive header lines closer than this are one marker. The addon
#: spaces its toggles 0.25 s apart, so a marker's headers are ~0.25 s
#: apart; the pairs WoW/the addon write on their own are further: the
#: login pair ~2 s (tests/fixtures/real_logs), and the addon's own
#: re-assert at CHALLENGE_MODE_START exactly 1.0 s (a real key,
#: 2026-09-16: 07:26:38.333 and 07:26:39.333) -- which the earlier rule
#: ("within 1.5 s of the cluster's FIRST header") would have read as a
#: healer marker at every key start. Gap between neighbours, not span.
MARKER_GAP_S = 0.6
#: Kept for callers that match a marker back to a timestamp (see
#: build_snapshot): the widest span a real marker can have.
MARKER_CLUSTER_S = 1.5
#: The role a header count encodes (docs/SNAPSHOT.md section 1); 4 or
#: more is "general", a lone header is not a marker at all.
ROLE_BY_COUNT = {2: "healer", 3: "tank"}
#: Radius of the "around the marker" section.
AROUND_MARKER_S = 10.0
#: The GCD the healer's "casts / possible GCDs" ratio assumes.
GCD_S = 1.5
#: Combat-log powerType for mana (Enum.PowerType.Mana).
MANA_POWER_TYPE = "0"
#: School mask bit for physical damage; anything else counts as magic.
PHYSICAL_SCHOOL = 0x1

ROLES = ("healer", "tank", "general")


@dataclass(frozen=True)
class Marker:
    ts: float
    role: str
    count: int


def role_for_count(count: int) -> Optional[str]:
    """The role a cluster of ``count`` headers encodes; None for a single
    header (a normal logging toggle, never a marker)."""
    if count < 2:
        return None
    return ROLE_BY_COUNT.get(count, "general")


def find_markers(events: list[Event]) -> list[Marker]:
    """Cluster consecutive ``COMBAT_LOG_VERSION`` events whose ts is within
    MARKER_GAP_S of the previous header; each cluster of 2+ is one
    marker at the first header's timestamp."""
    markers: list[Marker] = []
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    count = 0

    def flush() -> None:
        role = role_for_count(count)
        if role is not None and first_ts is not None:
            markers.append(Marker(ts=first_ts, role=role, count=count))

    for ev in events:
        if ev.name != "COMBAT_LOG_VERSION":
            continue
        if last_ts is not None and ev.ts - last_ts <= MARKER_GAP_S:
            count += 1
            last_ts = ev.ts
            continue
        flush()
        first_ts = last_ts = ev.ts
        count = 1
    flush()
    return markers


# --- one pass over the slice ---------------------------------------------

@dataclass
class _Scan:
    """Everything the series and focus sections need that compute_stats
    doesn't already expose, gathered in one pass over the sliced events."""
    # damage that landed on a group player: dicts with ts, player_guid,
    # player, spell, spell_id, source, amount, school, hp_pct
    hits: list[dict[str, Any]]
    # heals from a group player: ts, src_guid, src, dst_guid, dst, spell,
    # spell_id, amount (effective), overhealing
    heals: list[dict[str, Any]]
    # per player guid: name
    names: dict[str, str]
    # per player guid: list of (ts, hp_pct)
    hp: dict[str, list[tuple[float, float]]]
    # per player guid: list of (ts, mana_pct) -- only when powerType is mana
    mana: dict[str, list[tuple[float, float]]]
    # (player guid, spell_id) -> [[start, end], ...] buff windows, clipped
    # to the slice; a buff already up at the slice start counts from there
    aura_windows: dict[tuple[str, int], list[list[float]]]
    # player guid -> [(ts, spell_id, spell_name, target_name)]
    casts: dict[str, list[tuple[float, int, str, str]]]


def _scan(events: list[Event], start_ts: float, end_ts: float) -> _Scan:
    scan = _Scan(hits=[], heals=[], names={}, hp={}, mana={}, aura_windows={},
                 casts={})
    open_auras: dict[tuple[str, int], float] = {}

    def note_unit(ev: Event, guid: str, name: str) -> None:
        if name and guid not in scan.names:
            scan.names[guid] = name
        adv = advanced_info(ev)
        if adv is None or adv.info_guid != guid or not adv.max_hp:
            return
        scan.hp.setdefault(guid, []).append(
            (ev.ts, 100.0 * adv.current_hp / adv.max_hp)
        )
        if adv.power_type == MANA_POWER_TYPE and adv.max_power:
            scan.mana.setdefault(guid, []).append(
                (ev.ts, 100.0 * adv.current_power / adv.max_power)
            )

    for ev in events:
        name = ev.name
        if len(ev.params) < 8:
            continue
        src_guid, src_name, src_flags = ev.source_guid, ev.source_name, ev.source_flags
        dst_guid, dst_name, dst_flags = ev.dest_guid, ev.dest_name, ev.dest_flags
        src_is_player = is_group_player(src_flags)
        dst_is_player = is_group_player(dst_flags)

        damage = parse_damage(ev)
        if damage is not None:
            if name == "SWING_DAMAGE_LANDED":
                continue  # duplicates SWING_DAMAGE (see combatlog.events)
            if dst_is_player:
                note_unit(ev, dst_guid, dst_name)
                sp = spell_info(ev)
                hp_pct = scan.hp[dst_guid][-1][1] if scan.hp.get(dst_guid) else None
                scan.hits.append({
                    "ts": ev.ts,
                    "player_guid": dst_guid,
                    "player": dst_name,
                    "spell": sp.spell_name if sp else "Melee",
                    "spell_id": sp.spell_id if sp else 0,
                    "source": src_name or src_guid,
                    "amount": damage.amount,
                    "absorbed": damage.absorbed,
                    "school": damage.school,
                    "hp_pct": round(hp_pct, 1) if hp_pct is not None else None,
                })
            elif src_is_player:
                note_unit(ev, src_guid, src_name)
            continue

        heal = parse_heal(ev)
        if heal is not None:
            if dst_is_player:
                note_unit(ev, dst_guid, dst_name)
            if src_is_player and (dst_is_player or dst_guid == src_guid):
                sp = spell_info(ev)
                scan.heals.append({
                    "ts": ev.ts,
                    "src_guid": src_guid,
                    "src": src_name,
                    "dst_guid": dst_guid,
                    "dst": dst_name,
                    "spell": sp.spell_name if sp else "?",
                    "spell_id": sp.spell_id if sp else 0,
                    "amount": heal.effective,
                    "overhealing": heal.overhealing,
                })
            continue

        if name == "SPELL_CAST_SUCCESS":
            if src_is_player:
                note_unit(ev, src_guid, src_name)
                sp = spell_info(ev)
                if sp is not None:
                    scan.casts.setdefault(src_guid, []).append(
                        (ev.ts, sp.spell_id, sp.spell_name, dst_name)
                    )
            continue

        if name in ("SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH"):
            if dst_is_player:
                sp = spell_info(ev)
                if sp is not None:
                    key = (dst_guid, sp.spell_id)
                    if name == "SPELL_AURA_APPLIED" and key in open_auras:
                        # a re-application without a removal: close the
                        # old window so the uptime never double-counts
                        scan.aura_windows.setdefault(key, []).append(
                            [open_auras.pop(key), ev.ts]
                        )
                    open_auras.setdefault(key, ev.ts)
            continue

        if name == "SPELL_AURA_REMOVED":
            if dst_is_player:
                sp = spell_info(ev)
                if sp is not None:
                    key = (dst_guid, sp.spell_id)
                    # a removal with no application in the slice: the buff
                    # was already up when the window opened
                    started = open_auras.pop(key, start_ts)
                    scan.aura_windows.setdefault(key, []).append([started, ev.ts])
            continue

    for key, started in open_auras.items():
        scan.aura_windows.setdefault(key, []).append([started, end_ts])
    return scan


# --- series ----------------------------------------------------------------

def _bins(n: int) -> list[Optional[float]]:
    return [None] * n


def _build_series(scan: _Scan, start_ts: float, end_ts: float,
                  run_start: float) -> dict[str, Any]:
    n = int(end_ts - start_ts) + 1
    players: dict[str, dict[str, Any]] = {}

    def entry(guid: str) -> dict[str, Any]:
        name = scan.names.get(guid) or guid
        e = players.get(name)
        if e is None:
            e = players[name] = {
                "hp_pct": _bins(n),
                "damage_taken": [0] * n,
                "healing_done": [0] * n,
                "healing_received": [0] * n,
            }
        return e

    def idx(ts: float) -> int:
        return min(n - 1, max(0, int(ts - start_ts)))

    for guid, samples in scan.hp.items():
        e = entry(guid)
        for ts, pct in samples:
            e["hp_pct"][idx(ts)] = round(pct, 1)  # last sample in the second wins
    for guid, samples in scan.mana.items():
        e = entry(guid)
        series = e.setdefault("mana_pct", _bins(n))
        for ts, pct in samples:
            series[idx(ts)] = round(pct, 1)
    for hit in scan.hits:
        entry(hit["player_guid"])["damage_taken"][idx(hit["ts"])] += hit["amount"]
    for heal in scan.heals:
        entry(heal["src_guid"])["healing_done"][idx(heal["ts"])] += heal["amount"]
        if heal["dst_guid"] in scan.names or heal["dst_guid"] == heal["src_guid"]:
            entry(heal["dst_guid"])["healing_received"][idx(heal["ts"])] += heal["amount"]

    return {
        "step_s": 1,
        "t0": round(start_ts - run_start, 1),
        "n": n,
        "players": players,
    }


# --- focus sections --------------------------------------------------------

def _top_counter(counter: Counter, n: int = 10, key_names=("spell_id", "name")) -> list[dict[str, Any]]:
    out = []
    for key, total in counter.most_common(n):
        if isinstance(key, tuple):
            out.append({key_names[0]: key[0], key_names[1]: key[1], "total": total})
        else:
            out.append({"name": key, "total": total})
    return out


def _cooldown_casts(scan: _Scan, guid: str, table: dict[int, Any],
                    run_start: float) -> list[dict[str, Any]]:
    out = []
    for ts, spell_id, spell_name, target in scan.casts.get(guid, []):
        if spell_id in table:
            entry = table[spell_id]
            name = entry[0] if isinstance(entry, tuple) else entry
            out.append({
                "ts": ts, "t": round(ts - run_start, 1),
                "spell_id": spell_id, "spell": name or spell_name,
                "target": target or None,
            })
    return out


def _series_stats(values: list[Optional[float]]) -> dict[str, Optional[float]]:
    known = [v for v in values if v is not None]
    if not known:
        return {"start": None, "min": None, "end": None}
    return {"start": known[0], "min": min(known), "end": known[-1]}


def _healer_focus(focus, scan: _Scan, stats, series: dict[str, Any],
                  window_s: float, run_start: float) -> dict[str, Any]:
    guid = focus.guid
    by_target: Counter = Counter()
    overheal_by_target: Counter = Counter()
    for heal in scan.heals:
        if heal["src_guid"] == guid:
            by_target[heal["dst"] or heal["dst_guid"]] += heal["amount"]
            overheal_by_target[heal["dst"] or heal["dst_guid"]] += heal["overhealing"]
    raw = focus.healing_done + focus.overhealing
    overheal_pct = round(100.0 * focus.overhealing / raw, 1) if raw else None
    possible_gcds = window_s / GCD_S if window_s > 0 else 0
    gcd_use = (round(100.0 * focus.casts_total / possible_gcds, 1)
               if possible_gcds else None)
    own = series["players"].get(focus.name or guid, {})
    mana = _series_stats(own.get("mana_pct") or [])

    cooldown_table = {**DEFENSIVES, **HEALER_COOLDOWNS}
    dispels = [d for d in stats.dispel_events if d.get("player") == focus.name]
    damage_by_player = []
    for p in stats.players.values():
        if p.guid.startswith("_") or not p.damage_taken:
            continue
        damage_by_player.append({
            "name": p.name or p.guid,
            "damage_taken": p.damage_taken,
            "top_spells": [
                {"spell_id": sid, "name": sname, "total": total}
                for (sid, sname), total in p.damage_taken_by_spell.most_common(5)
            ],
        })
    damage_by_player.sort(key=lambda e: -e["damage_taken"])
    return {
        "role": "healer",
        "healing_done": focus.healing_done,
        "overhealing": focus.overhealing,
        "absorbs_granted": focus.absorbs_granted,
        "overheal_pct": overheal_pct,
        "healing_by_target": [
            {"name": name, "total": total,
             "overhealing": overheal_by_target[name]}
            for name, total in by_target.most_common()
        ],
        "healing_by_spell": _top_counter(focus.healing_by_spell, 15),
        "casts": focus.casts_total,
        "possible_gcds": round(possible_gcds, 1),
        "gcd_use_pct": gcd_use,
        "mana": mana,
        "cooldowns_used": _cooldown_casts(scan, guid, cooldown_table, run_start),
        "externals_given": _cooldown_casts(scan, guid, EXTERNALS, run_start),
        "dispels": dispels,
        "damage_taken_by_player": damage_by_player,
        "damage_by_source": [
            {"name": name, "total": total}
            for name, total in stats.enemy_damage_taken.most_common(10)
        ],
    }


def _tank_focus(focus, scan: _Scan, stats, series: dict[str, Any],
                window_s: float, run_start: float) -> dict[str, Any]:
    guid = focus.guid
    own_hits = [h for h in scan.hits if h["player_guid"] == guid]
    own = series["players"].get(focus.name or guid, {})
    per_second = own.get("damage_taken") or []
    total = sum(h["amount"] for h in own_hits)
    by_source: Counter = Counter()
    physical = 0
    for h in own_hits:
        by_source[h["source"]] += h["amount"]
        if h["school"] & PHYSICAL_SCHOOL and not (h["school"] & ~PHYSICAL_SCHOOL):
            physical += h["amount"]
    magic = total - physical

    _cls, _spec, _role = spec_info(focus.spec_id)
    mitigation = []
    for spell_id, (name, spec_ids) in ACTIVE_MITIGATION.items():
        windows = scan.aura_windows.get((guid, spell_id), [])
        casts = sum(1 for _ts, sid, _n, _t in scan.casts.get(guid, []) if sid == spell_id)
        relevant = spec_ids is None or (focus.spec_id in spec_ids)
        if not windows and not casts and not relevant:
            continue
        if not windows and not casts:
            continue  # relevant to the spec but never seen: still no data
        uptime = sum(max(0.0, end - start) for start, end in windows)
        mitigation.append({
            "spell_id": spell_id,
            "name": name,
            "casts": casts,
            "uptime_s": round(uptime, 1),
            "uptime_pct": round(100.0 * uptime / window_s, 1) if window_s > 0 else None,
        })
    # the Demon Spikes cast and its buff are two ids for one ability;
    # fold same-named rows together so the table reads as one line
    merged: dict[str, dict[str, Any]] = {}
    for row in mitigation:
        m = merged.get(row["name"])
        if m is None:
            merged[row["name"]] = row
        else:
            m["casts"] += row["casts"]
            m["uptime_s"] = round(m["uptime_s"] + row["uptime_s"], 1)
            m["uptime_pct"] = (round(100.0 * m["uptime_s"] / window_s, 1)
                               if window_s > 0 else None)
    mitigation = sorted(merged.values(), key=lambda r: -(r["uptime_s"] + r["casts"]))

    self_healing = sum(h["amount"] for h in scan.heals
                       if h["src_guid"] == guid and h["dst_guid"] == guid)
    biggest = sorted(own_hits, key=lambda h: -h["amount"])[:10]
    return {
        "role": "tank",
        "damage_taken": total,
        "dtps": {
            "peak": max(per_second) if per_second else 0,
            "mean": round(total / window_s, 1) if window_s > 0 else 0.0,
        },
        "damage_by_spell": _top_counter(focus.damage_taken_by_spell, 15),
        "damage_by_source": [
            {"name": name, "total": t} for name, t in by_source.most_common(10)
        ],
        "physical_vs_magic": {
            "physical": physical,
            "magic": magic,
            "physical_pct": round(100.0 * physical / total, 1) if total else None,
        },
        "active_mitigation": mitigation,
        "mitigation_note": None if mitigation else "no mitigation data",
        "self_healing": self_healing,
        "cooldowns_used": _cooldown_casts(scan, guid, DEFENSIVES, run_start),
        "biggest_hits": [
            {k: v for k, v in h.items() if k != "player_guid"} for h in biggest
        ],
    }


def _pick_focus(stats, role: str):
    """The group member whose spec role is ``role``; None when nobody in
    the slice has it (no COMBATANT_INFO, an unknown spec, or "general")."""
    if role not in ("healer", "tank"):
        return None
    for p in stats.players.values():
        if p.guid.startswith("_"):
            continue
        _cls, _spec, spec_role = spec_info(p.spec_id)
        if spec_role == role:
            return p
    return None


def _find_by_name(stats, name: str):
    """The player called ``name`` ("Dasfloof" or "Dasfloof-EmeraldDream-US"),
    case-insensitively; None when nobody matches."""
    want = name.strip().casefold()
    if not want:
        return None
    for p in stats.players.values():
        full = (p.name or "").casefold()
        if full == want or full.split("-", 1)[0] == want:
            return p
    return None


def _carry_combatant_info(segment: RunSegment, start_ts: float) -> list[Event]:
    """COMBATANT_INFO lines are written once at the key's start, so a slice
    that starts later would have no spec for anyone -- and no focus
    player. Carry the latest pre-window line per player into the slice,
    re-stamped to the window's start so compute_stats' own run bounds
    (its events[0].ts) stay inside the window."""
    latest: dict[str, Event] = {}
    for ev in segment.events:
        if ev.ts >= start_ts:
            break
        if ev.name == "COMBATANT_INFO" and ev.params:
            latest[ev.params[0]] = ev
    return [
        Event(start_ts, ev.name, ev.params, ev.line_no, ev.utc_offset)
        for ev in latest.values()
    ]


def build_snapshot(
    segment: RunSegment,
    marker_ts: float,
    *,
    before_s: float = 120.0,
    after_s: float = 60.0,
    role: str = "auto",
    store: Optional[DungeonDataStore] = None,
    avoidable: Optional[AvoidableData] = None,
    interrupt_data: Optional[InterruptibilityData] = None,
    stealable: Optional[StealableData] = None,
    dispel_data: Optional[DispelData] = None,
    pull_gap_seconds: float = DEFAULT_PULL_GAP_S,
    marker: Optional[Marker] = None,
    source: str = "marker",
    focus_name: Optional[str] = None,
) -> dict[str, Any]:
    """Build the snapshot report dict for the window around ``marker_ts``
    (see the module docstring and docs/SNAPSHOT.md section 2).

    ``role="auto"`` takes the marker's role (``marker`` when given, else
    the marker found at ``marker_ts`` in the segment, else "general").
    ``source`` records where the request came from ("marker" for a
    detected header cluster, "manual" for a CLI ``--at``, "hotkey" for
    the desktop app's global hotkey). ``focus_name`` picks the focus
    player by character name (with or without the realm) and takes the
    role from that player's spec -- the desktop hotkey's way of saying
    who pressed it, since no marker in the log carries a role then.
    """
    if role not in ROLES + ("auto",):
        raise ValueError(f"role must be one of {ROLES + ('auto',)}, got {role!r}")
    run_start = segment.start_ts
    run_end = segment.end_ts if segment.end_ts is not None else (
        segment.events[-1].ts if segment.events else run_start
    )
    start_ts = max(run_start, marker_ts - before_s)
    end_ts = min(run_end, marker_ts + after_s)
    if end_ts < start_ts:
        end_ts = start_ts
    window_s = end_ts - start_ts

    if role == "auto":
        if marker is None:
            for m in find_markers(segment.events):
                if abs(m.ts - marker_ts) <= MARKER_CLUSTER_S:
                    marker = m
                    break
        role = marker.role if marker is not None else "general"

    events = _carry_combatant_info(segment, start_ts) + [
        ev for ev in segment.events if start_ts <= ev.ts <= end_ts
    ]
    data = store.by_challenge_map_id(segment.challenge_map_id) if store else None
    if dispel_data is None:
        dispel_data = DispelData.load_bundled()
    pulls = detect_pulls(events, gap_seconds=pull_gap_seconds)
    stats = compute_stats(
        events, pulls, data, full_cast_timeline=True, avoidable=avoidable,
        keystone_level=segment.keystone_level, dispel_data=dispel_data,
        challenge_map_id=segment.challenge_map_id,
    )
    scan = _scan(events, start_ts, end_ts)
    series = _build_series(scan, start_ts, end_ts, run_start)

    if focus_name:
        named = _find_by_name(stats, focus_name)
        if named is not None:
            _cls, _spec, spec_role = spec_info(named.spec_id)
            role = spec_role if spec_role in ("healer", "tank") else "general"
    focus = _pick_focus(stats, role)
    if focus is not None and role == "healer":
        focus_section = _healer_focus(focus, scan, stats, series, window_s, run_start)
    elif focus is not None and role == "tank":
        focus_section = _tank_focus(focus, scan, stats, series, window_s, run_start)
    elif role in ("healer", "tank"):
        focus_section = {
            "role": role,
            "note": f"no {role} found in this window (no COMBATANT_INFO "
                    f"spec for one, or an unknown spec)",
        }
    else:
        focus_section = {"role": "general", "note": "no focus player for a general snapshot"}

    deaths = death_entries(stats)
    close_calls = [dict(c) for c in stats.close_calls]
    lo, hi = marker_ts - AROUND_MARKER_S, marker_ts + AROUND_MARKER_S
    around_hits = sorted(
        (h for h in scan.hits if lo <= h["ts"] <= hi),
        key=lambda h: -h["amount"],
    )[:10]
    around = {
        "radius_s": AROUND_MARKER_S,
        "deaths": [dict(d) for d in deaths if lo <= d["ts"] <= hi],
        "close_calls": [dict(c) for c in close_calls if lo <= c["ts"] <= hi],
        "largest_hits": [
            {k: v for k, v in h.items() if k != "player_guid"} for h in around_hits
        ],
    }

    players = player_entries(stats)
    active = stats.total_combat_s if stats.total_combat_s > 0 else window_s
    for p in players:
        healing = p["healing_done"] + p["absorbs_granted"]
        p["dps"] = round(p["damage_done"] / active, 1) if active > 0 else 0.0
        p["hps"] = round(healing / active, 1) if active > 0 else 0.0
        p["dtps"] = round(p["damage_taken"] / active, 1) if active > 0 else 0.0

    report: dict[str, Any] = {
        "snapshot": {
            "marker_ts": marker_ts,
            "t_marker": round(marker_ts - run_start, 1),
            "before_s": before_s,
            "after_s": after_s,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "t_start": round(start_ts - run_start, 1),
            "t_end": round(end_ts - run_start, 1),
            "window_s": round(window_s, 1),
            "active_s": round(active, 1),
            "role": role,
            "focus_player": (focus.name or focus.guid) if focus else None,
            "source": source,
            "marker_count": marker.count if marker else None,
        },
        "run": segment.summary(),
        "players": players,
        "pulls": stats.pull_stats,
        "deaths": deaths,
        "close_calls": close_calls,
        "dispels": stats.dispel_events,
        "interrupts": stats.interrupt_events,
        "enemy_casts": _enemy_cast_summary(stats, interrupt_data, stealable),
        "cc": _cc_summary(stats),
        "consumables": stats.consumable_events,
        "series": series,
        "focus": focus_section,
        "around_marker": around,
    }
    for key in ("pulls", "deaths", "close_calls", "dispels", "interrupts", "consumables"):
        _relativize(report[key], run_start)
    for p in report["pulls"]:
        p["t_start"] = round(p["start_ts"] - run_start, 1)
        p["t_end"] = round(p["end_ts"] - run_start, 1)
    _relativize(report["cc"]["events"], run_start, key="start_ts", target="t_start")
    _relativize(report["cc"]["events"], run_start, key="end_ts", target="t_end")
    for key in ("deaths", "close_calls", "largest_hits"):
        _relativize(around[key], run_start)
    if focus_section.get("role") == "tank":
        _relativize(focus_section.get("biggest_hits", []), run_start)
    return report
