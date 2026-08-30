"""Per-run statistics: damage, healing, deaths, interrupts, casts, forces.

Everything is computed in one chronological pass over the run's events,
with pull windows (from pulls.detect_pulls) used to bucket stats per pull
and to measure downtime between pulls.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Optional

from ..combatlog.events import (
    Event,
    advanced_info,
    extra_spell_info,
    is_group_owned,
    is_group_player,
    is_hostile_npc,
    parse_absorb,
    parse_damage,
    parse_heal,
    spell_info,
    to_int,
    unquote,
)
from ..combatlog.guid import parse_guid
from ..mdt.dungeon_data import DungeonData
from .gamedata import BREZ_SPELLS, LUST_SPELLS, spec_info
from .pulls import ActualPull

PET_BUCKET = "_pets"


@dataclass
class DeathRecord:
    ts: float
    player_guid: str
    player_name: str
    pull_index: Optional[int]
    killing_blow: Optional[dict[str, Any]]
    recap: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PlayerStats:
    guid: str
    name: str = ""
    spec_id: Optional[int] = None
    damage_done: int = 0
    damage_overkill: int = 0
    damage_taken: int = 0
    healing_done: int = 0
    overhealing: int = 0
    absorbs_granted: int = 0
    interrupts: int = 0
    kick_prevented_damage: int = 0  # estimated, see RunStats.enemy_cast_observations
    kick_prevented_healing: int = 0
    dispels: int = 0
    death_count: int = 0
    casts: Counter = field(default_factory=Counter)  # (spell_id, spell_name) -> n
    damage_by_spell: Counter = field(default_factory=Counter)
    healing_by_spell: Counter = field(default_factory=Counter)
    damage_taken_by_spell: Counter = field(default_factory=Counter)
    damage_by_pull: Counter = field(default_factory=Counter)
    healing_by_pull: Counter = field(default_factory=Counter)
    damage_taken_by_pull: Counter = field(default_factory=Counter)

    def summary(self) -> dict[str, Any]:
        cls, spec, role = spec_info(self.spec_id)
        return {
            "guid": self.guid,
            "name": self.name,
            "spec_id": self.spec_id,
            "class": cls,
            "spec": spec,
            "role": role,
            "damage_done": self.damage_done,
            "damage_overkill": self.damage_overkill,
            "damage_taken": self.damage_taken,
            "healing_done": self.healing_done,
            "overhealing": self.overhealing,
            "absorbs_granted": self.absorbs_granted,
            "interrupts": self.interrupts,
            "kick_prevented_damage": self.kick_prevented_damage,
            "kick_prevented_healing": self.kick_prevented_healing,
            "dispels": self.dispels,
            "deaths": self.death_count,
            "top_damage_spells": _top(self.damage_by_spell, 15),
            "top_healing_spells": _top(self.healing_by_spell, 15),
            "top_damage_taken": _top(self.damage_taken_by_spell, 15),
            "cast_counts": _top(self.casts, 40),
            "damage_by_pull": {str(k): v for k, v in sorted(self.damage_by_pull.items())},
            "healing_by_pull": {str(k): v for k, v in sorted(self.healing_by_pull.items())},
            "damage_taken_by_pull": {str(k): v for k, v in sorted(self.damage_taken_by_pull.items())},
        }


def _top(counter: Counter, n: int) -> list[dict[str, Any]]:
    return [
        {"spell_id": sid, "name": sname, "total": total}
        for (sid, sname), total in counter.most_common(n)
    ]


@dataclass
class RunStats:
    players: dict[str, PlayerStats] = field(default_factory=dict)
    deaths: list[DeathRecord] = field(default_factory=list)
    interrupt_events: list[dict[str, Any]] = field(default_factory=list)
    dispel_events: list[dict[str, Any]] = field(default_factory=list)
    lust_events: list[dict[str, Any]] = field(default_factory=list)
    brez_events: list[dict[str, Any]] = field(default_factory=list)
    cast_timeline: list[dict[str, Any]] = field(default_factory=list)
    forces_timeline: list[dict[str, Any]] = field(default_factory=list)
    forces_total: float = 0.0
    enemy_damage_taken: Counter = field(default_factory=Counter)  # npc name -> dmg dealt to group
    # spell_id -> {name, total, instances, avg} for enemy casts that landed;
    # the basis for the "damage prevented by kicks" estimates
    enemy_cast_observations: dict[int, dict[str, Any]] = field(default_factory=dict)
    enemy_heal_observations: dict[int, dict[str, Any]] = field(default_factory=dict)
    pull_stats: list[dict[str, Any]] = field(default_factory=list)
    downtime: list[dict[str, Any]] = field(default_factory=list)
    total_downtime_s: float = 0.0
    total_combat_s: float = 0.0


class _PullLocator:
    def __init__(self, pulls: list[ActualPull], slack: float = 1.0):
        self.windows = [(p.start_ts - slack, p.end_ts + slack, p.index) for p in pulls]
        self.pos = 0

    def locate(self, ts: float) -> Optional[int]:
        while self.pos < len(self.windows) and ts > self.windows[self.pos][1]:
            self.pos += 1
        if self.pos < len(self.windows):
            start, end, idx = self.windows[self.pos]
            if start <= ts <= end:
                return idx
        return None


def compute_stats(
    events: list[Event],
    pulls: list[ActualPull],
    data: Optional[DungeonData] = None,
    full_cast_timeline: bool = True,
) -> RunStats:
    stats = RunStats()
    owner_map: dict[str, str] = {}
    recent_damage: dict[str, deque] = {}
    locator = _PullLocator(pulls)
    killed_guids: set[str] = set()
    # (source_guid, spell_id) -> ts of the last observed hit, so multi-target
    # hits within a short window count as one cast instance
    last_obs_hit: dict[tuple[str, int], float] = {}
    _AOE_WINDOW = 1.0

    def observe_enemy_cast(obs: dict[int, dict[str, Any]], src: str,
                           sp, amount: int, ts: float) -> None:
        entry = obs.setdefault(sp.spell_id, {
            "name": sp.spell_name, "total": 0, "instances": 0,
        })
        entry["total"] += amount
        key = (src, sp.spell_id)
        last = last_obs_hit.get(key)
        if last is None or ts - last > _AOE_WINDOW:
            entry["instances"] += 1
        last_obs_hit[key] = ts

    def get_player(guid: str, name: str = "") -> PlayerStats:
        p = stats.players.get(guid)
        if p is None:
            p = PlayerStats(guid=guid, name=name)
            stats.players[guid] = p
        if name and not p.name:
            p.name = name
        return p

    def resolve_source(guid: str, name: str, flags: int) -> Optional[PlayerStats]:
        """Resolve an event source to the responsible group player."""
        if guid in stats.players:
            return get_player(guid, name)
        g = parse_guid(guid)
        if g.is_player and is_group_player(flags):
            return get_player(guid, name)
        owner = owner_map.get(guid)
        if owner is not None and owner in stats.players:
            return stats.players[owner]
        if is_group_owned(flags) and (g.is_pet or g.is_npc):
            return get_player(PET_BUCKET, "Pets & Guardians")
        return None

    for event in events:
        name = event.name
        params = event.params

        if name == "COMBATANT_INFO" and params:
            guid = params[0]
            spec_id = None
            for i, p in enumerate(params):
                if p.startswith("(") and i > 1:
                    spec_id = to_int(params[i - 1]) or None
                    break
            player = get_player(guid)
            if spec_id:
                player.spec_id = spec_id
            continue

        if len(params) < 8:
            continue

        src_guid, src_name, src_flags = event.source_guid, event.source_name, event.source_flags
        dst_guid, dst_name, dst_flags = event.dest_guid, event.dest_name, event.dest_flags

        # learn pet ownership from the advanced block of source-described events
        if name == "SPELL_SUMMON" and is_group_owned(src_flags):
            owner_map[dst_guid] = src_guid
        adv = advanced_info(event)
        if adv is not None and adv.info_guid == src_guid and adv.owner_guid.startswith("Player-"):
            owner_map.setdefault(src_guid, adv.owner_guid)

        pull_idx = locator.locate(event.ts)

        if name == "UNIT_DIED":
            unconscious = params[-1] == "1" if len(params) > 8 else False
            g = parse_guid(dst_guid)
            if g.is_player and is_group_player(dst_flags):
                if unconscious:
                    continue  # feign death
                player = get_player(dst_guid, dst_name)
                player.death_count += 1
                recap = list(recent_damage.get(dst_guid, ()))
                killing = recap[-1] if recap else None
                stats.deaths.append(DeathRecord(
                    ts=event.ts,
                    player_guid=dst_guid,
                    player_name=player.name or dst_name,
                    pull_index=pull_idx,
                    killing_blow=killing,
                    recap=recap,
                ))
            elif g.is_npc and dst_guid not in killed_guids:
                killed_guids.add(dst_guid)
                if data is not None and g.npc_id is not None:
                    count = data.npc_count(g.npc_id)
                    if count > 0:
                        stats.forces_total += count
                        stats.forces_timeline.append({
                            "ts": event.ts,
                            "forces": stats.forces_total,
                            "npc_id": g.npc_id,
                            "name": data.npc_name(g.npc_id) or dst_name,
                        })
            continue

        damage = parse_damage(event)
        if damage is not None and name != "SWING_DAMAGE_LANDED":
            source_player = resolve_source(src_guid, src_name, src_flags)
            if source_player is not None and is_hostile_npc(dst_flags):
                source_player.damage_done += damage.amount
                source_player.damage_overkill += damage.overkill
                sp = spell_info(event)
                key = (sp.spell_id, sp.spell_name) if sp else (0, "Melee")
                source_player.damage_by_spell[key] += damage.amount
                if pull_idx is not None:
                    source_player.damage_by_pull[pull_idx] += damage.amount
            if name in ("SPELL_DAMAGE", "RANGE_DAMAGE") and is_hostile_npc(src_flags) \
                    and (is_group_player(dst_flags) or is_group_owned(dst_flags)):
                sp = spell_info(event)
                if sp is not None:
                    observe_enemy_cast(stats.enemy_cast_observations, src_guid,
                                       sp, damage.amount + damage.absorbed, event.ts)
            if is_group_player(dst_flags):
                target = get_player(dst_guid, dst_name)
                target.damage_taken += damage.amount
                sp = spell_info(event)
                key = (sp.spell_id, sp.spell_name) if sp else (0, "Melee")
                target.damage_taken_by_spell[key] += damage.amount
                if pull_idx is not None:
                    target.damage_taken_by_pull[pull_idx] += damage.amount
                if is_hostile_npc(src_flags):
                    stats.enemy_damage_taken[src_name or src_guid] += damage.amount
                buf = recent_damage.setdefault(dst_guid, deque(maxlen=10))
                hp_left = None
                if adv is not None and adv.info_guid == dst_guid:
                    hp_left = adv.current_hp
                buf.append({
                    "ts": event.ts,
                    "spell": (spell_info(event).spell_name if spell_info(event) else "Melee"),
                    "source": src_name or src_guid,
                    "amount": damage.amount,
                    "overkill": damage.overkill,
                    "hp_after": hp_left,
                })
            continue

        heal = parse_heal(event)
        if heal is not None:
            if is_hostile_npc(src_flags):
                sp = spell_info(event)
                if sp is not None:
                    observe_enemy_cast(stats.enemy_heal_observations, src_guid,
                                       sp, heal.amount, event.ts)
            source_player = resolve_source(src_guid, src_name, src_flags)
            if source_player is not None and (
                is_group_player(dst_flags) or is_group_owned(dst_flags)
            ):
                source_player.healing_done += heal.effective
                source_player.overhealing += heal.overhealing
                sp = spell_info(event)
                if sp:
                    source_player.healing_by_spell[(sp.spell_id, sp.spell_name)] += heal.effective
                if pull_idx is not None:
                    source_player.healing_by_pull[pull_idx] += heal.effective
            continue

        if name == "SPELL_ABSORBED":
            absorbed = parse_absorb(event)
            if absorbed is not None:
                caster_guid, caster_name, _spell, amount = absorbed
                source_player = resolve_source(caster_guid, caster_name, 0)
                if source_player is None and caster_guid.startswith("Player-"):
                    source_player = get_player(caster_guid, caster_name)
                if source_player is not None:
                    source_player.absorbs_granted += amount
            continue

        if name == "SPELL_INTERRUPT":
            source_player = resolve_source(src_guid, src_name, src_flags)
            if source_player is not None:
                source_player.interrupts += 1
                extra = extra_spell_info(event)
                stats.interrupt_events.append({
                    "ts": event.ts,
                    "pull": pull_idx,
                    "player": source_player.name or src_name,
                    "player_guid": source_player.guid,
                    "target": dst_name,
                    "interrupted_spell": extra.spell_name if extra else None,
                    "interrupted_spell_id": extra.spell_id if extra else None,
                })
            continue

        if name in ("SPELL_DISPEL", "SPELL_STOLEN"):
            source_player = resolve_source(src_guid, src_name, src_flags)
            if source_player is not None:
                source_player.dispels += 1
                extra = extra_spell_info(event)
                stats.dispel_events.append({
                    "ts": event.ts,
                    "pull": pull_idx,
                    "player": source_player.name or src_name,
                    "target": dst_name,
                    "dispelled_spell": extra.spell_name if extra else None,
                })
            continue

        if name == "SPELL_CAST_SUCCESS":
            sp = spell_info(event)
            if sp is None:
                continue
            source_player = resolve_source(src_guid, src_name, src_flags)
            if source_player is None or source_player.guid == PET_BUCKET:
                continue
            source_player.casts[(sp.spell_id, sp.spell_name)] += 1
            if sp.spell_id in BREZ_SPELLS:
                stats.brez_events.append({
                    "ts": event.ts,
                    "pull": pull_idx,
                    "player": source_player.name or src_name,
                    "spell": sp.spell_name,
                    "target": dst_name,
                })
            if full_cast_timeline:
                stats.cast_timeline.append({
                    "ts": event.ts,
                    "pull": pull_idx,
                    "player": source_player.name or src_name,
                    "spell_id": sp.spell_id,
                    "spell": sp.spell_name,
                    "target": dst_name or None,
                })
            continue

        if name == "SPELL_AURA_APPLIED":
            sp = spell_info(event)
            if sp is not None and sp.spell_id in LUST_SPELLS and is_group_player(dst_flags):
                if not any(
                    abs(l["ts"] - event.ts) < 1.0 and l["spell_id"] == sp.spell_id
                    for l in stats.lust_events
                ):
                    stats.lust_events.append({
                        "ts": event.ts,
                        "pull": pull_idx,
                        "spell_id": sp.spell_id,
                        "spell": sp.spell_name,
                        "source": src_name or None,
                    })
            continue

    _estimate_kick_value(stats)
    _finish_pull_stats(stats, pulls, data)
    return stats


def _estimate_kick_value(stats: RunStats) -> None:
    """Estimate the damage/healing each kick prevented.

    Basis: the average amount per completed cast of the interrupted spell,
    observed elsewhere in this same run (multi-target hits within 1 s count
    as one cast). Conservative: DoT/debuff components and casts that never
    landed in the run contribute nothing.
    """
    for obs in (stats.enemy_cast_observations, stats.enemy_heal_observations):
        for entry in obs.values():
            entry["avg"] = round(entry["total"] / entry["instances"]) \
                if entry["instances"] else 0

    for ev in stats.interrupt_events:
        spell_id = ev.get("interrupted_spell_id")
        dmg = stats.enemy_cast_observations.get(spell_id) if spell_id else None
        heal = stats.enemy_heal_observations.get(spell_id) if spell_id else None
        ev["estimated_prevented_damage"] = dmg["avg"] if dmg else None
        ev["estimated_prevented_healing"] = heal["avg"] if heal else None
        ev["observed_casts"] = (dmg or heal or {}).get("instances", 0)
        player = stats.players.get(ev.get("player_guid", ""))
        if player is not None:
            player.kick_prevented_damage += (dmg or {}).get("avg") or 0
            player.kick_prevented_healing += (heal or {}).get("avg") or 0


def _finish_pull_stats(
    stats: RunStats, pulls: list[ActualPull], data: Optional[DungeonData]
) -> None:
    prev_end: Optional[float] = None
    for pull in pulls:
        stats.total_combat_s += pull.duration
        if prev_end is not None:
            gap = pull.start_ts - prev_end
            if gap > 0:
                stats.downtime.append({
                    "after_pull": pull.index - 1,
                    "before_pull": pull.index,
                    "start_ts": prev_end,
                    "seconds": round(gap, 1),
                })
                stats.total_downtime_s += gap
        prev_end = max(prev_end or pull.end_ts, pull.end_ts)

        forces = 0.0
        if data is not None:
            for unit in pull.units:
                if unit.killed and unit.npc_id is not None:
                    forces += data.npc_count(unit.npc_id)
        deaths_in_pull = sum(1 for d in stats.deaths if d.pull_index == pull.index)
        damage = sum(
            p.damage_by_pull.get(pull.index, 0) for p in stats.players.values()
        )
        entry = pull.summary()
        entry.update({
            "forces": forces,
            "player_deaths": deaths_in_pull,
            "group_damage": damage,
            "npcs": _pull_npcs(pull, data),
        })
        stats.pull_stats.append(entry)


def _pull_npcs(pull: ActualPull, data: Optional[DungeonData]) -> list[dict[str, Any]]:
    by_npc: dict[int, dict[str, Any]] = {}
    for unit in pull.units:
        npc_id = unit.npc_id if unit.npc_id is not None else 0
        entry = by_npc.setdefault(npc_id, {
            "npc_id": npc_id,
            "name": unit.name,
            "n": 0,
            "killed": 0,
        })
        entry["n"] += 1
        if unit.killed:
            entry["killed"] += 1
        if data is not None and unit.npc_id is not None:
            known = data.npc_name(unit.npc_id)
            if known:
                entry["name"] = known
    return sorted(by_npc.values(), key=lambda e: -e["n"])
