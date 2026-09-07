"""Parse the whole of a ``COMBATANT_INFO`` line: spec, talent picks and
equipped gear, for every player in the run -- not just whoever was
logging.

The line's shape (Midnight-era) is::

    COMBATANT_INFO, guid, faction, <22 stat fields>, specID,
        [(traitNodeID,traitNodeEntryID,rank), ...],       -- talents
        (pvpTalent1, pvpTalent2, pvpTalent3, pvpTalent4),
        [(itemID,ilvl,(enchants),(bonusIDs),(gems)), ...], -- 18 gear slots
        [interesting auras...], ...

Two things make this awkward with the plain comma-split params the rest
of the parser works on: the nested arrays are full of commas (so a
single field spans many params), and the field *count* before specID has
already changed once in the game's history -- see stats.py's own note
about spec landing at index 24 rather than the documented 23. So this
module re-joins the params and walks the string depth-aware, keying off
the bracketed blocks' positions rather than counting stat fields.

Everything here is tolerant: a line that doesn't match the expected
shape yields whatever it could read (often just the guid and spec) and
never raises. A future layout change should cost a field, not the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# COMBATANT_INFO's gear array is in INVSLOT order. Shirt and tabard carry
# no meaningful item level, so they're excluded from the average (the
# game leaves them out of "equipped item level" too).
GEAR_SLOTS = [
    "head", "neck", "shoulder", "shirt", "chest", "waist", "legs", "feet",
    "wrist", "hands", "finger1", "finger2", "trinket1", "trinket2", "back",
    "main_hand", "off_hand", "tabard",
]
_NO_ILVL_SLOTS = {"shirt", "tabard"}


@dataclass
class GearPiece:
    slot: str
    item_id: int
    item_level: int
    enchants: list[int] = field(default_factory=list)
    bonus_ids: list[int] = field(default_factory=list)
    gems: list[int] = field(default_factory=list)

    @property
    def equipped(self) -> bool:
        return self.item_id > 0

    def summary(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "item_id": self.item_id,
            "item_level": self.item_level,
            "enchants": list(self.enchants),
            "gems": list(self.gems),
        }


@dataclass
class CombatantInfo:
    guid: str
    spec_id: Optional[int] = None
    # (trait node id, trait node entry id, rank) exactly as logged
    talents: list[tuple[int, int, int]] = field(default_factory=list)
    gear: list[GearPiece] = field(default_factory=list)

    def average_item_level(self) -> Optional[float]:
        levels = [
            g.item_level for g in self.gear
            if g.equipped and g.item_level and g.slot not in _NO_ILVL_SLOTS
        ]
        return round(sum(levels) / len(levels), 1) if levels else None


def _split_top_level(text: str) -> list[str]:
    """Split on commas that aren't inside (), [] or a quoted string."""
    parts: list[str] = []
    depth = 0
    in_quotes = False
    current: list[str] = []
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append("".join(current))
                current = []
                continue
        current.append(ch)
    parts.append("".join(current))
    return parts


def _ints(text: str) -> list[int]:
    """Every integer in a flat ``(a,b,c)`` group, ignoring anything that
    isn't one (the game has written empty groups and stray values here)."""
    out = []
    for token in _split_top_level(text.strip().strip("()[]")):
        token = token.strip()
        if token.lstrip("-").isdigit():
            out.append(int(token))
    return out


def _parse_talents(block: str) -> list[tuple[int, int, int]]:
    picks = []
    for entry in _split_top_level(block.strip().strip("[]")):
        values = _ints(entry)
        if len(values) >= 3:
            picks.append((values[0], values[1], values[2]))
    return picks


def _gem_item_ids(group: str) -> list[int]:
    """Gem item ids from an item's gem group. The group is written as
    ``(gemItemID, gemItemLevel)`` pairs -- a two-gem neck logs as
    ``(240967,295,240900,295)`` -- so every other value is an item id
    (confirmed against a real log, 2026-09-07: the second of each pair
    was a constant 295 across every gem in the run, i.e. an item level,
    never a plausible item id)."""
    values = _ints(group)
    return [v for v in values[::2] if v]


def _parse_gear(block: str) -> list[GearPiece]:
    pieces = []
    for index, entry in enumerate(_split_top_level(block.strip().strip("[]"))):
        entry = entry.strip()
        if not entry.startswith("("):
            continue
        fields = _split_top_level(entry.strip("()"))
        if len(fields) < 2:
            continue
        head = _ints(",".join(fields[:2]))
        if len(head) < 2:
            continue
        groups = [f for f in fields[2:] if f.strip().startswith("(")]
        pieces.append(GearPiece(
            slot=GEAR_SLOTS[index] if index < len(GEAR_SLOTS) else f"slot{index}",
            item_id=head[0],
            item_level=head[1],
            enchants=[e for e in _ints(groups[0]) if e] if len(groups) > 0 else [],
            bonus_ids=_ints(groups[1]) if len(groups) > 1 else [],
            gems=_gem_item_ids(groups[2]) if len(groups) > 2 else [],
        ))
    return pieces


def parse_combatant_info(params: list[str]) -> Optional[CombatantInfo]:
    """Read one COMBATANT_INFO event's params (as the parser split them)
    into a CombatantInfo, or None when there isn't even a guid."""
    if not params:
        return None
    info = CombatantInfo(guid=params[0])
    joined = ",".join(params[1:])
    fields = _split_top_level(joined)

    # specID is the plain number immediately before the first grouped
    # field, whichever bracket that group uses -- the modern layout opens
    # with a "[(node,entry,rank),...]" talent block, older/synthetic ones
    # with a bare "(...)" (see the module docstring on why this is found
    # by position rather than by counting stat fields).
    groups = [i for i, f in enumerate(fields) if f.strip()[:1] in ("[", "(")]
    if not groups:
        return info
    first = groups[0]
    if first > 0:
        spec = fields[first - 1].strip()
        if spec.isdigit():
            info.spec_id = int(spec)

    # Only the modern bracketed block carries (node, entry, rank) picks;
    # a bare-paren block is the pre-Dragonflight talent list, which this
    # decoder has nothing to say about.
    brackets = [i for i in groups if fields[i].strip().startswith("[")]
    if not brackets:
        return info
    info.talents = _parse_talents(fields[brackets[0]])

    # Gear is the next bracketed block after the talents (the pvp-talent
    # group between them is parenthesised, not bracketed). A block that
    # isn't gear-shaped simply yields no pieces.
    if len(brackets) > 1:
        info.gear = _parse_gear(fields[brackets[1]])
    return info
