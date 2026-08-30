"""Tie everything together: one M+ run in, one report structure out."""

from __future__ import annotations

from typing import Any, Optional

from ..combatlog.segmenter import RunSegment
from ..mdt.dungeon_data import DungeonData, DungeonDataStore
from ..mdt.route import Route
from .compare import compare_route
from .pulls import detect_pulls
from .stats import compute_stats


def _relativize(entries: list[dict[str, Any]], start_ts: float, key: str = "ts") -> None:
    for e in entries:
        if key in e and isinstance(e[key], (int, float)):
            e["t"] = round(e[key] - start_ts, 1)


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


def _enemy_cast_summary(stats) -> dict[str, Any]:
    """Kick efficiency: for every enemy hard-cast, did it get through?"""
    spells = []
    kicked_total = 0
    landed_kickable = 0
    for spell_id, entry in stats.enemy_cast_outcomes.items():
        spells.append({
            "spell_id": spell_id,
            "name": entry["name"],
            "kicked": entry["kicked"],
            "got_through": entry["landed"],
            "expired": entry["expired"],
        })
        if entry["kicked"]:
            # only spells someone kicked at least once are provably kickable
            kicked_total += entry["kicked"]
            landed_kickable += entry["landed"]
    spells.sort(key=lambda s: -(s["got_through"] + s["kicked"]))
    efficiency = None
    if kicked_total + landed_kickable:
        efficiency = round(100.0 * kicked_total / (kicked_total + landed_kickable), 1)
    return {
        "note": "counts every enemy hard-cast (SPELL_CAST_START); efficiency "
                "covers spells that were kicked at least once this run",
        "kick_efficiency_pct": efficiency,
        "spells": spells,
    }


def analyze_run(
    segment: RunSegment,
    route: Optional[Route] = None,
    store: Optional[DungeonDataStore] = None,
    pull_gap_seconds: float = 5.0,
    full_cast_timeline: bool = True,
    death_penalty_s: float = 15.0,
) -> dict[str, Any]:
    """Analyze one M+ run; returns a JSON-ready report dict."""
    data: Optional[DungeonData] = None
    if store is not None:
        data = store.by_challenge_map_id(segment.challenge_map_id)
        if data is None and route is not None:
            data = store.by_dungeon_idx(route.dungeon_idx)

    pulls = detect_pulls(segment.events, gap_seconds=pull_gap_seconds)
    stats = compute_stats(
        segment.events, pulls, data, full_cast_timeline=full_cast_timeline
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
        "enemy_casts": _enemy_cast_summary(stats),
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

    if route is not None:
        report["route"] = route.summary(data)
        if data is not None:
            comparison = compare_route(route, pulls, data)
            report["comparison"] = comparison.summary(data)
        else:
            report["comparison"] = {
                "error": "no dungeon data for this dungeon — run "
                         "`mythic-analyzer extract-data` and pass --dungeon-data "
                         "to resolve planned pulls to NPCs"
            }

    for key in ("pulls", "deaths", "interrupts", "dispels", "lust", "brez",
                "cast_timeline", "consumables"):
        _relativize(report[key], start)
    _relativize(report["encounters"], start, key="start_ts")
    _relativize(report["forces"]["timeline"], start)
    _relativize(report["downtime"]["windows"], start, key="start_ts")
    for p in report["pulls"]:
        p["t_start"] = round(p["start_ts"] - start, 1)
        p["t_end"] = round(p["end_ts"] - start, 1)

    wall = segment.wall_duration
    if wall > 0:
        for player in report["players"]:
            player["dps"] = round(player["damage_done"] / wall, 1)
            player["hps"] = round(
                (player["healing_done"] + player["absorbs_granted"]) / wall, 1
            )
            player["cpm"] = round(player["casts_total"] * 60.0 / wall, 1)
    return report
