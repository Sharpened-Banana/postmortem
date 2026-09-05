"""Tie everything together: one M+ run in, one report structure out."""

from __future__ import annotations

from typing import Any, Optional

from ..combatlog.segmenter import RunSegment
from ..mdt.dungeon_data import DungeonData, DungeonDataStore
from ..mdt.route import Route
from .avoidable import AvoidableData
from .compare import compare_route
from .interruptibility import InterruptibilityData
from .mapping import build_map_report, collect_map_bounds
from .pulls import detect_pulls
from .stats import PET_BUCKET, compute_stats
from .stealable import StealableData


def _relativize(
    entries: list[dict[str, Any]], start_ts: float, key: str = "ts", target: str = "t",
) -> None:
    for e in entries:
        if key in e and isinstance(e[key], (int, float)):
            e[target] = round(e[key] - start_ts, 1)


def _kick_value_summary(stats) -> dict[str, Any]:
    """Estimated damage/healing prevented by interrupts (see stats module)."""
    interrupted_ids = {
        ev.get("interrupted_spell_id") for ev in stats.interrupt_events
    } - {None}
    observations = []
    for spell_id in sorted(interrupted_ids):
        for kind, obs in (("damage", stats.enemy_cast_observations),
                          ("healing", stats.enemy_heal_observations)):
            entry = obs.get(spell_id)
            if entry and (entry["observed_casts"] or entry["aura_applications"]):
                observations.append({
                    "spell_id": spell_id,
                    "name": entry["name"],
                    "kind": kind,
                    "observed_casts": entry["observed_casts"],
                    "avg_per_cast": entry["avg"],
                    "avg_direct": entry["avg_direct"],
                    "avg_dot": entry["avg_dot"],
                    "debuff_applications": entry["aura_applications"],
                })
    by_player = [
        {
            "name": p.name or p.guid,
            "kicks": p.interrupts,
            "estimated_prevented_damage": p.kick_prevented_damage,
            "estimated_prevented_healing": p.kick_prevented_healing,
        }
        for p in stats.players.values()
        if p.interrupts
    ]
    by_player.sort(key=lambda e: -(e["estimated_prevented_damage"]
                                   + e["estimated_prevented_healing"]))
    return {
        "note": "estimates: average observed amount per completed cast of the "
                "interrupted spell in this run, including its periodic "
                "(DoT/HoT) component per application; spells that never "
                "landed count as 0, zero-damage debuffs are reported as "
                "prevented applications",
        "total_estimated_prevented_damage": sum(
            e["estimated_prevented_damage"] for e in by_player
        ),
        "total_estimated_prevented_healing": sum(
            e["estimated_prevented_healing"] for e in by_player
        ),
        "by_player": by_player,
        "spell_observations": observations,
    }


def _enemy_cast_summary(
    stats,
    interrupt_data: Optional[InterruptibilityData] = None,
    stealable: Optional[StealableData] = None,
) -> dict[str, Any]:
    """Kick efficiency: for every enemy hard-cast, did it get through?

    When ``interrupt_data`` (addon-captured, ground-truth ``UnitCastingInfo``/
    ``UnitChannelInfo`` observations -- see interruptibility.py) has an
    answer for a spell id, it's trusted over the old heuristic:

    - confirmed uninterruptible (``known is False``): excluded entirely --
      it can never be kicked, so it isn't a missed kick opportunity and
      shouldn't appear in the report as one.
    - confirmed interruptible (``known is True``): included, and counted
      toward efficiency regardless of whether it was actually kicked this
      run -- zero kicks against N times it got through now correctly drags
      efficiency down, instead of being invisible the way the old
      kicked-at-least-once heuristic left it.
    - no data (``known is None``): falls back to the original heuristic
      unchanged -- included, and only counted toward efficiency if it was
      kicked at least once this run. This is the byte-for-byte-identical
      path when ``interrupt_data`` is None or has nothing for a spell id.
    """
    spells = []
    kicked_total = 0
    landed_kickable = 0
    for spell_id, entry in stats.enemy_cast_outcomes.items():
        known = interrupt_data.get(spell_id) if interrupt_data else None
        if known is False:
            # confirmed genuinely uninterruptible -- never a missed kick
            continue
        spells.append({
            "spell_id": spell_id,
            "name": entry["name"],
            "kicked": entry["kicked"],
            "got_through": entry["landed"],
            "expired": entry["expired"],
            "interruptible": known,
            # Community-tagged (see stealable.py -- no live/extracted
            # source exists for this, unlike interrupt_data above), so
            # this is only ever True or False, never "unknown": a spell
            # id absent from the loaded list is just untagged, not
            # confirmed non-stealable.
            "stealable": bool(stealable and stealable.is_stealable(spell_id)),
        })
        if known is True:
            # confirmed kickable: count it whether or not it was actually
            # kicked this run
            kicked_total += entry["kicked"]
            landed_kickable += entry["landed"]
        elif entry["kicked"]:
            # no ground truth -- only spells someone kicked at least once
            # are provably kickable (today's unchanged heuristic)
            kicked_total += entry["kicked"]
            landed_kickable += entry["landed"]
    spells.sort(key=lambda s: -(s["got_through"] + s["kicked"]))
    efficiency = None
    if kicked_total + landed_kickable:
        efficiency = round(100.0 * kicked_total / (kicked_total + landed_kickable), 1)
    return {
        "note": "counts every enemy hard-cast (SPELL_CAST_START); spells "
                "confirmed uninterruptible by the addon-captured "
                "interruptibility database (--interrupt-data) are excluded "
                "entirely; efficiency covers spells confirmed interruptible "
                "by that database (whether or not they were kicked this "
                "run) plus, for spells with no addon data, ones kicked at "
                "least once this run",
        "kick_efficiency_pct": efficiency,
        "spells": spells,
    }


def _avoidable_damage_summary(stats, avoidable: AvoidableData) -> dict[str, Any]:
    """Per-player/per-spell breakdown of damage taken from spells tagged as
    avoidable ("stand in the fire" mechanics) by the loaded avoidable-
    damage file. Built from each player's full damage_taken_by_spell
    Counter (see stats._tag_avoidable_damage), not the top-15-truncated
    top_damage_taken report field, so a low-frequency but tagged spell
    isn't silently dropped.
    """
    by_player = []
    total_damage = 0
    for p in stats.players.values():
        if not p.avoidable_damage_taken:
            continue
        by_spell = sorted((
            {
                "spell_id": spell_id,
                "name": avoidable.spells.get(spell_id, {}).get("name") or spell_name,
                "amount": total,
                "hits": p.damage_taken_hits_by_spell[(spell_id, spell_name)],
            }
            for (spell_id, spell_name), total in p.damage_taken_by_spell.items()
            if spell_id in avoidable.spells
        ), key=lambda s: -s["amount"])
        by_player.append({
            "name": p.name or p.guid,
            "avoidable_damage_taken": p.avoidable_damage_taken,
            "avoidable_hits": p.avoidable_hits,
            "by_spell": by_spell,
        })
        total_damage += p.avoidable_damage_taken
    by_player.sort(key=lambda e: -e["avoidable_damage_taken"])
    return {
        "note": "damage taken from spell ids tagged as avoidable in the "
                "loaded --avoidable-data file; post-absorb amounts",
        "tagged_spell_count": len(avoidable.spells),
        "total_damage": total_damage,
        "by_player": by_player,
    }


def _cc_summary(stats) -> dict[str, Any]:
    """Hard-CC uptime landed on hostile targets (see gamedata.CC_SPELLS /
    stats.cc_events) -- distinct from interrupts (already its own summary,
    _enemy_cast_summary/_kick_value_summary): this is about control that
    was applied and held, not casts that were stopped."""
    by_player: dict[str, dict[str, Any]] = {}
    by_type: dict[str, dict[str, Any]] = {}
    for ev in stats.cc_events:
        caster = ev["caster"] or "Unknown"
        p = by_player.setdefault(caster, {"name": caster, "casts": 0, "total_duration_s": 0.0})
        p["casts"] += 1
        p["total_duration_s"] += ev["duration_s"]

        t = by_type.setdefault(ev["cc_type"], {"cc_type": ev["cc_type"], "casts": 0, "total_duration_s": 0.0})
        t["casts"] += 1
        t["total_duration_s"] += ev["duration_s"]

    for entry in by_player.values():
        entry["total_duration_s"] = round(entry["total_duration_s"], 1)
    for entry in by_type.values():
        entry["total_duration_s"] = round(entry["total_duration_s"], 1)

    players = sorted(by_player.values(), key=lambda e: -e["total_duration_s"])
    types = sorted(by_type.values(), key=lambda e: -e["total_duration_s"])
    return {
        "total_duration_s": round(sum(ev["duration_s"] for ev in stats.cc_events), 1),
        "by_player": players,
        "by_type": types,
        "events": stats.cc_events,
    }


def _unplanned_pulls_summary(comparison: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Actual pulls that included enemies not part of the pasted route --
    surfaced directly rather than requiring a reader to dig through every
    pull's matched/early/late/off_route/untracked breakdown to notice one.
    Derived entirely from compare_route()'s own output (off_route: in the
    dungeon data but never planned; untracked: not in the dungeon data at
    all, e.g. mid-fight summons) -- no new tracking, just a clearer view of
    data that already existed. None when there's no route to deviate from.
    """
    if not comparison or "pulls" not in comparison:
        return None
    unplanned = []
    total_off_route = 0
    total_untracked = 0
    for p in comparison["pulls"]:
        off_route = p.get("off_route") or []
        untracked = p.get("untracked") or []
        if not off_route and not untracked:
            continue
        total_off_route += sum(e["n"] for e in off_route)
        total_untracked += sum(e["n"] for e in untracked)
        unplanned.append({
            "actual_pull": p["actual_pull"],
            "off_route": off_route,
            "untracked": untracked,
        })
    return {
        "pulls": unplanned,
        "total_off_route_mobs": total_off_route,
        "total_untracked_mobs": total_untracked,
    }


def _timer_summary(par_ms: int, duration_ms: Optional[int]) -> dict[str, Any]:
    """+2/+3 keystone-upgrade thresholds at 80%/60% of par time -- a fixed
    WoW Mythic+ formula since the system's introduction, not season- or
    expansion-specific (see WP-C2). ``margin_ms``/``threshold`` are only
    included once the run actually finished (``duration_ms`` known from
    CHALLENGE_MODE_END) -- an abandoned run has no final time to compare
    against par, so it gets par/thresholds only, no verdict.
    """
    threshold_2_ms = round(par_ms * 0.8)
    threshold_3_ms = round(par_ms * 0.6)
    summary: dict[str, Any] = {
        "par_ms": par_ms,
        "threshold_2_ms": threshold_2_ms,
        "threshold_3_ms": threshold_3_ms,
    }
    if duration_ms is not None:
        summary["margin_ms"] = par_ms - duration_ms
        if duration_ms <= threshold_3_ms:
            threshold = 3
        elif duration_ms <= threshold_2_ms:
            threshold = 2
        elif duration_ms <= par_ms:
            threshold = 1
        else:
            threshold = 0
        summary["threshold"] = threshold
    return summary


def analyze_run(
    segment: RunSegment,
    route: Optional[Route] = None,
    store: Optional[DungeonDataStore] = None,
    avoidable: Optional[AvoidableData] = None,
    interrupt_data: Optional[InterruptibilityData] = None,
    stealable: Optional[StealableData] = None,
    pull_gap_seconds: float = 5.0,
    full_cast_timeline: bool = True,
    death_penalty_s: float = 15.0,
    par_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Analyze one M+ run; returns a JSON-ready report dict."""
    data: Optional[DungeonData] = None
    if store is not None:
        data = store.by_challenge_map_id(segment.challenge_map_id)
        if data is None and route is not None:
            data = store.by_dungeon_idx(route.dungeon_idx)

    pulls = detect_pulls(segment.events, gap_seconds=pull_gap_seconds)
    stats = compute_stats(
        segment.events, pulls, data, full_cast_timeline=full_cast_timeline,
        avoidable=avoidable,
    )

    start = segment.start_ts
    report: dict[str, Any] = {
        "run": segment.summary(),
        "dungeon": {
            "name": data.name if data else segment.zone_name,
            "dungeon_idx": data.dungeon_idx if data else None,
            "required_forces": (data.total_count.get("normal") if data else None),
        },
        "players": [
            p.summary() for p in stats.players.values()
            # The shared "Pets & Guardians" bucket only exists for pets
            # whose owner never got resolved. resolve_source() creates it
            # on the first event from any unowned pet -- including a bare
            # cast or a killing blow that then contributes nothing -- so
            # it showed up as a sixth "player" with all-zero numbers in a
            # real report (2026-09-02). Only emit it when it actually
            # holds something.
            if p.guid != PET_BUCKET or p.damage_done or p.healing_done
        ],
        "pulls": stats.pull_stats,
        "deaths": [
            {
                "ts": d.ts,
                "player": d.player_name,
                "pull": d.pull_index,
                "killing_blow": d.killing_blow,
                "biggest_hit": max(
                    (r["amount"] for r in d.recap), default=None
                ),
                "damage_last_5s": sum(
                    r["amount"] for r in d.recap if r["ts"] >= d.ts - 5.0
                ),
                "defensives_used_before_death": d.defensives_used_before_death,
                "died_without_defensive": d.died_without_defensive,
                "recap": d.recap,
            }
            for d in stats.deaths
        ],
        "death_cost": {
            "deaths": len(stats.deaths),
            "per_death_s": death_penalty_s,
            "total_s": round(len(stats.deaths) * death_penalty_s, 1),
        },
        "encounters": [dict(e) for e in stats.encounters],
        "enemy_casts": _enemy_cast_summary(stats, interrupt_data, stealable),
        "consumables": stats.consumable_events,
        "interrupts": stats.interrupt_events,
        "dispels": stats.dispel_events,
        "lust": stats.lust_events,
        "brez": stats.brez_events,
        "forces": {
            "killed": stats.forces_total,
            "required": data.total_count.get("normal") if data else None,
            "pct": (
                round(100.0 * stats.forces_total / data.total_count["normal"], 1)
                if data and data.total_count.get("normal")
                else None
            ),
            "timeline": stats.forces_timeline,
        },
        "downtime": {
            "total_s": round(stats.total_downtime_s, 1),
            "combat_s": round(stats.total_combat_s, 1),
            "windows": stats.downtime,
        },
        "enemy_damage": [
            {"name": name, "damage_to_group": dmg}
            for name, dmg in stats.enemy_damage_taken.most_common(20)
        ],
        "kick_value": _kick_value_summary(stats),
        "cc": _cc_summary(stats),
        "close_calls": stats.close_calls,
        "cast_timeline": stats.cast_timeline,
        # per-player position samples [t, x, y] from advanced logging —
        # groundwork for map overlays; empty without advanced combat logging
        "positions": {
            (stats.players[g].name or g): samples
            for g, samples in stats.position_samples.items()
            if g in stats.players
        },
    }
    for player in report["players"]:
        uptimes = stats.buff_uptimes.get(player["guid"])
        if uptimes:
            player["buff_uptimes"] = uptimes

    if avoidable is not None:
        report["avoidable_damage"] = _avoidable_damage_summary(stats, avoidable)

    if par_ms is not None:
        report["timer"] = _timer_summary(par_ms, segment.duration_ms)

    if route is not None:
        report["route"] = route.summary(data)
        if data is not None:
            comparison = compare_route(route, pulls, data)
            report["comparison"] = comparison.summary(data)
        else:
            report["comparison"] = {
                "error": "no dungeon data for this dungeon — run "
                         "`postmortem extract-data` and pass --dungeon-data "
                         "to resolve planned pulls to NPCs"
            }
        unplanned = _unplanned_pulls_summary(report.get("comparison"))
        if unplanned is not None:
            report["unplanned_pulls"] = unplanned

    if data is not None:
        player_names = {g: (p.name or g) for g, p in stats.players.items()}
        report["map"] = build_map_report(
            data, route, report.get("comparison"), pulls,
            stats.position_samples, player_names, stats.deaths, start,
            map_bounds=collect_map_bounds(segment.events),
        )

    for key in ("pulls", "deaths", "interrupts", "dispels", "lust", "brez",
                "cast_timeline", "consumables", "close_calls"):
        _relativize(report[key], start)
    _relativize(report["encounters"], start, key="start_ts")
    _relativize(report["forces"]["timeline"], start)
    _relativize(report["downtime"]["windows"], start, key="start_ts")
    _relativize(report["cc"]["events"], start, key="start_ts", target="t_start")
    _relativize(report["cc"]["events"], start, key="end_ts", target="t_end")
    for p in report["pulls"]:
        p["t_start"] = round(p["start_ts"] - start, 1)
        p["t_end"] = round(p["end_ts"] - start, 1)

    # Rate denominators. Headline dps/hps/cpm are over *combat-active*
    # time (the sum of pull durations -- the same "active time" every
    # in-game meter and WarcraftLogs divide by), NOT wall-clock time. A
    # 25-minute key easily has 5-10% of its wall clock spent running
    # between packs; dividing by wall time made every number sit
    # consistently below the in-game meter -- a real report (2026-09-02:
    # "always 10-20k behind"). The wall-clock figures are kept alongside
    # as dps_wall/hps_wall for anyone who wants "over the whole key".
    # Falls back to wall time only when no pulls were detected at all.
    wall = segment.wall_duration
    active = stats.total_combat_s if stats.total_combat_s > 0 else wall
    report["run"]["active_duration_s"] = round(active, 1)
    if wall > 0:
        for player in report["players"]:
            healing = player["healing_done"] + player["absorbs_granted"]
            player["dps"] = round(player["damage_done"] / active, 1)
            player["hps"] = round(healing / active, 1)
            player["cpm"] = round(player["casts_total"] * 60.0 / active, 1)
            player["dps_wall"] = round(player["damage_done"] / wall, 1)
            player["hps_wall"] = round(healing / wall, 1)
    return report
