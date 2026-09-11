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
          "dungeons": [525],
          "levels": {
            "10": {"damage": 1834000, "healing": 0, "casts": 12,
                   "direct_damage": 1200000, "direct_casts": 12,
                   "dot_damage": 634000, "dot_casts": 8},
            "12": {"damage": 2401000, "healing": 0, "casts": 5}
          }
        }
      }
    }

Totals, not averages, are stored per level so files merge by simple
addition. ``damage`` is the direct hit plus the full periodic component,
and ``casts`` the larger of the two cast counts.

The per-component totals matter because the in-run estimate this is a
fallback for averages the two parts over their OWN cast counts and adds
the results -- a spell whose DoT was seen on 8 applications but whose
direct hit landed 12 times is priced ``direct/12 + dot/8``, not
``(direct + dot)/12``. Storing only the combined pair divided the whole
by the larger count, which on a plausible split came out more than a
factor of two below the number the module claims to be comparable with
(2026-09-11). Buckets carrying the split use it; older files, and
community data built before it existed, still average the combined pair,
which is the best their numbers support.

``dungeons`` lists the challenge-map ids a spell was seen in, and exists
only to scope the same-name borrow below.

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

    #: per-level totals that merge by addition. The first three are the
    #: original shape; the rest are the per-component split (see module
    #: docstring) and are absent from older files, which is why every
    #: reader defaults them to zero rather than requiring them.
    SPLIT_FIELDS = (
        "direct_damage", "direct_casts", "dot_damage", "dot_casts",
        "direct_healing", "direct_heal_casts", "dot_healing", "dot_heal_casts",
    )
    BUCKET_FIELDS = ("damage", "healing", "casts") + SPLIT_FIELDS

    def _entry(self, spell_id: int, name: str) -> dict[str, Any]:
        entry = self.spells.setdefault(
            spell_id, {"name": name, "levels": {}, "dungeons": []}
        )
        entry.setdefault("dungeons", [])
        if name and entry["name"].startswith("spell:"):
            entry["name"] = name
        return entry

    def add(self, spell_id: int, name: str, level: Optional[int],
            damage: int = 0, healing: int = 0, casts: int = 0,
            dungeon: Optional[int] = None, **split: int) -> None:
        """Fold ``casts`` completed casts totalling ``damage``/``healing``
        into the bucket for ``level`` (``None``/unknown -> level 0).

        ``split`` takes any of SPLIT_FIELDS -- the same totals broken into
        their direct and periodic parts with their own cast counts, which
        is what ``estimate`` prefers when it is there. ``dungeon`` is the
        challenge-map id the observation came from, recorded so the
        same-name borrow can stay inside one dungeon."""
        if casts <= 0:
            return
        entry = self._entry(spell_id, name)
        if dungeon and dungeon not in entry["dungeons"]:
            entry["dungeons"].append(int(dungeon))
        bucket = entry["levels"].setdefault(
            int(level or 0), {k: 0 for k in self.BUCKET_FIELDS}
        )
        for field_name in self.BUCKET_FIELDS:
            bucket.setdefault(field_name, 0)
        bucket["damage"] += int(damage)
        bucket["healing"] += int(healing)
        bucket["casts"] += int(casts)
        for field_name, value in split.items():
            if field_name not in self.SPLIT_FIELDS:
                raise TypeError(f"unknown bucket field {field_name!r}")
            bucket[field_name] += int(value)

    def note_dungeons(self, spell_id: int, name: str, dungeons: Any) -> None:
        """Record challenge-map ids for a spell without touching totals."""
        entry = self._entry(spell_id, name)
        for dungeon in dungeons or ():
            try:
                dungeon = int(dungeon)
            except (TypeError, ValueError):
                continue
            if dungeon and dungeon not in entry["dungeons"]:
                entry["dungeons"].append(dungeon)

    def merge(self, other: "SpellDamageData") -> None:
        for spell_id, entry in other.spells.items():
            name = entry.get("name") or f"spell:{spell_id}"
            for level, b in entry.get("levels", {}).items():
                self.add(spell_id, name, int(level), b.get("damage", 0),
                         b.get("healing", 0), b.get("casts", 0),
                         **{k: b.get(k, 0) for k in self.SPLIT_FIELDS})
            self.note_dungeons(spell_id, name, entry.get("dungeons"))

    # -- querying ---------------------------------------------------------

    def estimate(self, spell_id: int, level: Optional[int],
                 name: Optional[str] = None,
                 dungeon: Optional[int] = None) -> Optional[dict[str, Any]]:
        """Average damage/healing per completed cast for ``spell_id`` at
        ``level``, or the nearest level with data. ``None`` when the spell
        is unknown. Returns ``{"damage", "healing", "level", "casts"}``
        where ``level`` is the bucket actually used.

        A cast and its damage often carry different spell ids (Fel
        Missiles: channel 1216571, damage 1216570) but the same name, so
        when the id itself has nothing (or only zeros) on record and
        ``name`` is given, the best-sampled same-named spell is used
        instead -- mirroring ``stats._estimate_kick_value``.

        That borrow is scoped to one dungeon. The in-run version is
        inherently so (it can only see this key's own spells); this one
        searched the whole accumulated history, across every dungeon and
        season, so a generic name -- "Shadow Bolt", "Frost Nova" -- kicked
        in one dungeon could be priced from an unrelated mob in another
        (2026-09-11). ``dungeon`` is the challenge-map id of the run being
        analyzed; a candidate qualifies when it has been seen there. A
        candidate that records no dungeon at all (older history, community
        data built from Warcraft Logs) is still allowed, since rejecting
        it would silently disable the fallback for everyone who has not
        rebuilt their data -- but a candidate recorded in OTHER dungeons
        and not this one is now refused."""
        found = self._estimate_by_id(spell_id, level)
        if (found is None or not (found["damage"] or found["healing"])) and name:
            key = name.casefold()
            best: Optional[dict[str, Any]] = None
            for sid, entry in self.spells.items():
                if sid == spell_id or str(entry.get("name", "")).casefold() != key:
                    continue
                if not self._shares_dungeon(entry, dungeon):
                    continue
                alt = self._estimate_by_id(sid, level)
                if alt and (alt["damage"] or alt["healing"]) and \
                        (best is None or alt["casts"] > best["casts"]):
                    best = alt
            if best is not None:
                return best
        return found

    @staticmethod
    def _shares_dungeon(entry: dict[str, Any], dungeon: Optional[int]) -> bool:
        known = entry.get("dungeons") or []
        if not known or not dungeon:
            return True  # nothing recorded either side: no basis to refuse
        return int(dungeon) in known

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
            "damage": _per_cast(b, casts, "damage", "direct_damage",
                                "direct_casts", "dot_damage", "dot_casts"),
            "healing": _per_cast(b, casts, "healing", "direct_healing",
                                 "direct_heal_casts", "dot_healing",
                                 "dot_heal_casts"),
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
                             int(b.get("casts", 0)),
                             **{k: int(b.get(k, 0)) for k in cls.SPLIT_FIELDS})
                except (TypeError, ValueError, AttributeError):
                    continue
            try:
                data.note_dungeons(spell_id, name, entry.get("dungeons"))
            except (TypeError, ValueError, AttributeError):
                pass
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "season": self.season,
            "version": self.version,
            "spells": {
                str(sid): {
                    "name": entry["name"],
                    # omitted entirely when empty, so a file built from data
                    # with no dungeon information reads exactly as before
                    **({"dungeons": sorted(entry["dungeons"])}
                       if entry.get("dungeons") else {}),
                    "levels": {
                        str(lv): {k: v for k, v in b.items() if v}
                                 or {"damage": 0, "healing": 0, "casts": 0}
                        for lv, b in sorted(entry["levels"].items())
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


def _per_cast(bucket: dict[str, Any], casts: int, combined: str,
              direct_total: str, direct_casts: str,
              dot_total: str, dot_casts: str) -> int:
    """Average amount per completed cast.

    Prefers the split: each component over its own cast count, summed --
    the same arithmetic the in-run estimate does, which is the whole point
    of this file being comparable with it. Falls back to the combined
    total over the combined count for buckets written before the split
    existed (and for community data, which has no split to give)."""
    dc = int(bucket.get(direct_casts, 0) or 0)
    pc = int(bucket.get(dot_casts, 0) or 0)
    if dc or pc:
        direct = int(bucket.get(direct_total, 0) or 0) / dc if dc else 0
        # ticks seen with no observed application fall back to the
        # direct-cast count, exactly as stats._estimate_kick_value does
        periodic_casts = pc or dc
        periodic = int(bucket.get(dot_total, 0) or 0) / periodic_casts if periodic_casts else 0
        return round(direct + periodic)
    return round(int(bucket.get(combined, 0) or 0) / casts)


HISTORY_COMMENT = (
    "Learned from your own combat logs by postmortem -- see "
    "analysis/spell_damage.py. Per enemy spell and keystone level: total "
    "damage/healing over the completed casts observed, so the kick-value "
    "estimate has a number for a spell that was kicked every time this run. "
    "Safe to delete; it rebuilds as you play."
)


def observe_stats(stats: Any, level: Optional[int],
                  dungeon: Optional[int] = None) -> SpellDamageData:
    """What one analyzed run contributes: every enemy spell that landed,
    with its total damage (direct + periodic) and completed-cast count,
    read off the same observation tables ``_estimate_kick_value`` uses.
    Requires those tables to have been finalized (``avg`` etc. present),
    i.e. call after ``compute_stats``.

    The direct and periodic parts are recorded with their OWN cast counts
    as well as combined, because that is how the in-run estimate averages
    them -- see the module docstring. ``dungeon`` is the run's
    challenge-map id, which scopes the same-name borrow in ``estimate``.
    """
    out = SpellDamageData()
    for entry_id, entry in stats.enemy_cast_observations.items():
        casts = entry.get("observed_casts", 0)
        if casts:
            out.add(entry_id, entry["name"], level,
                    damage=entry.get("direct_total", 0) + entry.get("dot_total", 0),
                    casts=casts, dungeon=dungeon,
                    direct_damage=entry.get("direct_total", 0),
                    direct_casts=entry.get("direct_casts", 0),
                    dot_damage=entry.get("dot_total", 0),
                    dot_casts=entry.get("dot_casts", 0))
    for entry_id, entry in stats.enemy_heal_observations.items():
        casts = entry.get("observed_casts", 0)
        if casts:
            out.add(entry_id, entry["name"], level,
                    healing=entry.get("direct_total", 0) + entry.get("dot_total", 0),
                    casts=casts, dungeon=dungeon,
                    direct_healing=entry.get("direct_total", 0),
                    direct_heal_casts=entry.get("direct_casts", 0),
                    dot_healing=entry.get("dot_total", 0),
                    dot_heal_casts=entry.get("dot_casts", 0))
    return out


def update_from_stats(stats: Any, level: Optional[int], path: str | Path,
                      dungeon: Optional[int] = None) -> SpellDamageData:
    """Fold one run's landed-cast observations into the history file at
    ``path`` and return the merged result. Best-effort by contract, like
    ``interrupt_learning.update_from_events``: callers must never lose a
    report because this cache could not be written."""
    merged = SpellDamageData.load(path)
    merged.merge(observe_stats(stats, level, dungeon))
    merged.save(path, comment=HISTORY_COMMENT)
    return merged
