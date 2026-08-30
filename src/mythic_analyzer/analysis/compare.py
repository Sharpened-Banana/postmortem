"""Compare the MDT route (intended pulls) against the pulls actually made.

The matcher walks actual pulls in order and greedily consumes mobs from the
planned pulls, preferring the current position in the route, then later
pulls (mobs pulled early), then earlier pulls (leftovers picked up late).
Anything the plan never contained is flagged as off-route; mobs that are in
the dungeon data but were never engaged are missed/skipped; NPC ids absent
from the dungeon data entirely (mid-fight summons, boss adds) are listed as
untracked and excluded from deviation accounting.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from ..mdt.dungeon_data import DungeonData
from ..mdt.route import Route
from .pulls import ActualPull


@dataclass
class PullMatch:
    actual_pull: int
    primary_plan_pull: Optional[int]
    matched: dict[int, Counter]  # plan pull -> npc counter
    early: Counter = field(default_factory=Counter)      # npc -> n (from later plan pulls)
    late: Counter = field(default_factory=Counter)       # npc -> n (leftovers from earlier)
    off_route: Counter = field(default_factory=Counter)  # in dungeon data, not in plan
    untracked: Counter = field(default_factory=Counter)  # not in dungeon data (summons)

    @property
    def deviation_count(self) -> int:
        return (
            sum(self.early.values())
            + sum(self.late.values())
            + sum(self.off_route.values())
        )


@dataclass
class RouteComparison:
    matches: list[PullMatch]
    missed: dict[int, Counter]  # plan pull -> npc counter never engaged
    plan_forces: float
    actual_forces: float
    required_forces: Optional[float]
    adherence_pct: Optional[float]

    def summary(self, data: Optional[DungeonData]) -> dict[str, Any]:
        def npc_list(counter: Counter) -> list[dict[str, Any]]:
            out = []
            for npc_id, n in sorted(counter.items()):
                entry = {"npc_id": npc_id, "n": n}
                if data is not None:
                    name = data.npc_name(npc_id)
                    if name:
                        entry["name"] = name
                out.append(entry)
            return out

        return {
            "adherence_pct": self.adherence_pct,
            "plan_forces": self.plan_forces,
            "actual_forces": self.actual_forces,
            "required_forces": self.required_forces,
            "pulls": [
                {
                    "actual_pull": m.actual_pull,
                    "primary_plan_pull": m.primary_plan_pull,
                    "matched": {
                        str(plan_idx): npc_list(counter)
                        for plan_idx, counter in sorted(m.matched.items())
                    },
                    "pulled_early": npc_list(m.early),
                    "picked_up_late": npc_list(m.late),
                    "off_route": npc_list(m.off_route),
                    "untracked": npc_list(m.untracked),
                    "deviations": m.deviation_count,
                }
                for m in self.matches
            ],
            "missed": {
                str(plan_idx): npc_list(counter)
                for plan_idx, counter in sorted(self.missed.items())
            },
        }


def compare_route(
    route: Route,
    actual_pulls: list[ActualPull],
    data: DungeonData,
    count_engaged_only_if_killed: bool = False,
) -> RouteComparison:
    plan_counters: list[tuple[int, Counter]] = []
    for pull in route.pulls:
        plan_counters.append((pull.index, pull.npc_counter(data)))

    remaining: dict[int, Counter] = {idx: Counter(c) for idx, c in plan_counters}
    plan_order = [idx for idx, _ in plan_counters]
    plan_npcs: set[int] = set()
    for _, c in plan_counters:
        plan_npcs.update(c)
    dungeon_npcs = {e.npc_id for e in data.enemies}

    def next_open(after: int = -1) -> Optional[int]:
        for pos, idx in enumerate(plan_order):
            if pos > after and sum(remaining[idx].values()) > 0:
                return pos
        return None

    matches: list[PullMatch] = []
    cursor = next_open()  # position in plan_order of the first unconsumed pull

    total_matched_units = 0
    total_off_route_units = 0
    actual_forces = 0.0

    for pull in actual_pulls:
        counter = Counter()
        for unit in pull.units:
            if unit.npc_id is None:
                continue
            if count_engaged_only_if_killed and not unit.killed:
                continue
            counter[unit.npc_id] += 1
            if unit.killed:
                actual_forces += data.npc_count(unit.npc_id)

        match = PullMatch(actual_pull=pull.index, primary_plan_pull=None, matched={})
        matched_positions: list[int] = []
        per_unit_pos: Counter = Counter()  # (npc, plan position) usage

        for npc_id, n in counter.items():
            for _ in range(n):
                if npc_id not in dungeon_npcs:
                    match.untracked[npc_id] += 1
                    continue
                if npc_id not in plan_npcs:
                    match.off_route[npc_id] += 1
                    continue
                pos = _find_plan_pull(npc_id, remaining, plan_order, cursor)
                if pos is None:
                    match.off_route[npc_id] += 1
                    continue
                plan_idx = plan_order[pos]
                remaining[plan_idx][npc_id] -= 1
                if remaining[plan_idx][npc_id] <= 0:
                    del remaining[plan_idx][npc_id]
                match.matched.setdefault(plan_idx, Counter())[npc_id] += 1
                matched_positions.append(pos)
                per_unit_pos[(npc_id, pos)] += 1

        if matched_positions:
            primary_pos = Counter(matched_positions).most_common(1)[0][0]
            match.primary_plan_pull = plan_order[primary_pos]
            for (npc_id, pos), n in per_unit_pos.items():
                if pos > primary_pos:
                    match.early[npc_id] += n
                elif pos < primary_pos:
                    match.late[npc_id] += n
            total_matched_units += len(matched_positions)
        total_off_route_units += sum(match.off_route.values())
        matches.append(match)
        cursor = next_open()

    missed = {
        idx: Counter(c) for idx, c in remaining.items() if sum(c.values()) > 0
    }

    plan_forces = route.total_forces(data)
    required = data.total_count.get("normal")
    # An "on plan" unit matched its actual pull's primary planned pull; the
    # denominator is every engaged unit the route could have known about
    # (matched anywhere + off-route packs; untracked summons excluded).
    denom = total_matched_units + total_off_route_units
    adherence = None
    if denom > 0:
        on_plan = total_matched_units - sum(
            sum(m.early.values()) + sum(m.late.values()) for m in matches
        )
        adherence = round(100.0 * max(0, on_plan) / denom, 1)

    return RouteComparison(
        matches=matches,
        missed=missed,
        plan_forces=plan_forces,
        actual_forces=actual_forces,
        required_forces=required,
        adherence_pct=adherence,
    )


def _find_plan_pull(
    npc_id: int,
    remaining: dict[int, Counter],
    plan_order: list[int],
    cursor: Optional[int],
) -> Optional[int]:
    """Preferred order: current plan position, later pulls, then earlier."""
    if cursor is None:
        cursor = 0
    n = len(plan_order)
    for pos in range(cursor, n):
        if remaining[plan_order[pos]].get(npc_id, 0) > 0:
            return pos
    for pos in range(min(cursor, n) - 1, -1, -1):
        if remaining[plan_order[pos]].get(npc_id, 0) > 0:
            return pos
    return None
