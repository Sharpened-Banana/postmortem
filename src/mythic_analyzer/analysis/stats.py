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
from .avoidable import AvoidableData
from .gamedata import BREZ_SPELLS, DEFENSIVES, LUST_SPELLS, spec_info
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
    # personal defensive cooldowns (see gamedata.DEFENSIVES) the victim had
    # up around their death -- see _tag_death_defensives
    defensives_used_before_death: list[dict[str, Any]] = field(default_factory=list)
    # True: spec has a known defensive and none were used/active.
    # False: spec has a known defensive and at least one was used/active.
    # None: can't honestly say either way (unknown spec, a spec this
    # table doesn't cover, or cast data wasn't collected this run).
    died_without_defensive: Optional[bool] = None


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
    dispels: int = 0      # dispels on friendly targets
    purges: int = 0       # dispels/steals of enemy buffs
    death_count: int = 0
    killing_blows: int = 0
    casts_total: int = 0
    potions_used: int = 0
    healthstones_used: int = 0
    damage_to_bosses: int = 0
    distance_traveled: float = 0.0
    avoidable_damage_taken: int = 0  # see avoidable.py; 0 unless --avoidable-data used
    avoidable_hits: int = 0
    casts: Counter = field(default_factory=Counter)  # (spell_id, spell_name) -> n
    damage_by_spell: Counter = field(default_factory=Counter)
    healing_by_spell: Counter = field(default_factory=Counter)
    damage_taken_by_spell: Counter = field(default_factory=Counter)
    # (spell_id, spell_name) -> hit count, parallel to damage_taken_by_spell
    damage_taken_hits_by_spell: Counter = field(default_factory=Counter)
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
            "avoidable_damage_taken": self.avoidable_damage_taken,
            "healing_done": self.healing_done,
            "overhealing": self.overhealing,
            "absorbs_granted": self.absorbs_granted,
            "interrupts": self.interrupts,
            "kick_prevented_damage": self.kick_prevented_damage,
            "kick_prevented_healing": self.kick_prevented_healing,
            "dispels": self.dispels,
            "purges": self.purges,
            "deaths": self.death_count,
            "killing_blows": self.killing_blows,
            "casts_total": self.casts_total,
            "potions_used": self.potions_used,
            "healthstones_used": self.healthstones_used,
            "damage_to_bosses": self.damage_to_bosses,
            "distance_traveled": round(self.distance_traveled),
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
    # spell_id -> {name, kicked, landed, expired}: outcome of every enemy
    # hard-cast (SPELL_CAST_START) — the basis for kick efficiency
    enemy_cast_outcomes: dict[int, dict[str, Any]] = field(default_factory=dict)
    encounters: list[dict[str, Any]] = field(default_factory=list)
    consumable_events: list[dict[str, Any]] = field(default_factory=list)
    buff_uptimes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    position_samples: dict[str, list[list[float]]] = field(default_factory=dict)


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
    avoidable: Optional[AvoidableData] = None,
) -> RunStats:
    stats = RunStats()
    owner_map: dict[str, str] = {}
    recent_damage: dict[str, deque] = {}
    locator = _PullLocator(pulls)
    killed_guids: set[str] = set()
    # (source_guid, spell_id, kind) -> ts of the last observed hit, so
    # multi-target hits/applications within a short window count as one cast
    last_obs_hit: dict[tuple[str, int, str], float] = {}
    _AOE_WINDOW = 1.0

    def _obs_entry(obs: dict[int, dict[str, Any]], sp) -> dict[str, Any]:
        return obs.setdefault(sp.spell_id, {
            "name": sp.spell_name,
            "direct_total": 0, "direct_casts": 0,     # up-front hit / heal
            "dot_total": 0, "dot_casts": 0,           # periodic ticks / casts
            "aura_applications": 0,                   # raw per-target debuffs
        })

    def _new_cast_instance(src: str, spell_id: int, kind: str, ts: float) -> bool:
        key = (src, spell_id, kind)
        last = last_obs_hit.get(key)
        last_obs_hit[key] = ts
        return last is None or ts - last > _AOE_WINDOW

    def observe_direct(obs, src: str, sp, amount: int, ts: float) -> None:
        entry = _obs_entry(obs, sp)
        entry["direct_total"] += amount
        if _new_cast_instance(src, sp.spell_id, "direct", ts):
            entry["direct_casts"] += 1

    def observe_periodic(obs, sp, amount: int) -> None:
        _obs_entry(obs, sp)["dot_total"] += amount

    def observe_aura(obs, src: str, sp, ts: float) -> None:
        entry = _obs_entry(obs, sp)
        entry["aura_applications"] += 1
        if _new_cast_instance(src, sp.spell_id, "aura", ts):
            entry["dot_casts"] += 1

    # enemy hard-casts in flight: caster guid -> (spell_id, spell_name, ts)
    open_enemy_casts: dict[str, tuple[int, str, float]] = {}
    # buff uptime windows: (player_guid, spell_id) -> window start ts
    open_buffs: dict[tuple[str, int], float] = {}
    buff_totals: dict[tuple[str, int], dict[str, Any]] = {}
    # start/end timestamps of buff windows, but ONLY for DEFENSIVES spell
    # ids (unbounded growth is fine at this scale, but not worth it
    # globally). open_buffs/buff_totals above discard each window's actual
    # start/end once it closes, keeping only a running total -- deaths need
    # the real windows to catch a defensive cast >10s before death that
    # was still active at the moment of death.
    # (player_guid, spell_id) -> [[start, end_or_None], ...]
    defensive_windows: dict[tuple[str, int], list[list[Optional[float]]]] = {}
    last_position: dict[str, tuple[float, float, float]] = {}  # guid -> (ts, x, y)
    open_encounter: Optional[dict[str, Any]] = None
    boss_npc_ids: set[int] = (
        {e.npc_id for e in data.enemies if e.is_boss} if data is not None else set()
    )
    run_start_ts = events[0].ts if events else 0.0
    run_end_ts = events[-1].ts if events else 0.0

    def close_enemy_cast(caster_guid: str, outcome: str) -> None:
        cast = open_enemy_casts.pop(caster_guid, None)
        if cast is None:
            return
        spell_id, spell_name, _ = cast
        entry = stats.enemy_cast_outcomes.setdefault(spell_id, {
            "name": spell_name, "kicked": 0, "landed": 0, "expired": 0,
        })
        entry[outcome] += 1

    def track_movement(guid: str, ts: float, x: float, y: float) -> None:
        prev = last_position.get(guid)
        if prev is not None:
            dt = ts - prev[0]
            dist = ((x - prev[1]) ** 2 + (y - prev[2]) ** 2) ** 0.5
            # ignore teleports/graveyard runs masquerading as sprints
            if dist < 150:
                player = stats.players.get(guid)
                if player is not None:
                    player.distance_traveled += dist
            samples = stats.position_samples.setdefault(guid, [])
            if not samples or ts - samples[-1][0] >= 2.0:
                samples.append([round(ts - run_start_ts, 1), round(x, 1), round(y, 1)])
        else:
            stats.position_samples.setdefault(guid, []).append(
                [round(ts - run_start_ts, 1), round(x, 1), round(y, 1)]
            )
        last_position[guid] = (ts, x, y)

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

        if name == "ENCOUNTER_START" and params:
            open_encounter = {
                "encounter_id": to_int(params[0]),
                "name": unquote(params[1]) if len(params) > 1 else "",
                "start_ts": event.ts,
            }
            continue
        if name == "ENCOUNTER_END" and params:
            if open_encounter is not None:
                open_encounter["end_ts"] = event.ts
                open_encounter["duration_s"] = round(event.ts - open_encounter["start_ts"], 1)
                open_encounter["kill"] = len(params) > 4 and to_int(params[4]) == 1
                stats.encounters.append(open_encounter)
                open_encounter = None
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
                close_enemy_cast(dst_guid, "expired")
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

        if name == "PARTY_KILL":
            source_player = resolve_source(src_guid, src_name, src_flags)
            if source_player is not None and source_player.guid != PET_BUCKET:
                source_player.killing_blows += 1
            continue

        damage = parse_damage(event)
        if damage is not None and name != "SWING_DAMAGE_LANDED":
            source_player = resolve_source(src_guid, src_name, src_flags)
            if source_player is not None and is_hostile_npc(dst_flags):
                source_player.damage_done += damage.amount
                source_player.damage_overkill += damage.overkill
                if boss_npc_ids and parse_guid(dst_guid).npc_id in boss_npc_ids:
                    source_player.damage_to_bosses += damage.amount
                sp = spell_info(event)
                key = (sp.spell_id, sp.spell_name) if sp else (0, "Melee")
                source_player.damage_by_spell[key] += damage.amount
                if pull_idx is not None:
                    source_player.damage_by_pull[pull_idx] += damage.amount
            if is_hostile_npc(src_flags) \
                    and (is_group_player(dst_flags) or is_group_owned(dst_flags)):
                sp = spell_info(event)
                if sp is not None:
                    if name in ("SPELL_DAMAGE", "RANGE_DAMAGE"):
                        observe_direct(stats.enemy_cast_observations, src_guid,
                                       sp, damage.amount + damage.absorbed, event.ts)
                    elif name == "SPELL_PERIODIC_DAMAGE":
                        observe_periodic(stats.enemy_cast_observations, sp,
                                         damage.amount + damage.absorbed)
            if is_group_player(dst_flags):
                target = get_player(dst_guid, dst_name)
                target.damage_taken += damage.amount
                sp = spell_info(event)
                key = (sp.spell_id, sp.spell_name) if sp else (0, "Melee")
                # amount is post-absorb (what actually landed) -- see
                # combatlog.events.parse_damage; that's the right basis for
                # damage-taken totals (including avoidable-damage tagging),
                # unlike the amount+absorbed basis used for enemy-cast
                # observation above, which prices the full potential hit.
                target.damage_taken_by_spell[key] += damage.amount
                target.damage_taken_hits_by_spell[key] += 1
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
                    "spell": sp.spell_name if sp else "Melee",
                    "spell_id": sp.spell_id if sp else 0,
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
                    if name == "SPELL_PERIODIC_HEAL":
                        observe_periodic(stats.enemy_heal_observations, sp,
                                         heal.amount)
                    else:
                        observe_direct(stats.enemy_heal_observations, src_guid,
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
                close_enemy_cast(dst_guid, "kicked")
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
                purge = name == "SPELL_STOLEN" or is_hostile_npc(dst_flags)
                if purge:
                    source_player.purges += 1
                else:
                    source_player.dispels += 1
                extra = extra_spell_info(event)
                stats.dispel_events.append({
                    "ts": event.ts,
                    "pull": pull_idx,
                    "player": source_player.name or src_name,
                    "target": dst_name,
                    "kind": "purge" if purge else "dispel",
                    "dispelled_spell": extra.spell_name if extra else None,
                })
            continue

        if name == "SPELL_CAST_START":
            if is_hostile_npc(src_flags):
                sp = spell_info(event)
                if sp is not None:
                    # a new hard-cast supersedes any stale open one
                    close_enemy_cast(src_guid, "expired")
                    open_enemy_casts[src_guid] = (sp.spell_id, sp.spell_name, event.ts)
            continue

        if name == "SPELL_CAST_SUCCESS":
            sp = spell_info(event)
            if sp is None:
                continue
            if is_hostile_npc(src_flags):
                cast = open_enemy_casts.get(src_guid)
                if cast is not None and cast[0] == sp.spell_id:
                    close_enemy_cast(src_guid, "landed")
                continue
            source_player = resolve_source(src_guid, src_name, src_flags)
            if source_player is None or source_player.guid == PET_BUCKET:
                continue
            source_player.casts[(sp.spell_id, sp.spell_name)] += 1
            source_player.casts_total += 1
            if adv is not None and adv.info_guid == src_guid \
                    and src_guid.startswith("Player-") and (adv.pos_x or adv.pos_y):
                track_movement(src_guid, event.ts, adv.pos_x, adv.pos_y)
            lowered = sp.spell_name.lower()
            if "potion" in lowered:
                source_player.potions_used += 1
                stats.consumable_events.append({
                    "ts": event.ts, "pull": pull_idx, "kind": "potion",
                    "player": source_player.name or src_name, "spell": sp.spell_name,
                })
            elif "healthstone" in lowered:
                source_player.healthstones_used += 1
                stats.consumable_events.append({
                    "ts": event.ts, "pull": pull_idx, "kind": "healthstone",
                    "player": source_player.name or src_name, "spell": sp.spell_name,
                })
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
                    "player_guid": source_player.guid,
                    "spell_id": sp.spell_id,
                    "spell": sp.spell_name,
                    "target": dst_name or None,
                })
            continue

        if name == "SPELL_AURA_APPLIED":
            sp = spell_info(event)
            if sp is not None and is_hostile_npc(src_flags):
                aura_kind = event.params[11] if len(event.params) > 11 else ""
                if aura_kind == "DEBUFF" and (
                    is_group_player(dst_flags) or is_group_owned(dst_flags)
                ):
                    observe_aura(stats.enemy_cast_observations, src_guid, sp,
                                 event.ts)
                elif aura_kind == "BUFF" and is_hostile_npc(dst_flags):
                    # enemy buffing an ally (HoTs, empowerments): heal-side
                    observe_aura(stats.enemy_heal_observations, src_guid, sp,
                                 event.ts)
            if sp is not None and is_group_player(dst_flags):
                aura_kind = event.params[11] if len(event.params) > 11 else ""
                if aura_kind == "BUFF":
                    key = (dst_guid, sp.spell_id)
                    open_buffs.setdefault(key, event.ts)
                    buff_totals.setdefault(key, {
                        "name": sp.spell_name, "uptime_s": 0.0, "applications": 0,
                    })["applications"] += 1
                    if sp.spell_id in DEFENSIVES:
                        windows = defensive_windows.setdefault(key, [])
                        if not windows or windows[-1][1] is not None:
                            windows.append([event.ts, None])
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

        if name == "SPELL_AURA_REMOVED":
            sp = spell_info(event)
            if sp is not None and is_group_player(dst_flags):
                key = (dst_guid, sp.spell_id)
                start = open_buffs.pop(key, None)
                if start is not None:
                    buff_totals[key]["uptime_s"] += event.ts - start
                if sp.spell_id in DEFENSIVES:
                    windows = defensive_windows.get(key)
                    if windows and windows[-1][1] is None:
                        windows[-1][1] = event.ts
            continue

    # buffs still up when the run ends count until the last event
    for key, start in open_buffs.items():
        buff_totals[key]["uptime_s"] += run_end_ts - start
    for windows in defensive_windows.values():
        for w in windows:
            if w[1] is None:
                w[1] = run_end_ts

    duration = max(1.0, run_end_ts - run_start_ts)
    per_player: dict[str, list[dict[str, Any]]] = {}
    for (guid, spell_id), entry in buff_totals.items():
        per_player.setdefault(guid, []).append({
            "spell_id": spell_id,
            "name": entry["name"],
            "uptime_s": round(entry["uptime_s"], 1),
            "uptime_pct": round(100.0 * entry["uptime_s"] / duration, 1),
            "applications": entry["applications"],
        })
    for guid, entries in per_player.items():
        entries.sort(key=lambda e: -e["uptime_s"])
        stats.buff_uptimes[guid] = entries[:15]

    _estimate_kick_value(stats)
    _finish_pull_stats(stats, pulls, data)
    _tag_death_defensives(stats, defensive_windows, full_cast_timeline)
    if avoidable is not None:
        _tag_avoidable_damage(stats, avoidable)
    return stats


def _tag_death_defensives(
    stats: RunStats,
    defensive_windows: dict[tuple[str, int], list[list[Optional[float]]]],
    full_cast_timeline: bool,
) -> None:
    """For each death, find the victim's personal defensive cooldowns (see
    gamedata.DEFENSIVES) that were used around the time of death.

    Two sources, merged (deduped by spell id):
      - cast_timeline: any DEFENSIVES cast by the victim (matched by GUID,
        not name -- see module notes) in the 10s window before death.
      - defensive_windows: the retained start/end of DEFENSIVES-only buff
        windows (see compute_stats), so a defensive cast well before the
        10s cutoff but still active at the moment of death is still caught
        -- the naive cast_timeline window alone would miss it.

    died_without_defensive is left None -- never guessed True -- unless we
    actually know both the victim's spec AND that spec has at least one
    DEFENSIVES entry that applies to it. It's also forced to None when
    full_cast_timeline was off (cast_timeline is empty) and neither source
    found anything: with casts not being tracked, an empty result there
    proves nothing, so treating it as "no defensive used" would be a false
    claim rather than an honest "we don't know".
    """
    casts_by_player: dict[str, list[dict[str, Any]]] = {}
    for c in stats.cast_timeline:
        guid = c.get("player_guid")
        if guid:
            casts_by_player.setdefault(guid, []).append(c)

    for death in stats.deaths:
        player = stats.players.get(death.player_guid)
        spec_id = player.spec_id if player is not None else None
        applicable = spec_id is not None and any(
            spec_ids is None or spec_id in spec_ids
            for _name, spec_ids in DEFENSIVES.values()
        )
        if not applicable:
            death.died_without_defensive = None
            death.defensives_used_before_death = []
            continue

        found: dict[int, dict[str, Any]] = {}
        for cast in casts_by_player.get(death.player_guid, ()):
            spell_id = cast["spell_id"]
            entry = DEFENSIVES.get(spell_id)
            if entry is not None and death.ts - 10.0 <= cast["ts"] <= death.ts:
                found[spell_id] = {
                    "spell_id": spell_id, "name": entry[0], "ts": cast["ts"],
                }

        for (guid, spell_id), windows in defensive_windows.items():
            if guid != death.player_guid or spell_id in found:
                continue
            entry = DEFENSIVES.get(spell_id)
            if entry is None:
                continue
            for start, end in windows:
                if start is not None and end is not None and start <= death.ts <= end:
                    found[spell_id] = {
                        "spell_id": spell_id, "name": entry[0], "ts": start,
                    }
                    break

        death.defensives_used_before_death = sorted(
            found.values(), key=lambda e: e["ts"]
        )
        if not found and not full_cast_timeline:
            # cast-based detection had no data to work with (--no-cast-
            # timeline was used); the buff-window path is independent of
            # that flag and already had its chance above, so an empty
            # result here isn't good enough evidence for a "died without a
            # defensive" claim
            death.died_without_defensive = None
        else:
            death.died_without_defensive = not bool(found)


def _tag_avoidable_damage(stats: RunStats, avoidable: AvoidableData) -> None:
    """Break out each player's damage taken from spells tagged as
    avoidable ("stand in the fire" mechanics) in the loaded avoidable-
    damage file.

    Tags from the player's full damage_taken_by_spell/
    damage_taken_hits_by_spell Counters, not the top-15-truncated
    top_damage_taken report field -- otherwise a low-frequency but tagged
    spell could be silently dropped.
    """
    for player in stats.players.values():
        for key, total in player.damage_taken_by_spell.items():
            spell_id = key[0]
            if spell_id in avoidable.spells:
                player.avoidable_damage_taken += total
                player.avoidable_hits += player.damage_taken_hits_by_spell[key]


def _estimate_kick_value(stats: RunStats) -> None:
    """Estimate the damage/healing each kick prevented.

    Basis: the average amount per completed cast of the interrupted spell,
    observed elsewhere in this same run — the up-front hit plus the full
    periodic (DoT/HoT) component per application. Multi-target hits and
    debuff applications within 1 s count as one cast. Spells that never
    landed in the run still contribute nothing; a debuff that carries no
    damage at all is reported as a prevented application instead.
    """
    for obs in (stats.enemy_cast_observations, stats.enemy_heal_observations):
        for entry in obs.values():
            direct_casts = entry["direct_casts"]
            # ticks seen without any observed aura application (e.g. applied
            # before the run slice) fall back to the direct-cast count
            dot_casts = entry["dot_casts"] or direct_casts
            avg_direct = entry["direct_total"] / direct_casts if direct_casts else 0
            avg_dot = entry["dot_total"] / dot_casts if dot_casts else 0
            entry["avg_direct"] = round(avg_direct)
            entry["avg_dot"] = round(avg_dot)
            entry["avg"] = round(avg_direct + avg_dot)
            entry["observed_casts"] = max(direct_casts, entry["dot_casts"])

    for ev in stats.interrupt_events:
        spell_id = ev.get("interrupted_spell_id")
        dmg = stats.enemy_cast_observations.get(spell_id) if spell_id else None
        heal = stats.enemy_heal_observations.get(spell_id) if spell_id else None
        ev["estimated_prevented_damage"] = (dmg["avg"] or None) if dmg else None
        ev["estimated_prevented_healing"] = (heal["avg"] or None) if heal else None
        ev["prevented_dot_damage"] = dmg["avg_dot"] if dmg else 0
        ev["observed_casts"] = (dmg or heal or {}).get("observed_casts", 0)
        # a zero-damage debuff (CC, snare, heal absorb...): no number to put
        # on it, but the kick still visibly prevented an application
        ev["prevented_debuff_applications"] = (
            dmg["aura_applications"]
            if dmg and not dmg["avg"] and dmg["aura_applications"] else 0
        )
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
            "group_dps": round(damage / pull.duration) if pull.duration > 0 else 0,
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
