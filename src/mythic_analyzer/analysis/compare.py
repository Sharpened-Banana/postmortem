"""Compare the MDT route (intended pulls) against the pulls actually made.

The matcher's job for each actual pull is to decide which planned pull each
of its units "belongs to" (its primary plan pull), then report anything that
deviates from that: mobs pulled early (from a later plan pull), picked up
late (leftovers from an earlier plan pull), off-route (in the dungeon data
but never planned), or untracked (not in the dungeon data at all — mid-fight
summons, boss adds).

Two candidate assignments are computed and the better one wins:

* A **greedy** pass that walks actual pulls in order and consumes mobs from
  the planned pulls, preferring the current cursor position, then later
  pulls (mobs pulled early), then earlier pulls (leftovers picked up late).
  This is fast and correct for routes executed roughly in plan order.

* A **windowed DP alignment** (see ``_dp_alignment``) that looks at the
  *whole* sequence of actual pulls against the *whole* sequence of planned
  pulls to find a globally better set of "preferred" plan pulls per actual
  pull. This catches cases the greedy pass gets wrong: e.g. the group pulls
  plan-pull B's mobs before plan-pull A's, and A/B share an NPC type, so
  greedy's per-unit, cursor-order consumption misattributes units between
  them even though a clean 1:1 pull-to-pull assignment exists.

The DP's result is only used when it strictly reduces the total deviation
count; otherwise the greedy result stands unchanged. This means an
already-clean run (zero deviations) is untouched — greedy is trivially
optimal whenever every unit already matched its immediate primary pull.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from ..mdt.dungeon_data import DungeonData
from ..mdt.route import Route
from .pulls import ActualPull

# Radius (in plan-pull positions) of the window searched around the naive
# diagonal mapping when aligning actual pulls to planned pulls. A route
# rarely has more than ~40 pulls, so this keeps the DP comfortably linear
# without ever needing to search the full N*M grid.
_DP_WINDOW = 12


@dataclass
class PullMatch:
    actual_pull: int
    primary_plan_pull: Optional[int]
    matched: dict[int, Counter]  # plan pull -> npc counter
    early: Counter = field(default_factory=Counter)      # npc -> n (from later plan pulls)
    late: Counter = field(default_factory=Counter)       # npc -> n (leftovers from earlier)
    off_route: Counter = field(default_factory=Counter)  # in dungeon data, not in plan
    untracked: Counter = field(default_factory=Counter)  # not in dungeon data (summons)
    # Fraction of this pull's matched units (excluding off-route/untracked)
    # that went to primary_plan_pull. None when nothing matched at all (a
    # pull that's purely off-route/untracked).
    match_confidence: Optional[float] = None

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
                    "match_confidence": m.match_confidence,
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

    plan_order = [idx for idx, _ in plan_counters]
    plan_counter_list = [c for _, c in plan_counters]  # position-aligned with plan_order
    plan_npcs: set[int] = set()
    for c in plan_counter_list:
        plan_npcs.update(c)
    dungeon_npcs = {e.npc_id for e in data.enemies}

    # Each actual pull's trackable npc multiset, and the total killed forces
    # (which doesn't depend on matching at all) — computed once and shared
    # between the greedy and DP matching attempts below.
    actual_counters: list[Counter] = []
    actual_forces = 0.0
    for pull in actual_pulls:
        counter: Counter = Counter()
        for unit in pull.units:
            if unit.npc_id is None:
                continue
            if count_engaged_only_if_killed and not unit.killed:
                continue
            counter[unit.npc_id] += 1
            if unit.killed:
                actual_forces += data.npc_count(unit.npc_id)
        actual_counters.append(counter)

    def run_matching(
        cursor_hints: Optional[list[int]],
    ) -> tuple[list[PullMatch], dict[int, Counter], int, int]:
        """Match actual pulls to plan pulls, consuming ``remaining`` as we
        go. ``cursor_hints[i]`` (if given) overrides the search starting
        point for actual pull ``i``; otherwise the original greedy cursor
        (earliest not-yet-fully-consumed plan position) is used."""
        remaining: dict[int, Counter] = {
            idx: Counter(c) for idx, c in zip(plan_order, plan_counter_list)
        }

        def next_open(after: int = -1) -> Optional[int]:
            for pos, idx in enumerate(plan_order):
                if pos > after and sum(remaining[idx].values()) > 0:
                    return pos
            return None

        matches: list[PullMatch] = []
        cursor = next_open()  # position in plan_order of the first unconsumed pull

        total_matched_units = 0
        total_off_route_units = 0

        for i, pull in enumerate(actual_pulls):
            counter = actual_counters[i]
            pull_cursor = cursor
            if cursor_hints is not None:
                pull_cursor = cursor_hints[i]

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
                    pos = _find_plan_pull(npc_id, remaining, plan_order, pull_cursor)
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
                at_primary = sum(1 for p in matched_positions if p == primary_pos)
                match.match_confidence = round(at_primary / len(matched_positions), 3)
                for (npc_id, pos), n2 in per_unit_pos.items():
                    if pos > primary_pos:
                        match.early[npc_id] += n2
                    elif pos < primary_pos:
                        match.late[npc_id] += n2
                total_matched_units += len(matched_positions)
            total_off_route_units += sum(match.off_route.values())
            matches.append(match)
            cursor = next_open()

        missed = {
            idx: Counter(c) for idx, c in remaining.items() if sum(c.values()) > 0
        }
        return matches, missed, total_matched_units, total_off_route_units

    matches, missed, total_matched_units, total_off_route_units = run_matching(None)
    best_deviation = sum(m.deviation_count for m in matches)

    dp_hints = _dp_alignment(actual_counters, plan_counter_list, plan_npcs)
    if dp_hints is not None:
        dp_matches, dp_missed, dp_matched_units, dp_off_route_units = run_matching(dp_hints)
        dp_deviation = sum(m.deviation_count for m in dp_matches)
        if dp_deviation < best_deviation:
            matches, missed, total_matched_units, total_off_route_units = (
                dp_matches, dp_missed, dp_matched_units, dp_off_route_units,
            )

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


def _dp_alignment(
    actual_counters: list[Counter],
    plan_counter_list: list[Counter],
    plan_npcs: set[int],
    window: int = _DP_WINDOW,
) -> Optional[list[int]]:
    """Find a good (not necessarily perfectly optimal) assignment of each
    actual pull to a preferred plan-pull *position* (an index into
    ``plan_counter_list`` / ``plan_order``), used as a cursor hint for the
    real consumption-aware matcher in ``run_matching``.

    The cost of aligning actual pull ``i`` to plan pull ``j`` is the number
    of pull ``i``'s trackable units (npc ids that appear *somewhere* in the
    plan) that exceed plan pull ``j``'s original multiset for that npc —
    i.e. units that couldn't possibly all belong to ``j``. This is a static
    (order-independent) approximation: the DP picks *candidate* pull-level
    assignments this way, while the actual per-unit consumption bookkeeping
    (which decides what's really matched/early/late/missed, respecting
    quantities as pulls are processed in order) is still done by the exact
    same code path the greedy assignment uses.

    This is a banded (windowed) sequence-alignment DP, in the spirit of
    edit distance / Needleman-Wunsch: positions normally advance
    monotonically as actual pulls advance (matching plan order), but one
    extra transition — swapping two *adjacent* actual pulls' assignments —
    is allowed, which is exactly the shape of the most common real-world
    deviation this is meant to catch (the group does pull B's pack before
    pull A's). Only a window of candidate positions around the naive
    diagonal is searched per actual pull, keeping this roughly linear in
    pull count rather than O(n*m); for the pull counts a route can realistically
    have (rarely more than ~40) a full O(n*m) search would also have been
    fine, but the window costs nothing and scales better for pathological
    inputs.

    Returns ``None`` when there's nothing to align (no actual pulls or no
    planned pulls), or if the banded search failed to reach a full
    assignment (shouldn't normally happen — the caller falls back to greedy
    either way).
    """
    n = len(actual_counters)
    m = len(plan_counter_list)
    if n == 0 or m == 0:
        return None

    def cost(i: int, j: int) -> int:
        actual = actual_counters[i]
        plan = plan_counter_list[j]
        total = 0
        for npc_id, cnt in actual.items():
            if npc_id not in plan_npcs:
                continue  # can't belong to any plan pull; irrelevant to j
            total += max(0, cnt - plan.get(npc_id, 0))
        return total

    def window_for(i: int) -> range:
        center = round(i * (m - 1) / (n - 1)) if n > 1 else 0
        lo = max(0, center - window)
        hi = min(m - 1, center + window)
        return range(lo, hi + 1)

    # rows[i][j] = (total_cost, kind, prev_j)
    #   kind == "start"  -> first row, no predecessor
    #   kind == "normal" -> predecessor is rows[i-1][prev_j], prev_j <= j
    #   kind == "swap"   -> pulls (i-1, i) swap their natural relative order:
    #                       i takes j, i-1 (retroactively) takes j+1; the
    #                       real predecessor is rows[i-2][prev_j]
    Cell = tuple[int, str, Optional[int]]
    rows: list[dict[int, Cell]] = []

    row0: dict[int, Cell] = {j: (cost(0, j), "start", None) for j in window_for(0)}
    if not row0:
        return None
    rows.append(row0)

    for i in range(1, n):
        prev_row = rows[i - 1]
        prev2_row: dict[int, Cell] = rows[i - 2] if i >= 2 else {-1: (0, "start", None)}
        prev_sorted = sorted(prev_row.items())
        prev2_sorted = sorted(prev2_row.items())

        cur: dict[int, Cell] = {}
        for j in window_for(i):
            c_ij = cost(i, j)
            best_cost: Optional[int] = None
            best_choice: Optional[tuple[str, Optional[int]]] = None

            best_prev = None
            for jp, (pc, _, _) in prev_sorted:
                if jp > j:
                    break
                if best_prev is None or pc < best_prev[0]:
                    best_prev = (pc, jp)
            if best_prev is not None:
                total = c_ij + best_prev[0]
                if best_cost is None or total < best_cost:
                    best_cost, best_choice = total, ("normal", best_prev[1])

            if j + 1 < m:
                partner_cost = cost(i - 1, j + 1)
                best_prev2 = None
                for jp, (pc, _, _) in prev2_sorted:
                    if jp > j + 1:
                        break
                    if best_prev2 is None or pc < best_prev2[0]:
                        best_prev2 = (pc, jp)
                if best_prev2 is not None:
                    total = c_ij + partner_cost + best_prev2[0]
                    if best_cost is None or total < best_cost:
                        best_cost, best_choice = total, ("swap", best_prev2[1])

            if best_choice is not None:
                cur[j] = (best_cost, best_choice[0], best_choice[1])

        if not cur:
            return None  # window too tight to reach anything; bail to greedy
        rows.append(cur)

    last_row = rows[-1]
    best_j = min(last_row, key=lambda j: last_row[j][0])

    assign: list[Optional[int]] = [None] * n
    i = n - 1
    j: Optional[int] = best_j
    while i >= 0 and j is not None:
        _cost_here, kind, prev_j = rows[i][j]
        if kind == "swap":
            assign[i] = j
            assign[i - 1] = j + 1
            i -= 2
            j = prev_j
        else:  # "normal" or "start"
            assign[i] = j
            i -= 1
            j = prev_j

    if any(a is None for a in assign):
        return None
    return assign  # type: ignore[return-value]
