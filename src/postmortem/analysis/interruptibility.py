"""Real, addon-captured spell-interruptibility data.

Unlike avoidable-damage tagging (see ``avoidable.py`` -- a hand-curated
community/user-maintained file), this data is captured live in-game by the
WoW addon itself: every enemy cast the player has ever seen gets checked
against WoW's own ``UnitCastingInfo()``/``UnitChannelInfo()`` return values
and recorded, per spell id, with the game's own ground-truth interruptible
flag (see ``addon/Postmortem/InterruptDatabase.lua``) -- no guessing or
manual curation involved, just an accumulating log of what the client has
actually observed.

The addon persists this as a ``PostmortemSpellDB`` SavedVariables
table. ``postmortem extract-interrupts`` (see ``cli.py``) pulls that
table out of the addon's SavedVariables file and writes it out in the JSON
shape this module reads:

    {
      "spells": {
        "196607": {"name": "Eye Beam", "interruptible": true},
        "204331": {"name": "Runic Spike", "interruptible": false}
      }
    }

``spells`` is keyed by spell id (as a JSON string key, since JSON object
keys are always strings -- converted back to int on load). A spell id
absent from this dict was never seen/recorded by the addon; that is
distinct from a spell id that *was* seen and confirmed interruptible or
not, so ``.get()`` returns ``None`` for "no data" rather than conflating it
with either boolean outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class InterruptibilityData:
    # spell_id -> {"name": str, "interruptible": bool}
    spells: dict[int, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "InterruptibilityData":
        """Load and tolerantly parse an extract-interrupts JSON file.

        Raises OSError (missing/unreadable file), json.JSONDecodeError
        (invalid JSON) or KeyError/ValueError (missing/malformed expected
        keys) on bad input -- callers (the CLI) turn these into a clear
        SystemExit rather than letting a crash or a silent empty result
        through. Mirrors AvoidableData.load's contract exactly.
        """
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        spells: dict[int, dict[str, Any]] = {}
        for sid_str, entry in payload["spells"].items():
            sid = int(sid_str)
            spells[sid] = {
                "name": str(entry.get("name") or f"spell:{sid}"),
                "interruptible": bool(entry.get("interruptible")),
            }

        return cls(spells=spells)

    def get(self, spell_id: int) -> Optional[bool]:
        """Return the addon-observed interruptible flag for ``spell_id``.

        Returns ``None`` when the spell was never seen/recorded by the
        addon -- meaning "no data", not "known interruptible" or "known
        uninterruptible". This distinction matters to callers: it's the
        signal to fall back to a heuristic instead of trusting a (missing)
        ground-truth answer.
        """
        entry = self.spells.get(spell_id)
        if entry is None:
            return None
        return bool(entry["interruptible"])
