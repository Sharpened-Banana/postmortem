"""Spell-interruptibility data: which enemy casts are (or aren't) kickable.

This USED to be captured live in-game by the WoW addon itself, checking
every observed enemy cast against ``UnitCastingInfo()``/
``UnitChannelInfo()``'s own ground-truth interruptible flag (see
``addon/Postmortem/InterruptDatabase.lua``). That path is now permanently
dead: Patch 12.0.0 made that flag a "secret" value addon code cannot read
into a real boolean at all (Blizzard's own "Secret Values" anti-exploit
system, confirmed 2026-09-04 -- deliberate and, per Blizzard's own planned-
API notes, not expected to get a replacement API any time soon). Old
captures can still be extracted with ``postmortem extract-interrupts`` and
loaded here -- the JSON shape below hasn't changed -- but no client on
Patch 12.0.0+ can ever add a new entry to one again.

The primary source now is ``postmortem build-interrupt-data`` (see
``cli.py``), which converts a community mechanics-guide export (schema:
albvar/mplus-interrupts, MIT -- see ``docs/THIRD_PARTY_NOTICES.md``) into
this same shape, and is what ``bundled.py``'s ``bundled_interrupt_data_path()``
ships by default so every consumer (CLI, desktop app, Watch Live, the
public site) gets it with zero configuration. One real limitation worth
knowing: that source only ever tags "this is worth interrupting" -- it
never asserts "this cannot be interrupted" the way the live client flag
used to. So spells built from it are always ``interruptible: true``;
``false`` entries, if any exist at all, can only come from an old
addon-captured extraction. See ``.get()``'s own docstring for how that
plays with the "kicked at least once" fallback heuristic.

    {
      "spells": {
        "196607": {"name": "Eye Beam", "interruptible": true},
        "204331": {"name": "Runic Spike", "interruptible": false}
      }
    }

``spells`` is keyed by spell id (as a JSON string key, since JSON object
keys are always strings -- converted back to int on load). A spell id
absent from this dict has no data either way; that is distinct from a
spell id that *is* present and confirmed interruptible or not, so
``.get()`` returns ``None`` for "no data" rather than conflating it with
either boolean outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Confirmed uninterruptible by design, not by any data source above --
# WoW's weekly Mythic+ affix ("Xal'atath's Bargain": Ascendant/Voidbound/
# Devour/Pulsar, rotating weekly) casts a signature environmental
# mechanic every key regardless of dungeon, and it is never actually
# interruptible. It still logs as an ordinary hard cast
# (SPELL_CAST_START/SUCCESS), so without an explicit exclusion it
# clutters the kick-efficiency table with something no kick could ever
# have stopped. Confirmed real, 2026-09-05: "Xal'atath's Bargain: Devour"
# (465051) showed 0 kicked / 19-20 landed in two different dungeons the
# same week, in two of the user's own real reports.
#
# Deliberately hardcoded here rather than folded into interrupt_data.json
# (bundled.py) -- these apply regardless of season/dungeon-pool and
# regardless of which interrupt_data source (or none) is loaded, so this
# check runs unconditionally in _enemy_cast_summary rather than depending
# on a file being present. Only Devour is confirmed so far; add the other
# three variants' spell ids here once observed the same way -- don't
# guess them from a wiki/guide (see interruptibility.py's own posture on
# never fabricating spell ids).
KNOWN_UNINTERRUPTIBLE_SPELL_IDS: dict[int, str] = {
    465051: "Xal'atath's Bargain: Devour",
}


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
