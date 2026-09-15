"""Spellsteal-worthy buff tagging.

Same shape and philosophy as ``avoidable.py``: a small, community/user-
maintained JSON file, not something this project ships a real database
for. See ``docs/stealable_spells.example.json`` for the schema:

    {
      "spells": [
        {"id": 123456, "name": "Empowering Shield", "note": "big shield, steal it"}
      ]
    }

Unlike interrupt data (``interruptibility.py``), there is no equivalent
of ``mplus-interrupts`` to build this from: every "is this worth
stealing" addon investigated (Mage Nuggets, Big Debuffs) answers it
purely from WoW's own live ``UnitAura`` ``isStealable`` flag, scanned at
runtime, not from a maintained per-dungeon spell list -- and Patch
12.1.0's Secret Values changes restrict exactly that kind of blind aura
enumeration during Mythic+ the same way Patch 12.0.0 killed live
interrupt-flag reads (see interruptibility.py's own module docstring).
So there was nothing to convert; this is deliberately a manually-curated
list from the start, the same posture ``avoidable.py`` already takes and
for the same underlying reason (this project doesn't own or maintain
spell-mechanics knowledge, players do).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class StealableData:
    # spell_id -> {"name": str, "note": Optional[str]}
    spells: dict[int, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "StealableData":
        """Load and tolerantly parse a stealable-spells JSON file.

        Raises OSError (missing/unreadable file), json.JSONDecodeError
        (invalid JSON) or KeyError/ValueError (missing/malformed expected
        keys) on bad input -- callers (the CLI) turn these into a clear
        SystemExit rather than letting a crash or a silent empty result
        through. Mirrors AvoidableData.load's contract exactly.
        """
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        spells: dict[int, dict[str, Any]] = {}
        for entry in payload["spells"]:
            sid = int(entry["id"])
            spells[sid] = {
                "name": str(entry.get("name") or f"spell:{sid}"),
                "note": entry.get("note"),
            }

        return cls(spells=spells)

    @classmethod
    def load_bundled(cls) -> Optional["StealableData"]:
        """The packaged list built from public Warcraft Logs (see
        wcl_events.py), or None when it was never built / is empty -- a
        zero-config default, never a requirement. Until 2026-09-15 there
        was no such list at all (see the module docstring); now every
        buff anyone has spellstolen or purged in a public key is in it."""
        from ..bundled import bundled_stealable_data_path
        path = bundled_stealable_data_path()
        if not path.is_file():
            return None
        try:
            data = cls.load(path)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        return data if data.spells else None

    def is_stealable(self, spell_id: int) -> bool:
        return spell_id in self.spells
