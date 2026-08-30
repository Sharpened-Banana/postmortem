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


def analyze_run(
    segment: RunSegment,
    route: Optional[Route] = None,
    store: Optional[DungeonDataStore] = None,
    pull_gap_seconds: float = 5.0,
    full_cast_timeline: bool = True,
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
                "recap": d.recap,
            }
            for d in stats.deaths
        ],
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
        "cast_timeline": stats.cast_timeline,
    }

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
                "cast_timeline"):
        _relativize(report[key], start)
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
    return report
