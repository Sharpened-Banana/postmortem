"""Dispel efficiency: which dispellable enemy debuffs landed on the
group, how many were actually dispelled, how fast -- and, crucially,
whether anyone in the group *could* have dispelled them.

Two inputs:

``DispelData`` -- the tagged debuffs, ``bundled_dispel_data_path()`` by
default. Built by ``postmortem build-dispel-data`` from the same
Method.gg-derived source the interrupt database comes from (its
``dispel-magic`` / ``dispel-poison`` / ``dispel-curse`` /
``dispel-disease`` rows), with ids resolved against real logs the same
way, since Method's published ids often point at an ability's damage
component rather than the debuff itself. Shape::

    {"spells": {"1294569": {"name": "Paralyzing Shots", "school": "magic",
                            "note": "magic dispel or freedom effect"}}}

``DISPEL_CAPABILITIES`` -- which class/spec can dispel which school on a
*friendly* target, from each spec's baseline kit. A missed dispel only
counts against the group when someone present could have done it; a
group with no poison dispel is reported as "no dispeller", not as 0%.
Talent-gated extras (an Evoker's Cauterizing Flame, a Warlock's Imp)
aren't assumed -- but a school anyone in the group was *seen* dispelling
in the log counts as covered regardless of this table, so an unlisted
talent still gets credit the moment it's used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

SCHOOLS: tuple[str, ...] = ("magic", "poison", "curse", "disease", "bleed")

# (class, spec) -> schools that spec can dispel from a friendly target
# with its baseline kit. A class-wide entry uses spec None.
DISPEL_CAPABILITIES: dict[tuple[str, Optional[str]], frozenset[str]] = {
    # Priest: Purify (Disc/Holy) is magic + disease; Shadow has Purify Disease
    ("Priest", "Discipline"): frozenset({"magic", "disease"}),
    ("Priest", "Holy"): frozenset({"magic", "disease"}),
    ("Priest", "Shadow"): frozenset({"disease"}),
    # Paladin: Cleanse (Holy) covers magic too; Cleanse Toxins otherwise
    ("Paladin", "Holy"): frozenset({"magic", "poison", "disease"}),
    ("Paladin", None): frozenset({"poison", "disease"}),
    # Druid: Nature's Cure (Resto) adds magic to Remove Corruption
    ("Druid", "Restoration"): frozenset({"magic", "curse", "poison"}),
    ("Druid", None): frozenset({"curse", "poison"}),
    # Shaman: Purify Spirit (Resto) adds magic to Cleanse Spirit
    ("Shaman", "Restoration"): frozenset({"magic", "curse"}),
    ("Shaman", None): frozenset({"curse"}),
    # Monk: Detox -- Mistweaver's version adds magic
    ("Monk", "Mistweaver"): frozenset({"magic", "poison", "disease"}),
    ("Monk", None): frozenset({"poison", "disease"}),
    # Evoker: Naturalize (Preservation) adds magic to Expunge
    ("Evoker", "Preservation"): frozenset({"magic", "poison"}),
    ("Evoker", None): frozenset({"poison"}),
    # Mage: Remove Curse
    ("Mage", None): frozenset({"curse"}),
    # Warrior, Rogue, Hunter, Death Knight, Demon Hunter, Warlock: no
    # baseline friendly dispel (Warlock's Imp is a pet ability and shows
    # up in the log if used -- see the "seen dispelling" rule above).
}


def capable_schools(cls: Optional[str], spec: Optional[str]) -> frozenset[str]:
    """Schools a (class, spec) can dispel from a friendly target."""
    if not cls:
        return frozenset()
    exact = DISPEL_CAPABILITIES.get((cls, spec))
    if exact is not None:
        return exact
    return DISPEL_CAPABILITIES.get((cls, None), frozenset())


@dataclass
class DispelData:
    # spell_id -> {"name": str, "school": str, "note": Optional[str]}
    spells: dict[int, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "DispelData":
        """Load and tolerantly parse a dispel-data JSON file. Raises
        OSError / json.JSONDecodeError / KeyError / ValueError on bad
        input, like AvoidableData.load; callers turn those into a clear
        CLI error. Entries with an unknown school are skipped."""
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        spells: dict[int, dict[str, Any]] = {}
        raw = payload["spells"]
        items: Iterable[tuple[Any, Any]] = (
            raw.items() if isinstance(raw, dict) else ((e.get("id"), e) for e in raw)
        )
        for key, entry in items:
            if not isinstance(entry, dict):
                continue
            sid = int(key)
            school = str(entry.get("school") or "").lower()
            if school not in SCHOOLS:
                continue
            spells[sid] = {
                "name": str(entry.get("name") or f"spell:{sid}"),
                "school": school,
                "note": entry.get("note"),
            }
        return cls(spells=spells)

    @classmethod
    def load_bundled(cls) -> Optional["DispelData"]:
        """The packaged list, or None when it was never built / is empty
        -- a zero-config default, never a requirement."""
        from ..bundled import bundled_dispel_data_path
        path = bundled_dispel_data_path()
        if not path.is_file():
            return None
        try:
            data = cls.load(path)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        return data if data.spells else None

    def school_of(self, spell_id: int) -> Optional[str]:
        entry = self.spells.get(spell_id)
        return entry["school"] if entry else None
