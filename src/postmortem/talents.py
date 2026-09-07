"""Turn a run's logged talent picks into names.

``COMBATANT_INFO`` logs each pick as ``(traitNodeID, traitNodeEntryID,
rank)``. Blizzard's API publishes talent trees keyed by *node* id -- the
same ids the log uses -- so a bundled node->talent map (built by
``postmortem build-talent-data``) is enough to name most picks outright.

The exception is **choice nodes**, where one node offers two talents and
only the *entry* id says which was taken -- and entry ids appear nowhere
in Blizzard's published data (verified 2026-09-07 against the talent-tree
and character-profile endpoints). About a fifth of a real build's picks
land on such nodes.

Rather than leave those ambiguous, the run's own combat log settles them:
each option has a spell id, and a player who took an option generally
*casts* it (or applies its aura) at some point in the key. So a choice
node whose one option's spell appears in the run -- and whose other
doesn't -- is resolved to that option. Only a choice between two talents
that both stayed invisible all run stays ambiguous, and those are
reported honestly as such rather than guessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass
class TalentPick:
    node_id: int
    rank: int
    name: Optional[str] = None
    spell_id: Optional[int] = None
    #: Both options of a choice node the run couldn't tell apart.
    options: Optional[list[str]] = None

    @property
    def ambiguous(self) -> bool:
        return self.name is None and bool(self.options)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"node_id": self.node_id, "rank": self.rank}
        if self.name:
            out["name"] = self.name
        if self.spell_id:
            out["spell_id"] = self.spell_id
        if self.options:
            out["options"] = list(self.options)
        return out


class TalentData:
    """Bundled ``node id -> talent option(s)`` map. Empty when no data
    file is available, in which case every pick decodes to just its
    node id and rank -- the report simply has no talent names, exactly
    like a run analyzed without dungeon data has no NPC names."""

    def __init__(self, nodes: Optional[dict[int, dict[str, Any]]] = None):
        self.nodes = nodes or {}

    def __bool__(self) -> bool:
        return bool(self.nodes)

    @classmethod
    def load(cls, path: Optional[str | Path]) -> "TalentData":
        if not path:
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return cls()
        nodes = {}
        for node_id, entry in (payload.get("nodes") or {}).items():
            if not str(node_id).isdigit() or not isinstance(entry, dict):
                continue
            options = [
                {"name": o.get("name"), "spell_id": o.get("spell_id")}
                for o in entry.get("options") or [] if o.get("name")
            ]
            if options:
                nodes[int(node_id)] = {"options": options}
        return cls(nodes)

    def decode(
        self,
        picks: Iterable[tuple[int, int, int]],
        spells_seen: Optional[set[int]] = None,
        spell_names_seen: Optional[set[str]] = None,
    ) -> list[TalentPick]:
        """Name each ``(node, entry, rank)`` pick. ``spells_seen`` and
        ``spell_names_seen`` are the spell ids and (lower-cased) spell
        names that player was seen using in this run, used to settle
        choice nodes (see the module docstring).

        Names matter as much as ids: a talent's id in Blizzard's tree is
        the *talent's* spell, which is often not the id the resulting
        effect logs under (a real run named only 65 of 78 picks on ids
        alone), while the effect almost always logs under the talent's
        own name.
        """
        spells_seen = spells_seen or set()
        spell_names_seen = spell_names_seen or set()
        out = []
        for node_id, _entry_id, rank in picks:
            entry = self.nodes.get(node_id)
            if entry is None:
                out.append(TalentPick(node_id=node_id, rank=rank))
                continue
            options = entry["options"]
            if len(options) == 1:
                out.append(TalentPick(
                    node_id=node_id, rank=rank,
                    name=options[0]["name"], spell_id=options[0]["spell_id"],
                ))
                continue
            witnessed = [
                o for o in options
                if o.get("spell_id") in spells_seen
                or (o.get("name") or "").lower() in spell_names_seen
            ]
            if len(witnessed) == 1:
                out.append(TalentPick(
                    node_id=node_id, rank=rank,
                    name=witnessed[0]["name"], spell_id=witnessed[0]["spell_id"],
                ))
            else:
                # either neither option showed up, or (rarely) both did --
                # in both cases the run genuinely can't tell them apart
                out.append(TalentPick(
                    node_id=node_id, rank=rank,
                    options=[o["name"] for o in options],
                ))
        return out


def summarize(picks: list[TalentPick]) -> dict[str, Any]:
    """Report-ready view of one player's decoded build."""
    named = [p for p in picks if p.name]
    ambiguous = [p for p in picks if p.ambiguous]
    return {
        "picks": [p.summary() for p in picks],
        "named_count": len(named),
        "total_count": len(picks),
        "ambiguous_count": len(ambiguous),
    }
