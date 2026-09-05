"""Learn which enemy casts are interruptible from your own combat logs.

The client-side ground truth this project used to rely on is gone (see
``interruptibility.py`` -- Patch 12.0.0's Secret Values), and community
mechanics databases only ever say "this is worth interrupting", never
"this can't be". That leaves the kick-efficiency table listing every
uninterruptible enemy cast as though it were a missed kick, which is the
actual day-to-day complaint (2026-09-05: 220 of 264 distinct casts in 13
real runs were never kicked even once).

Both answers are derivable from the log itself:

* **Interruptible** -- somebody landed a ``SPELL_INTERRUPT`` on it. That
  is proof, not inference.
* **Not interruptible** -- somebody *used an interrupt ability* on the
  enemy while it was mid-cast and no interrupt ever followed, repeatedly,
  and the spell has never once been successfully interrupted. Strong
  evidence, though not proof (see ``to_interrupt_data``'s threshold and
  its caveat about a group that is simply always too slow).

Neither signal needs a maintained spell database: the set of interrupt
*abilities* is read out of the log itself, since any spell that appears
as the interrupting spell in a ``SPELL_INTERRUPT`` is by definition one.
A small seed list (``KNOWN_INTERRUPT_ABILITY_IDS``, every entry taken
from a real observed interrupt) covers the one case that bootstrap
cannot: a run where nothing was successfully interrupted at all, which
is precisely a run worth learning "these are immune" from.

Observations accumulate across runs (``merge``), so the answer improves
the more you play -- the self-building database the addon was meant to
provide before that door closed, moved to the Python side where nothing
is protected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..combatlog.events import Event, extra_spell_info, is_hostile_npc, spell_info

#: How long after an interrupt ability is used its ``SPELL_INTERRUPT`` may
#: still arrive. They are logged essentially simultaneously; this is slack
#: for ordering, not for a real delay.
CORRELATION_WINDOW_S = 1.0

#: Interrupt abilities to recognise even in a log that contains no
#: successful interrupt at all. The set is normally read out of the log
#: itself (any spell that interrupts something is one), but that
#: bootstrap fails in exactly the run worth learning from most: one where
#: the group kicked all night and nothing was interruptible, so there is
#: no landed interrupt to learn the ability list from.
#:
#: Every id here was observed as the interrupting spell in a real
#: SPELL_INTERRUPT in this project's own logs -- none are guessed, and
#: the log-derived set is still unioned on top, so an ability missing
#: from this list costs nothing as soon as it lands one interrupt.
KNOWN_INTERRUPT_ABILITY_IDS: frozenset[int] = frozenset({
    2139,    # Counterspell (mage)
    6552,    # Pummel (warrior)
    19647,   # Spell Lock (warlock pet)
    31935,   # Avenger's Shield (paladin)
    47528,   # Mind Freeze (death knight)
    57994,   # Wind Shear (shaman)
    93985,   # Skull Bash (druid)
    96231,   # Rebuke (paladin)
    116705,  # Spear Hand Strike (monk)
    147362,  # Counter Shot (hunter)
    187707,  # Muzzle (hunter)
    347008,  # Axe Toss (warlock pet)
})

#: Default number of failed attempts, with zero successes ever, before a
#: spell is called uninterruptible. Two is enough to clear the common
#: "two people kicked the same cast, one of them lost the race" case --
#: but that case is already excluded by requiring zero successes, so this
#: mainly guards against a single mistimed kick on a genuinely
#: interruptible spell.
DEFAULT_MIN_ATTEMPTS = 3


@dataclass
class InterruptObservations:
    """Accumulated per-spell evidence. ``spells`` maps spell id ->
    ``{"name", "interrupted", "survived_attempts"}``."""

    spells: dict[int, dict[str, Any]] = field(default_factory=dict)

    def _entry(self, spell_id: int, name: str) -> dict[str, Any]:
        entry = self.spells.setdefault(
            spell_id, {"name": name, "interrupted": 0, "survived_attempts": 0}
        )
        # a later log may carry a better name for the same id
        if name and entry["name"].startswith("spell:"):
            entry["name"] = name
        return entry

    def add_interrupted(self, spell_id: int, name: str, n: int = 1) -> None:
        self._entry(spell_id, name)["interrupted"] += n

    def add_survived(self, spell_id: int, name: str, n: int = 1) -> None:
        self._entry(spell_id, name)["survived_attempts"] += n

    def merge(self, other: "InterruptObservations") -> None:
        for spell_id, entry in other.spells.items():
            mine = self._entry(spell_id, entry.get("name") or f"spell:{spell_id}")
            mine["interrupted"] += entry.get("interrupted", 0)
            mine["survived_attempts"] += entry.get("survived_attempts", 0)

    @classmethod
    def load(cls, path: str | Path) -> "InterruptObservations":
        """Load accumulated observations. A missing file is an empty set
        of observations, not an error -- this is a cache that builds up
        over time, and "nothing learned yet" is a normal state."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return cls()
        spells: dict[int, dict[str, Any]] = {}
        for sid, entry in (payload.get("spells") or {}).items():
            try:
                spells[int(sid)] = {
                    "name": str(entry.get("name") or f"spell:{sid}"),
                    "interrupted": int(entry.get("interrupted", 0)),
                    "survived_attempts": int(entry.get("survived_attempts", 0)),
                }
            except (TypeError, ValueError):
                continue
        return cls(spells=spells)

    def save(self, path: str | Path) -> None:
        payload = {
            "_comment": "Learned from your own combat logs by postmortem -- "
                        "see analysis/interrupt_learning.py. 'interrupted' "
                        "proves a spell is interruptible; repeated "
                        "'survived_attempts' with zero interrupts is strong "
                        "evidence it is not. Safe to delete; it rebuilds "
                        "as you play.",
            "spells": {
                str(sid): entry for sid, entry in sorted(self.spells.items())
            },
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    def to_interrupt_data(
        self, min_attempts: int = DEFAULT_MIN_ATTEMPTS
    ) -> dict[str, Any]:
        """Convert to the JSON shape ``InterruptibilityData.load`` reads.

        A spell interrupted at least once is ``true`` -- proof. A spell
        never interrupted but which survived ``min_attempts`` or more
        interrupt attempts mid-cast is ``false``. Everything else is
        omitted entirely, which the consumer treats as "no data" and
        falls back to its own heuristic.

        The false case is evidence, not proof: a genuinely interruptible
        spell that this group is consistently too slow on would land here
        too. Raising ``min_attempts`` trades coverage for confidence.
        """
        out: dict[str, dict[str, Any]] = {}
        for spell_id, entry in self.spells.items():
            if entry["interrupted"] > 0:
                verdict = True
            elif entry["survived_attempts"] >= min_attempts:
                verdict = False
            else:
                continue
            out[str(spell_id)] = {"name": entry["name"], "interruptible": verdict}
        return {"spells": out}


def observe_events(events: Iterable[Event]) -> InterruptObservations:
    """Watch one run's events and report what it proves about
    interruptibility. See the module docstring for the two signals.

    Deliberately only considers enemy *hard* casts (SPELL_CAST_START ..
    SPELL_CAST_SUCCESS) for the negative signal: a channel's end is not
    logged, so "the kick landed while it was channelling" cannot be
    established without guessing, and a wrong "uninterruptible" is worse
    than no answer. Successful interrupts are counted for both.
    """
    events = list(events)

    # Pass 1: which player spells ARE interrupt abilities, and when did an
    # interrupt actually land? Read straight out of the log rather than a
    # maintained list -- any spell that interrupts something is one.
    interrupt_ability_ids: set[int] = set(KNOWN_INTERRUPT_ABILITY_IDS)
    landed: list[tuple[float, str]] = []   # (ts, target guid)
    obs = InterruptObservations()
    for event in events:
        if event.name != "SPELL_INTERRUPT":
            continue
        kick = spell_info(event)
        if kick is not None:
            interrupt_ability_ids.add(kick.spell_id)
        landed.append((event.ts, event.dest_guid))
        stopped = extra_spell_info(event)
        if stopped is not None:
            obs.add_interrupted(stopped.spell_id, stopped.spell_name)

    # Pass 2: an interrupt ability used on an enemy that was mid-hard-cast,
    # with no interrupt landing on that enemy around then, means the cast
    # shrugged it off.
    open_casts: dict[str, tuple[int, str]] = {}
    for event in events:
        name = event.name
        if name == "SPELL_CAST_START" and is_hostile_npc(event.source_flags):
            sp = spell_info(event)
            if sp is not None:
                open_casts[event.source_guid] = (sp.spell_id, sp.spell_name)
        elif name == "SPELL_INTERRUPT":
            open_casts.pop(event.dest_guid, None)
        elif name == "SPELL_CAST_SUCCESS":
            sp = spell_info(event)
            if sp is None:
                continue
            if is_hostile_npc(event.source_flags):
                open_casts.pop(event.source_guid, None)
                continue
            if sp.spell_id not in interrupt_ability_ids or not event.dest_guid:
                continue
            target_cast = open_casts.get(event.dest_guid)
            if target_cast is None:
                continue  # kicked something that wasn't casting; says nothing
            if any(abs(ts - event.ts) <= CORRELATION_WINDOW_S and guid == event.dest_guid
                   for ts, guid in landed):
                continue  # it worked; already counted as interrupted above
            obs.add_survived(target_cast[0], target_cast[1])
    return obs


def update_from_events(
    events: Iterable[Event], path: str | Path
) -> InterruptObservations:
    """Fold one run's observations into the accumulated file at ``path``
    and return the merged result. Best-effort by contract: the caller
    (Watch Live, the CLI) must never lose a run's report because this
    cache could not be written."""
    merged = InterruptObservations.load(path)
    merged.merge(observe_events(events))
    merged.save(path)
    return merged
