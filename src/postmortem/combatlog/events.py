"""Event model and structured accessors for combat log events.

The combat log is positional: every "combat" event starts with 8 base
params (source GUID/name/flags/raidFlags, dest GUID/name/flags/raidFlags),
then a per-prefix block (spell id/name/school for SPELL_*/RANGE_*), then —
with advanced combat logging enabled — a 17-value advanced block
(unit GUID, owner GUID, hp, power, position, map, facing, level), then the
per-suffix payload (_DAMAGE, _HEAL, ...).

Everything here is defensive about layout drift between game versions:
the advanced block is detected by GUID shape, and damage/heal suffixes
support both the pre- and post-10.x layouts (with/without baseAmount).
"""

from __future__ import annotations

import time as _time
from typing import NamedTuple, Optional


class Event:
    __slots__ = ("ts", "name", "params", "line_no", "utc_offset")

    def __init__(self, ts: float, name: str, params: list[str], line_no: int = 0,
                 utc_offset: Optional[str] = None):
        self.ts = ts
        self.name = name
        self.params = params
        self.line_no = line_no
        self.utc_offset = utc_offset

    def __repr__(self) -> str:
        return f"Event({self.time_str} {self.name} {self.params[:4]}...)"

    @property
    def time_str(self) -> str:
        frac = f"{self.ts % 1:.3f}"[1:]
        return _time.strftime("%H:%M:%S", _time.localtime(self.ts)) + frac

    # --- base fields ---

    @property
    def source_guid(self) -> str:
        return self.params[0] if len(self.params) > 0 else ""

    @property
    def source_name(self) -> str:
        return unquote(self.params[1]) if len(self.params) > 1 else ""

    @property
    def source_flags(self) -> int:
        return parse_flags(self.params[2]) if len(self.params) > 2 else 0

    @property
    def dest_guid(self) -> str:
        return self.params[4] if len(self.params) > 4 else ""

    @property
    def dest_name(self) -> str:
        return unquote(self.params[5]) if len(self.params) > 5 else ""

    @property
    def dest_flags(self) -> int:
        return parse_flags(self.params[6]) if len(self.params) > 6 else 0


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse_flags(value: str) -> int:
    try:
        return int(value, 0)
    except (ValueError, TypeError):
        return 0


def to_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return default


def to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


# unit flag bits (COMBATLOG_OBJECT_*)
AFFILIATION_MINE = 0x1
AFFILIATION_PARTY = 0x2
AFFILIATION_RAID = 0x4
AFFILIATION_GROUP = AFFILIATION_MINE | AFFILIATION_PARTY | AFFILIATION_RAID
REACTION_FRIENDLY = 0x10
REACTION_NEUTRAL = 0x20
REACTION_HOSTILE = 0x40
CONTROL_PLAYER = 0x100
TYPE_PLAYER = 0x400
TYPE_NPC = 0x800
TYPE_PET = 0x1000
TYPE_GUARDIAN = 0x2000


def is_group_player(flags: int) -> bool:
    return bool(flags & TYPE_PLAYER) and bool(flags & AFFILIATION_GROUP)


def is_hostile_npc(flags: int) -> bool:
    return bool(flags & (TYPE_NPC | TYPE_GUARDIAN)) and bool(flags & REACTION_HOSTILE) \
        and not (flags & CONTROL_PLAYER)


def is_group_owned(flags: int) -> bool:
    """Player, pet or guardian belonging to the group."""
    return bool(flags & AFFILIATION_GROUP) and bool(flags & CONTROL_PLAYER)


# --- event families --------------------------------------------------------

_SPELL_PREFIXES = ("SPELL_PERIODIC_", "SPELL_BUILDING_", "SPELL_", "RANGE_",
                   "DAMAGE_SHIELD", "DAMAGE_SPLIT")
_NON_COMBAT_EVENTS = frozenset({
    "COMBAT_LOG_VERSION", "ZONE_CHANGE", "MAP_CHANGE", "ENCOUNTER_START",
    "ENCOUNTER_END", "CHALLENGE_MODE_START", "CHALLENGE_MODE_END",
    "COMBATANT_INFO", "ARENA_MATCH_START", "ARENA_MATCH_END",
    "WORLD_MARKER_PLACED", "WORLD_MARKER_REMOVED", "EMOTE",
    "STAGGER_CLEAR",
})


def is_combat_event(name: str) -> bool:
    return name not in _NON_COMBAT_EVENTS


def prefix_len(name: str) -> int:
    if name.startswith("SWING_"):
        return 0
    if name.startswith("ENVIRONMENTAL_"):
        return 1
    for p in _SPELL_PREFIXES:
        if name.startswith(p):
            return 3
    return 0  # UNIT_DIED, PARTY_KILL, ...


class SpellInfo(NamedTuple):
    spell_id: int
    spell_name: str
    school: int


def spell_info(event: Event) -> Optional[SpellInfo]:
    if prefix_len(event.name) != 3 or len(event.params) < 11:
        return None
    return SpellInfo(
        to_int(event.params[8]),
        unquote(event.params[9]),
        parse_flags(event.params[10]),
    )


def extra_spell_info(event: Event) -> Optional[SpellInfo]:
    """The interrupted / dispelled / stolen spell for SPELL_INTERRUPT,
    SPELL_DISPEL, SPELL_STOLEN, SPELL_AURA_BROKEN_SPELL."""
    base = 8 + prefix_len(event.name)
    if len(event.params) < base + 3:
        return None
    return SpellInfo(
        to_int(event.params[base]),
        unquote(event.params[base + 1]),
        parse_flags(event.params[base + 2]),
    )


# --- advanced block --------------------------------------------------------

_GUID_PREFIXES = (
    "Player-", "Creature-", "Pet-", "Vehicle-", "GameObject-", "Corpse-",
)

# Was 17 -- confirmed wrong against two independent real combat-log lines
# (a SPELL_DAMAGE and a SPELL_HEAL, both from a real 2026-era client,
# BUILD_VERSION 12.1.0) captured during real in-game testing: the true
# block is 19 fields, with two extra fields (both observed as "0" in every
# sample so far, semantics unknown -- not one of the named AdvancedInfo
# fields below) inserted between powerCost and pos_x. Under the old
# ADVANCED_LEN=17, every field read *after* the advanced block -- which
# includes the damage/heal "amount" suffix field, not just pos_x/pos_y/
# ui_map_id/facing/level inside the block itself -- was shifted by 2,
# silently corrupting damage/healing totals project-wide whenever advanced
# combat logging is on (i.e. on every real M+ run recorded through this
# addon, which forces advanced logging on). See memory/
# advanced_block_parsing_bug.md for the full real-line-by-real-line
# derivation.
ADVANCED_LEN = 19


def _looks_like_guid(value: str) -> bool:
    if value.startswith(_GUID_PREFIXES):
        return True
    # the "no unit" GUID is all zeros
    return len(value) >= 16 and set(value) == {"0"}


class AdvancedInfo(NamedTuple):
    info_guid: str
    owner_guid: str
    current_hp: int
    max_hp: int
    absorb: int
    power_type: str
    current_power: int
    max_power: int
    pos_x: float
    pos_y: float
    ui_map_id: int
    facing: float
    level: int


def _advanced_offset(event: Event) -> int:
    """Index where the advanced block starts, or -1 if absent."""
    base = 8 + prefix_len(event.name)
    if len(event.params) >= base + ADVANCED_LEN and _looks_like_guid(event.params[base]):
        return base
    return -1


def advanced_info(event: Event) -> Optional[AdvancedInfo]:
    off = _advanced_offset(event)
    if off < 0:
        return None
    p = event.params
    return AdvancedInfo(
        info_guid=p[off],
        owner_guid=p[off + 1],
        current_hp=to_int(p[off + 2]),
        max_hp=to_int(p[off + 3]),
        absorb=to_int(p[off + 7]),
        power_type=p[off + 8],
        current_power=to_int(p[off + 9]),
        max_power=to_int(p[off + 10]),
        # off + 11 is powerCost; off + 12/13 are the two newly-discovered
        # fields (see ADVANCED_LEN's own comment) -- both skipped here since
        # AdvancedInfo has no field for them and their meaning is unknown.
        pos_x=to_float(p[off + 14]),
        pos_y=to_float(p[off + 15]),
        ui_map_id=to_int(p[off + 16]),
        facing=to_float(p[off + 17]),
        level=to_int(p[off + 18]),
    )


def _suffix_params(event: Event) -> list[str]:
    base = 8 + prefix_len(event.name)
    off = _advanced_offset(event)
    if off >= 0:
        base = off + ADVANCED_LEN
    return event.params[base:]


# --- damage / heal suffixes ------------------------------------------------

DAMAGE_EVENTS = frozenset({
    "SWING_DAMAGE", "RANGE_DAMAGE", "SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE",
    "SPELL_BUILDING_DAMAGE", "DAMAGE_SHIELD", "DAMAGE_SPLIT",
    "ENVIRONMENTAL_DAMAGE",
})
# SWING_DAMAGE_LANDED duplicates SWING_DAMAGE; never total it.
HEAL_EVENTS = frozenset({"SPELL_HEAL", "SPELL_PERIODIC_HEAL", "SPELL_BUILDING_HEAL"})


class Damage(NamedTuple):
    amount: int
    base_amount: int
    overkill: int
    school: int
    resisted: int
    blocked: int
    absorbed: int
    critical: bool


def parse_damage(event: Event) -> Optional[Damage]:
    if event.name not in DAMAGE_EVENTS and event.name != "SWING_DAMAGE_LANDED":
        return None
    s = _suffix_params(event)
    if len(s) < 9:
        return None
    # 10.x layout: amount, baseAmount, overkill, school, resisted, blocked,
    #              absorbed, critical, glancing, crushing, isOffHand
    # legacy:      amount, overkill, school, resisted, blocked, absorbed,
    #              critical, glancing, crushing, isOffHand
    if len(s) >= 11:
        return Damage(
            amount=to_int(s[0]),
            base_amount=to_int(s[1]),
            overkill=max(0, to_int(s[2])),
            school=parse_flags(s[3]),
            resisted=to_int(s[4]),
            blocked=to_int(s[5]),
            absorbed=to_int(s[6]),
            critical=s[7] == "1",
        )
    return Damage(
        amount=to_int(s[0]),
        base_amount=to_int(s[0]),
        overkill=max(0, to_int(s[1])),
        school=parse_flags(s[2]),
        resisted=to_int(s[3]),
        blocked=to_int(s[4]),
        absorbed=to_int(s[5]),
        critical=s[6] == "1",
    )


class Heal(NamedTuple):
    amount: int
    base_amount: int
    overhealing: int
    absorbed: int
    critical: bool

    @property
    def effective(self) -> int:
        return max(0, self.amount - self.overhealing)


def parse_heal(event: Event) -> Optional[Heal]:
    if event.name not in HEAL_EVENTS:
        return None
    s = _suffix_params(event)
    if len(s) < 4:
        return None
    if len(s) >= 5:
        return Heal(
            amount=to_int(s[0]),
            base_amount=to_int(s[1]),
            overhealing=to_int(s[2]),
            absorbed=to_int(s[3]),
            critical=s[4] == "1",
        )
    return Heal(
        amount=to_int(s[0]),
        base_amount=to_int(s[0]),
        overhealing=to_int(s[1]),
        absorbed=to_int(s[2]),
        critical=s[3] == "1",
    )


def parse_absorb(event: Event) -> Optional[tuple[str, str, int, int]]:
    """SPELL_ABSORBED -> (absorber_guid, absorber_name, absorb_spell_id, amount).

    Layout (after base 8): [spellId, spellName, school]? (present when the
    absorbed damage came from a spell), then casterGUID, casterName,
    casterFlags, casterRaidFlags, absorbSpellId, absorbSpellName,
    absorbSchool, amount [, totalAmount] [, critical]
    """
    if event.name != "SPELL_ABSORBED":
        return None
    p = event.params
    idx = 8
    if len(p) > idx and not _looks_like_guid(p[idx]):
        idx += 3  # damage spell triple
    if len(p) < idx + 8:
        return None
    guid = p[idx]
    name = unquote(p[idx + 1])
    absorb_spell = to_int(p[idx + 4])
    amount = to_int(p[idx + 7])
    return guid, name, absorb_spell, amount
