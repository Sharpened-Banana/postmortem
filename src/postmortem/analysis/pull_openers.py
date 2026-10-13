"""Who started each pull, and whether it should have been the tank.

A pull's opener is the opener of its first engaged enemy (see
pulls.UnitEngagement.opened_by): the group member who hit it first, or the
one it came at first. A pull a non-tank started is flagged unless the tank
was on it within TANK_GRACE_S anyway -- a DPS whose opener lands in the
same instant as the tank's is not pulling for them. One the tank never
touched at all is flagged too: someone else did the tank's job.
"""

from __future__ import annotations

from typing import Any, Optional

from .pulls import OPENED_BY_ENEMY, ActualPull

# How far ahead of the tank's own first touch a non-tank may open a pull
# without it counting: travel time on the tank's opener, and a DPS
# pre-casting to land with it.
TANK_GRACE_S = 0.5


def pull_openers(
    pulls: list[ActualPull], tank_guid: str, grace_s: float = TANK_GRACE_S,
) -> list[dict[str, Any]]:
    """One entry per pull with a known opener: ``pull``, ``start_ts``,
    ``boss``, ``opener`` (name), ``how`` (pulls.OPENED_BY_*),
    ``tank_lead_s`` (how long before the tank's first touch; None when the
    tank never touched the pull or opened it), ``flagged``."""
    out = []
    for pull in pulls:
        first = min(
            (u for u in pull.units if u.opened_by is not None),
            key=lambda u: u.first_ts, default=None,
        )
        if first is None:
            continue
        tank_touches = [u.first_touch[tank_guid] for u in pull.units
                        if tank_guid in u.first_touch]
        tank_first: Optional[float] = min(tank_touches) if tank_touches else None
        lead: Optional[float] = None
        if first.opener_guid == tank_guid:
            flagged = False
        elif tank_first is None:
            flagged = True
        else:
            lead = round(tank_first - first.first_ts, 1)
            flagged = tank_first - first.first_ts > grace_s
        out.append({
            "pull": pull.index,
            "start_ts": first.first_ts,
            "boss": pull.encounter_name,
            "opener": first.opener_name,
            "how": first.opened_by,
            "tank_lead_s": lead,
            "flagged": flagged,
        })
    return out


def non_tank_pulls_summary(
    entries: list[dict[str, Any]], tank_name: Optional[str],
) -> dict[str, Any]:
    """The report's ``non_tank_pulls`` section: the flagged pulls, and a
    per-player count of pulls started (hit first) and aggroed (a mob came
    at them first)."""
    flagged = [e for e in entries if e["flagged"]]
    by_player: dict[str, dict[str, Any]] = {}
    for e in flagged:
        name = e["opener"] or "?"
        row = by_player.setdefault(name, {"name": name, "pulled": 0, "aggroed": 0})
        row["aggroed" if e["how"] == OPENED_BY_ENEMY else "pulled"] += 1
    return {
        "tank": tank_name,
        "grace_s": TANK_GRACE_S,
        "pull_count": len(entries),
        "flagged": flagged,
        "by_player": sorted(by_player.values(),
                            key=lambda r: (-(r["pulled"] + r["aggroed"]), r["name"])),
    }
