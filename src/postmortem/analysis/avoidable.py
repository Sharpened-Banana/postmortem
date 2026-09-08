"""Avoidable-damage tagging.

This module defines and loads a small JSON format that tags which spell
ids are "stand in the fire"-type mechanics, so per-player damage taken
from those specific spells can be broken out in the report.

Where the list comes from (2026-09-08): Blizzard's own damage meter
(patch 12.0.0) classifies spells as avoidable -- the "Avoidable Damage
Taken" meter type Details! also shows -- but that flag is not written to
the combat log, so it can't be derived from WoWCombatLog.txt. The addon
harvests it instead (``addon/Postmortem/AvoidableDatabase.lua`` reads the
meter at the end of every key into the PostmortemAvoidableDB
SavedVariables table) and ``postmortem extract-avoidable`` turns that
into this file, bundled at ``bundled_avoidable_data_path()`` so every
consumer (CLI, desktop app, Watch Live, the public site) tags avoidable
damage with zero configuration. A hand-maintained file in the same shape
still works everywhere via ``--avoidable-data``.
See ``docs/avoidable_spells.example.json`` for the schema:

    {
      "spells": [
        {"id": 123456, "name": "Dark Bolt", "note": "easily dodged"}
      ],
      "dungeons": {
        "160": [123456]
      }
    }

``spells`` is the master list of tagged spell ids. ``dungeons`` is
optional metadata mapping a dungeon_idx (string key, matching how
mdt_data.json/DungeonDataStore keys dungeons) to the subset of tagged
spell ids relevant to that dungeon -- a future consumer could use it to
filter to "avoidable abilities in *this* dungeon". This module only
loads and passes it through; scoring works off the full tagged-id set
regardless of dungeon.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class AvoidableData:
    # spell_id -> {"name": str, "note": Optional[str]}
    spells: dict[int, dict[str, Any]] = field(default_factory=dict)
    # dungeon_idx -> set of spell ids relevant to that dungeon (optional)
    dungeons: dict[int, set[int]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "AvoidableData":
        """Load and tolerantly parse an avoidable-damage JSON file.

        Raises OSError (missing/unreadable file), json.JSONDecodeError
        (invalid JSON) or KeyError/ValueError (missing/malformed expected
        keys) on bad input -- callers (the CLI) turn these into a clear
        SystemExit rather than letting a crash or a silent empty result
        through.
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

        dungeons: dict[int, set[int]] = {}
        for key, ids in (payload.get("dungeons") or {}).items():
            dungeons[int(key)] = {int(i) for i in ids}

        return cls(spells=spells, dungeons=dungeons)

    @classmethod
    def load_bundled(cls) -> Optional["AvoidableData"]:
        """The packaged avoidable-spell list, or None when it was never
        built (or is empty/unreadable) -- a zero-config default, never a
        requirement. Callers pass an explicit file first and fall back to
        this, so a bad bundled file can't mask a user's own."""
        from ..bundled import bundled_avoidable_data_path
        path = bundled_avoidable_data_path()
        if not path.is_file():
            return None
        try:
            data = cls.load(path)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        return data if data.spells else None
