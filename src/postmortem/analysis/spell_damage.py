"""Per-spell "how much does one completed cast of this do" data, bucketed
by keystone level -- the fallback for the kick-value estimate.

``stats._estimate_kick_value`` values every interrupt at the average
amount per completed cast of the interrupted spell *observed in the same
run*. That is the most faithful number available (same key level, same
week, same group) but it has one hole: a spell that was kicked every
single time it was cast never landed, so there is nothing to average and
the kick is credited at zero -- which is exactly backwards for the kicks
that mattered most. This module supplies the fallback chain for that
case:

1. **history** -- this account's own past runs. Every analyzed run folds
   its landed-cast observations into an accumulated file (see
   ``update_from_stats``), the same pattern as
   ``interrupt_learning.py``'s ``learned_interrupts.json``.
2. **community** -- a bundled file built from public Warcraft Logs
   reports across the whole key-level range by
   ``postmortem build-spell-damage`` (see ``wcl.py`` and ``cli.py``).

Both use the same JSON shape so one class reads either:

    {
      "spells": {
        "1289416": {
          "name": "Envenom",
          "levels": {
            "10": {"damage": 1834000, "healing": 0, "casts": 12},
            "12": {"damage": 2401000, "healing": 0, "casts": 5}
          }
        }
      }
    }

Totals, not averages, are stored per level so files merge by simple
addition and the average is always ``damage / casts`` over everything
seen. ``damage`` is the direct hit plus the full periodic component per
application, matching what the in-run estimate uses, so a fallback
number and an observed number are comparable.

Lookup takes the exact key level when there is data for it, otherwise
the nearest level that has any -- a plus-11's numbers are a far better
guess for a plus-12 than nothing. Mythic+ damage scales roughly
geometrically with level, so a caller could interpolate; deliberately
not done here, because a fallback that is honest about being "from a
plus-11" beats one that looks more precise than it is. The chosen
level and sample count travel with every estimate for the report to
show.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class SpellDamageData:
    #: spell_id -> {"name": str, "levels": {level:int -> {"damage", "healing", "casts"}}}
    spells: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: free-form provenance carried through to the saved file
    source: Optional[str] = None
    season: Optional[str] = None
    version: Optional[str] = None

    # -- building ---------------------------------------------------------

    def _entry(self, spell_id: int, name: str) -> dict[str, Any]:
        entry = self.spells.setdefault(spell_id, {"name": name, "levels": {}})
        if name and entry["name"].startswith("spell:"):
            entry["name"] = name
        return entry

    def add(self, spell_id: int, name: str, level: Optional[int],
            damage: int = 0, healing: int = 0, casts: int = 0) -> None:
        """Fold ``casts`` completed casts totalling ``damage``/``healing``
        into the bucket for ``level`` (``None``/unknown -> level 0)."""
        if casts <= 0:
            return
        bucket = self._entry(spell_id, name)["levels"].setdefault(
            int(level or 0), {"damage": 0, "healing": 0, "casts": 0}
        )
        bucket["damage"] += int(damage)
        bucket["healing"] += int(healing)
        bucket["casts"] += int(casts)

    def merge(self, other: "SpellDamageData") -> None:
        for spell_id, entry in other.spells.items():
            for level, b in entry.get("levels", {}).items():
                self.add(spell_id, entry.get("name") or f"spell:{spell_id}",
                         int(level), b.get("damage", 0), b.get("healing", 0),
                         b.get("casts", 0))

    # -- querying ---------------------------------------------------------

    def estimate(self, spell_id: int, level: Optional[int],
                 name: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Average damage/healing per completed cast for ``spell_id`` at
        ``level``, or the nearest level with data. ``None`` when the spell
        is unknown. Returns ``{"damage", "healing", "level", "casts"}``
        where ``level`` is the bucket actually used.

        A cast and its damage often carry different spell ids (Fel
        Missiles: channel 1216571, damage 1216570) but the same name, so
        when the id itself has nothing (or only zeros) on record and
        ``name`` is given, the best-sampled same-named spell is used
        instead -- mirroring ``stats._estimate_kick_value``."""
        found = self._estimate_by_id(spell_id, level)
        if (found is None or not (found["damage"] or found["healing"])) and name:
            key = name.casefold()
            best: Optional[dict[str, Any]] = None
            for sid, entry in self.spells.items():
                if sid == spell_id or str(entry.get("name", "")).casefold() != key:
                    continue
                alt = self._estimate_by_id(sid, level)
                if alt and (alt["damage"] or alt["healing"]) and \
                        (best is None or alt["casts"] > best["casts"]):
                    best = alt
            if best is not None:
                return best
        return found

    def _estimate_by_id(self, spell_id: int, level: Optional[int]) -> Optional[dict[str, Any]]:
        entry = self.spells.get(spell_id)
        if not entry or not entry.get("levels"):
            return None
        levels = entry["levels"]
        want = int(level or 0)
        if want in levels:
            chosen = want
        else:
            # nearest level; ties go to the lower level (never over-credit)
            chosen = min(levels, key=lambda lv: (abs(lv - want), lv))
        b = levels[chosen]
        casts = b.get("casts", 0)
        if casts <= 0:
            return None
        return {
            "damage": round(b.get("damage", 0) / casts),
            "healing": round(b.get("healing", 0) / casts),
            "level": chosen,
            "casts": casts,
        }

    # -- persistence ------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "SpellDamageData":
        """A missing or unreadable file is an empty dataset, not an error
        -- both the history file (builds up over time) and the bundled
        community file (may simply not have been built) are optional."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SpellDamageData":
        data = cls(
            source=payload.get("source"),
            season=payload.get("season"),
            version=payload.get("version"),
        )
        for sid, entry in (payload.get("spells") or {}).items():
            try:
                spell_id = int(sid)
            except (TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or f"spell:{spell_id}")
            for level, b in (entry.get("levels") or {}).items():
                try:
                    data.add(spell_id, name, int(level),
                             int(b.get("damage", 0)), int(b.get("healing", 0)),
                             int(b.get("casts", 0)))
                except (TypeError, ValueError, AttributeError):
                    continue
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "season": self.season,
            "version": self.version,
            "spells": {
                str(sid): {
                    "name": entry["name"],
                    "levels": {
                        str(lv): dict(b) for lv, b in sorted(entry["levels"].items())
                    },
                }
                for sid, entry in sorted(self.spells.items())
            },
        }

    def save(self, path: str | Path, comment: Optional[str] = None) -> None:
        payload: dict[str, Any] = {}
        if comment:
            payload["_comment"] = comment
        payload.update(self.to_dict())
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    def __bool__(self) -> bool:
        return bool(self.spells)


HISTORY_COMMENT = (
    "Learned from your own combat logs by postmortem -- see "
    "analysis/spell_damage.py. Per enemy spell and keystone level: total "
    "damage/healing over the completed casts observed, so the kick-value "
    "estimate has a number for a spell that was kicked every time this run. "
    "Safe to delete; it rebuilds as you play."
)


def observe_stats(stats: Any, level: Optional[int]) -> SpellDamageData:
    """What one analyzed run contributes: every enemy spell that landed,
    with its total damage (direct + periodic) and completed-cast count,
    read off the same observation tables ``_estimate_kick_value`` uses.
    Requires those tables to have been finalized (``avg`` etc. present),
    i.e. call after ``compute_stats``."""
    out = SpellDamageData()
    for entry_id, entry in stats.enemy_cast_observations.items():
        casts = entry.get("observed_casts", 0)
        if casts:
            out.add(entry_id, entry["name"], level,
                    damage=entry.get("direct_total", 0) + entry.get("dot_total", 0),
                    casts=casts)
    for entry_id, entry in stats.enemy_heal_observations.items():
        casts = entry.get("observed_casts", 0)
        if casts:
            out.add(entry_id, entry["name"], level,
                    healing=entry.get("direct_total", 0) + entry.get("dot_total", 0),
                    casts=casts)
    return out


def update_from_stats(stats: Any, level: Optional[int], path: str | Path) -> SpellDamageData:
    """Fold one run's landed-cast observations into the history file at
    ``path`` and return the merged result. Best-effort by contract, like
    ``interrupt_learning.update_from_events``: callers must never lose a
    report because this cache could not be written."""
    merged = SpellDamageData.load(path)
    merged.merge(observe_stats(stats, level))
    merged.save(path, comment=HISTORY_COMMENT)
    return merged
