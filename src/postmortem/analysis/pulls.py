"""Detect the pulls you actually made from the combat log.

A hostile NPC "engagement" starts at its first combat interaction with the
group and ends at its death (or its last activity, for mobs that reset or
were left behind). Engagements whose combat windows overlap — or follow
within a small gap while combat continues — are grouped into one pull.
Boss pulls are labeled from ENCOUNTER_START/END.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..combatlog.events import (
    Event,
    is_group_owned,
    is_group_player,
    is_hostile_npc,
    advanced_info,
)
from ..combatlog.guid import parse_guid

# Events that constitute "interaction" between group and enemy.
_INTERACTION_SUBSTRINGS = ("_DAMAGE", "_MISSED", "_INTERRUPT", "_DISPEL",
                           "_AURA_APPLIED", "_AURA_REFRESH", "_CAST_SUCCESS",
                           "_HEAL", "_ENERGIZE", "_LEECH", "_DRAIN")


@dataclass
class UnitEngagement:
    guid: str
    npc_id: Optional[int]
    name: str
    first_ts: float
    last_ts: float
    died_at: Optional[float] = None
    first_pos: Optional[tuple[float, float]] = None
    # The uiMapID the advanced block reported alongside first_pos -- which
    # of the dungeon's Blizzard sub-maps that world position lives on. The
    # route map's calibration is per sub-map (see mapping.calibrate_maps),
    # so a position without its map is not usable as an anchor.
    first_map_id: Optional[int] = None

    @property
    def killed(self) -> bool:
        return self.died_at is not None

    @property
    def end_ts(self) -> float:
        return self.died_at if self.died_at is not None else self.last_ts


@dataclass
class ActualPull:
    index: int
    units: list[UnitEngagement] = field(default_factory=list)
    encounter_name: Optional[str] = None
    encounter_id: Optional[int] = None

    @property
    def start_ts(self) -> float:
        return min(u.first_ts for u in self.units)

    @property
    def end_ts(self) -> float:
        return max(u.end_ts for u in self.units)

    @property
    def duration(self) -> float:
        return self.end_ts - self.start_ts

    @property
    def is_boss(self) -> bool:
        return self.encounter_name is not None

    def npc_counter(self) -> dict[int, int]:
        counter: dict[int, int] = {}
        for u in self.units:
            if u.npc_id is not None:
                counter[u.npc_id] = counter.get(u.npc_id, 0) + 1
        return counter

    def summary(self) -> dict[str, Any]:
        return {
            "pull": self.index,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_s": round(self.duration, 1),
            "boss": self.encounter_name,
            "mob_count": len(self.units),
            "killed": sum(1 for u in self.units if u.killed),
        }


def _is_interaction(name: str) -> bool:
    if name.startswith("SWING_"):
        return True
    return any(s in name for s in _INTERACTION_SUBSTRINGS)


def collect_engagements(events: Iterable[Event]) -> dict[str, UnitEngagement]:
    """Map of enemy GUID -> engagement window."""
    engagements: dict[str, UnitEngagement] = {}
    for event in events:
        name = event.name
        if name == "UNIT_DIED":
            eng = engagements.get(event.dest_guid)
            if eng is not None and eng.died_at is None:
                eng.died_at = event.ts
            continue
        if len(event.params) < 8 or not _is_interaction(name):
            continue

        src_flags = event.source_flags
        dst_flags = event.dest_flags
        enemy_guid: Optional[str] = None
        enemy_name = ""
        enemy_flags = 0
        if is_group_owned(src_flags) and is_hostile_npc(dst_flags):
            enemy_guid, enemy_name, enemy_flags = event.dest_guid, event.dest_name, dst_flags
        elif is_hostile_npc(src_flags) and (
            is_group_player(dst_flags) or is_group_owned(dst_flags)
        ):
            enemy_guid, enemy_name, enemy_flags = event.source_guid, event.source_name, src_flags
        if enemy_guid is None:
            continue
        g = parse_guid(enemy_guid)
        if not g.is_npc:
            continue

        # The advanced block describes ONE unit per line -- the source for
        # most events -- so an enemy's own position is only on lines where
        # it is that unit (its own casts/swings), not on the player's hit
        # that usually opens an engagement. Taking the position only from
        # the very first interaction left most units (including bosses,
        # which are the best calibration anchors there are) with no
        # position at all -- 6 of 8 Ruby Life Pools bosses had first_pos=None
        # in a real run (2026-09-03). Keep the FIRST line that actually
        # carries the enemy's own position instead, whenever it arrives.
        adv = advanced_info(event)
        pos = None
        map_id = None
        if adv is not None and adv.info_guid == enemy_guid and adv.pos_x:
            pos = (adv.pos_x, adv.pos_y)
            map_id = adv.ui_map_id or None

        eng = engagements.get(enemy_guid)
        if eng is None:
            engagements[enemy_guid] = UnitEngagement(
                guid=enemy_guid,
                npc_id=g.npc_id,
                name=enemy_name,
                first_ts=event.ts,
                last_ts=event.ts,
                first_pos=pos,
                first_map_id=map_id,
            )
        else:
            if eng.first_pos is None and pos is not None:
                eng.first_pos = pos
                eng.first_map_id = map_id
            if eng.died_at is None:
                eng.last_ts = event.ts
    return engagements


def _encounter_windows(events: Iterable[Event]) -> list[tuple[float, float, int, str]]:
    windows = []
    open_enc: Optional[tuple[float, int, str]] = None
    last_ts = 0.0
    for event in events:
        last_ts = event.ts
        if event.name == "ENCOUNTER_START":
            enc_id = int(event.params[0]) if event.params and event.params[0].isdigit() else 0
            enc_name = event.params[1].strip('"') if len(event.params) > 1 else ""
            open_enc = (event.ts, enc_id, enc_name)
        elif event.name == "ENCOUNTER_END" and open_enc is not None:
            windows.append((open_enc[0], event.ts, open_enc[1], open_enc[2]))
            open_enc = None
    if open_enc is not None:
        windows.append((open_enc[0], last_ts, open_enc[1], open_enc[2]))
    return windows


def detect_pulls(
    events: list[Event],
    gap_seconds: float = 5.0,
    min_engagement_seconds: float = 0.0,
) -> list[ActualPull]:
    """Group enemy engagements into pulls.

    ``gap_seconds``: a new engagement starting within this many seconds of
    the previous pull's combat end is merged into that pull (covers chained
    packs and mid-fight adds).
    """
    engagements = collect_engagements(events)
    ordered = sorted(engagements.values(), key=lambda e: e.first_ts)
    ordered = [
        e for e in ordered
        if e.killed or (e.end_ts - e.first_ts) >= min_engagement_seconds
    ]

    pulls: list[ActualPull] = []
    current: Optional[ActualPull] = None
    current_end = 0.0
    for eng in ordered:
        if current is not None and eng.first_ts <= current_end + gap_seconds:
            current.units.append(eng)
            current_end = max(current_end, eng.end_ts)
        else:
            current = ActualPull(index=len(pulls) + 1, units=[eng])
            current_end = eng.end_ts
            pulls.append(current)

    for start, end, enc_id, enc_name in _encounter_windows(events):
        best = None
        best_overlap = 0.0
        for pull in pulls:
            overlap = min(pull.end_ts, end) - max(pull.start_ts, start)
            if overlap > best_overlap:
                best_overlap = overlap
                best = pull
        if best is not None and best.encounter_name is None:
            best.encounter_name = enc_name
            best.encounter_id = enc_id
    return pulls
