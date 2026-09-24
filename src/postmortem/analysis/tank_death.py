"""Tank death post-mortem: which mitigation was up, and which was sitting
ready, at the moment a tank died.

``stats._tag_death_defensives`` already answers "what did the victim have
*up* when they died" (see ``gamedata.DEFENSIVES``). This module answers the
other half of the question -- the one ROADMAP.md has carried as an open item:
*was a defensive available and unused?* -- and adds the group-side version of
it: was an external (Pain Suppression, Ironbark, Blessing of Sacrifice, ...)
sitting ready on a healer who never pressed it.

Why this lives out here, and not in the addon
---------------------------------------------
Patch 12.0's Secret Values put every in-combat number an addon would need
for this -- health, absorbs, damage taken -- permanently out of reach of
addon Lua (see ``addon/Postmortem/MeterUtil.lua``'s header for the same
wall, hit from the other side). ``WoWCombatLog.txt`` is written server-side
and parsed out of process, so none of that applies here: the file still
carries full ``SWING_DAMAGE``/``SPELL_DAMAGE`` detail on a 12.x client. The
analysis that can no longer be done live is exactly the analysis that can
still be done afterwards.

The honesty rule this module is built around
--------------------------------------------
Telling a tank "you had Shield Wall up and didn't press it" is worthless if
they never talented Shield Wall. The combat log does not carry a spellbook,
so absence of a cast is genuinely ambiguous: *not talented* and *had it,
never used it* look identical. This module therefore splits the two and only
makes the accusing claim when it is earned:

- **Cast at least once this run** -> the player demonstrably has the spell.
  Its cooldown can be tracked from that cast, and "ready at death, unused"
  is a real finding. Reported in ``available_unused``.
- **Never cast this run** -> could be an untalented spell, a swapped build,
  or a genuinely unused button. Reported separately in ``never_used``, which
  the renderers present as an observation, never as a mistake.

Same discipline as ``DeathRecord.died_without_defensive``: where we cannot
honestly say, we say so, rather than guessing in the direction that makes
the report look clever.

...and how the addon settles it
-------------------------------
That second case is a hedge forced on us by the log format, not a fact
about the world -- and it is exactly the gap the addon can close, because
the client will simply tell it whether a spell is in the spellbook. When a
``PostmortemTankDB`` capture is available (see :class:`TankKnowledge`, fed
by ``postmortem analyze --tank-db``), each never-cast spell resolves three
ways instead of one:

- **known not to be talented** -> dropped entirely; nothing is said.
- **known to be talented**, and the run has already been going longer than
  the spell's own cooldown (so it cannot still be recovering from a cast
  made before the log began) -> promoted to a real ``available_unused``
  finding, flagged ``never_cast``.
- **still unknown** -> stays the caveated observation it was.

This is the one place the two halves of the feature are worth more together
than apart, and it is why the addon persists a spellbook snapshot it never
displays.

Cooldown values are estimates
-----------------------------
``data/tank_defensives.json`` carries baseline cooldowns, which talents,
tier bonuses and haste all move. Almost every such effect *reduces* a
cooldown, so a baseline is normally an over-estimate of the real recharge --
which makes "we think it was ready" the conservative direction. ``READY_MARGIN_S``
adds a further grace period on top so a borderline case is dropped rather
than asserted. The spell ids, not the durations, are the part of that file
to trust.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

#: Extra seconds a spell must have been off cooldown before we are willing
#: to call it "available and unused". Absorbs small errors in the baseline
#: cooldown table and any rounding in log timestamps, in the direction of
#: saying nothing rather than saying something wrong.
READY_MARGIN_S = 3.0

#: How far back to look for the active-mitigation window that *should* have
#: covered a death. Deliberately short: active mitigation is the button a
#: tank is meant to be holding continuously, so a gap of more than a few
#: seconds is the interesting signal.
MITIGATION_GAP_WINDOW_S = 10.0


@dataclass(frozen=True)
class Defensive:
    """One entry from the ``defensives`` list in tank_defensives.json."""

    spell_id: int
    name: str
    specs: tuple[int, ...]
    category: str
    cooldown_s: float
    duration_s: float
    charges: int
    note: Optional[str] = None

    @property
    def scorable(self) -> bool:
        """True when "was it ready?" is a meaningful question for this spell.

        Resource-gated buttons (Ironfur, Ignore Pain, Shield of the
        Righteous, Death Strike) carry ``cooldown_s: 0`` precisely because
        their availability is governed by rage/Holy Power/runes, which the
        combat log does not expose. They are tracked for *usage* and never
        scored as unused -- claiming a tank "had Ironfur available" without
        knowing their rage would be a guess wearing a fact's clothing.
        """
        return self.cooldown_s > 0


@dataclass(frozen=True)
class External:
    """One entry from the ``externals`` list: a defensive cast on someone
    else, which is why it is keyed by the *caster's* spec rather than the
    victim's."""

    spell_id: int
    name: str
    caster_specs: tuple[int, ...]
    cooldown_s: float
    duration_s: float


@dataclass
class TankKnowledge:
    """What the addon saw in the player's spellbook, read back from the
    ``PostmortemTankDB`` SavedVariable (see addon/Postmortem/TankDeath.lua).

    This exists to close the one gap the combat log cannot: it carries no
    spellbook, so *never talented* and *had it, never pressed it* are
    indistinguishable from the log alone. The addon can simply ask the
    client, and this is that answer carried across.

    Keyed by spec id, most recent capture winning -- a player who
    respecced between runs gets judged on the build they were actually
    wearing, not on an older snapshot.
    """

    known: dict[int, set[int]] = field(default_factory=dict)
    not_known: dict[int, set[int]] = field(default_factory=dict)

    def verdict(self, spec_id: Optional[int], spell_id: int) -> Optional[bool]:
        """True if the player is known to have the spell, False if they are
        known not to, None if this capture says nothing about it (no record
        for the spec, or the client wouldn't answer at the time)."""
        if spec_id is None:
            return None
        if spell_id in self.known.get(spec_id, ()):
            return True
        if spell_id in self.not_known.get(spec_id, ()):
            return False
        return None


def knowledge_from_savedvariables(global_table: Any) -> TankKnowledge:
    """Build a :class:`TankKnowledge` from the parsed ``.global`` table of
    ``PostmortemTankDB``.

    Tolerant by construction: this is a file a game client wrote and a user
    may have edited, so anything unexpected is skipped rather than raised
    on. Records are applied oldest-first so the newest capture for a spec
    ends up as the one that counts.

    The Lua parser returns array-like tables as 1-indexed dicts, so both a
    list and a dict-of-index are accepted here.
    """
    knowledge = TankKnowledge()
    if not isinstance(global_table, dict):
        return knowledge

    def _rows(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value[k] for k in sorted(value, key=lambda k: (str(type(k)), k))]
        return []

    def _ids(value: Any) -> set[int]:
        return {v for v in _rows(value) if isinstance(v, int)}

    records = [r for r in _rows(global_table.get("deaths")) if isinstance(r, dict)]
    records.sort(key=lambda r: r.get("ts") if isinstance(r.get("ts"), (int, float)) else 0)

    for record in records:
        spec_id = record.get("specID")
        if not isinstance(spec_id, int):
            continue
        known = _ids(record.get("known"))
        not_known = _ids(record.get("notKnown"))
        if not known and not not_known:
            continue
        # Whole-snapshot replace, not union: a respec removes spells, and
        # unioning across captures would keep claiming the player still has
        # something they dropped a patch ago.
        knowledge.known[spec_id] = known
        knowledge.not_known[spec_id] = not_known
    return knowledge


@dataclass
class TankDefensiveData:
    """The loaded tank_defensives.json table."""

    defensives: dict[int, Defensive] = field(default_factory=dict)
    externals: dict[int, External] = field(default_factory=dict)
    tank_spec_ids: frozenset[int] = frozenset()
    spec_names: dict[int, str] = field(default_factory=dict)

    def for_spec(self, spec_id: Optional[int]) -> list[Defensive]:
        """Every defensive this spec can have, in a stable order."""
        if spec_id is None:
            return []
        return sorted(
            (d for d in self.defensives.values() if spec_id in d.specs),
            key=lambda d: (d.category, d.name),
        )

    def externals_for_spec(self, spec_id: Optional[int]) -> list[External]:
        if spec_id is None:
            return []
        return sorted(
            (e for e in self.externals.values() if spec_id in e.caster_specs),
            key=lambda e: e.name,
        )

    def is_tank_spec(self, spec_id: Optional[int]) -> bool:
        return spec_id is not None and spec_id in self.tank_spec_ids


def load_tank_defensives(path: Path) -> TankDefensiveData:
    """Load the table. Raises on a malformed file -- unlike the optional
    data files (avoidable, dispel), this one is bundled with the package,
    so a broken read is a packaging bug worth surfacing, not a missing
    optional input to shrug at. Callers that want the "no section" behaviour
    use :func:`load_bundled_tank_defensives`, which tolerates absence.
    """
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    defensives: dict[int, Defensive] = {}
    for entry in payload.get("defensives", ()):
        spell_id = int(entry["id"])
        defensives[spell_id] = Defensive(
            spell_id=spell_id,
            name=str(entry["name"]),
            specs=tuple(int(s) for s in entry.get("specs", ())),
            category=str(entry.get("category", "major")),
            cooldown_s=float(entry.get("cooldown_s", 0) or 0),
            duration_s=float(entry.get("duration_s", 0) or 0),
            charges=int(entry.get("charges", 1) or 1),
            note=entry.get("note"),
        )

    externals: dict[int, External] = {}
    for entry in payload.get("externals", ()):
        spell_id = int(entry["id"])
        externals[spell_id] = External(
            spell_id=spell_id,
            name=str(entry["name"]),
            caster_specs=tuple(int(s) for s in entry.get("caster_specs", ())),
            cooldown_s=float(entry.get("cooldown_s", 0) or 0),
            duration_s=float(entry.get("duration_s", 0) or 0),
        )

    spec_names = {
        int(k): str(v.get("name", "")) for k, v in (payload.get("specs") or {}).items()
    }
    return TankDefensiveData(
        defensives=defensives,
        externals=externals,
        tank_spec_ids=frozenset(int(s) for s in payload.get("tank_spec_ids", ())),
        spec_names=spec_names,
    )


def load_bundled_tank_defensives() -> Optional[TankDefensiveData]:
    """The packaged table, or None if it is missing or unreadable.

    Mirrors how the other bundled tables degrade: a consumer that cannot
    load this simply renders no tank-death section, rather than failing a
    whole run's analysis over it.
    """
    from ..bundled import bundled_tank_defensives_path

    path = bundled_tank_defensives_path()
    try:
        return load_tank_defensives(path)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _last_cast_before(
    casts: Iterable[dict[str, Any]], spell_id: int, ts: float
) -> Optional[float]:
    """Timestamp of the latest cast of ``spell_id`` at or before ``ts``.

    ``cast_timeline`` is built in event order, so a reverse scan finds the
    latest match immediately in the common case.
    """
    latest: Optional[float] = None
    for cast in casts:
        cast_ts = cast.get("ts")
        if cast.get("spell_id") != spell_id or cast_ts is None:
            continue
        if cast_ts <= ts and (latest is None or cast_ts > latest):
            latest = cast_ts
    return latest


def _ready_at(last_cast_ts: Optional[float], cooldown_s: float, ts: float) -> bool:
    """Was a spell last cast at ``last_cast_ts`` off cooldown by ``ts``?

    ``None`` (never cast before this point) is *not* treated as ready here;
    that ambiguity is handled one level up, where it becomes ``never_used``
    rather than an accusation.
    """
    if last_cast_ts is None:
        return False
    return last_cast_ts + cooldown_s + READY_MARGIN_S <= ts


def analyze_tank_death(
    death: Any,
    spec_id: Optional[int],
    casts_by_guid: dict[str, list[dict[str, Any]]],
    players: dict[str, Any],
    data: TankDefensiveData,
    full_cast_timeline: bool,
    knowledge: Optional[TankKnowledge] = None,
    run_start_ts: Optional[float] = None,
    deaths: Iterable[Any] = (),
) -> dict[str, Any]:
    """Build the tank-death annotation for one :class:`stats.DeathRecord`.

    Returns a dict shaped for the report JSON. ``scored`` is False whenever
    the inputs cannot support a real finding (no cast data, unknown spec, a
    spec the table does not cover) -- renderers use it to decide between
    showing a finding and showing nothing at all.
    """
    own = data.for_spec(spec_id)
    if not full_cast_timeline or spec_id is None or not own:
        return {
            "scored": False,
            "is_tank": data.is_tank_spec(spec_id),
            "spec_name": data.spec_names.get(spec_id or -1),
            "available_unused": [],
            "active_at_death": [],
            "never_used": [],
            "externals_available": [],
            "mitigation_gap_s": None,
            "spellbook_known": False,
        }

    victim_casts = casts_by_guid.get(death.player_guid, [])
    used_ids = {
        entry.get("spell_id") for entry in (death.defensives_used_before_death or ())
    }

    available_unused: list[dict[str, Any]] = []
    never_used: list[dict[str, Any]] = []
    active_at_death: list[dict[str, Any]] = []

    for defensive in own:
        if defensive.spell_id in used_ids:
            continue  # it was up; that is _tag_death_defensives' finding, not ours
        last_cast = _last_cast_before(victim_casts, defensive.spell_id, death.ts)
        cast_at_all = any(
            c.get("spell_id") == defensive.spell_id for c in victim_casts
        )

        # This table is broader than gamedata.DEFENSIVES (which is what
        # fills used_ids), so a spell can be in ours, have been pressed,
        # and still be inside its own duration at the moment of death
        # without the other table knowing about it. Reporting that as
        # "ready and unused" would be the exact false accusation this
        # module exists to avoid, so duration is checked here too.
        if (
            last_cast is not None
            and defensive.duration_s > 0
            and last_cast + defensive.duration_s >= death.ts
        ):
            active_at_death.append({
                "spell_id": defensive.spell_id,
                "name": defensive.name,
                "category": defensive.category,
                "ts": last_cast,
            })
            continue

        if not cast_at_all:
            # Never pressed in this run. From the log alone this is
            # indistinguishable from untalented, so it stays an
            # observation -- unless the addon's spellbook capture settles
            # it (see TankKnowledge).
            verdict = knowledge.verdict(spec_id, defensive.spell_id) if knowledge else None
            if verdict is False:
                continue  # provably not talented: say nothing at all
            if (
                verdict is True
                and defensive.scorable
                and run_start_ts is not None
                and death.ts - run_start_ts >= defensive.cooldown_s
            ):
                # Provably known, never pressed, and the run has been going
                # longer than the spell's own cooldown -- so it cannot still
                # be recovering from a cast made before the log started.
                # That makes this a real finding rather than a hedge.
                available_unused.append({
                    "spell_id": defensive.spell_id,
                    "name": defensive.name,
                    "category": defensive.category,
                    "last_used_ts": None,
                    "ready_for_s": None,
                    "never_cast": True,
                })
                continue
            never_used.append({
                "spell_id": defensive.spell_id,
                "name": defensive.name,
                "category": defensive.category,
                # True when we know they have it but can't prove it was off
                # cooldown (too early in the run); the renderers can then
                # drop the "may not be talented" caveat honestly.
                "known": verdict is True,
            })
            continue

        if not defensive.scorable:
            continue  # resource-gated; see Defensive.scorable

        if _ready_at(last_cast, defensive.cooldown_s, death.ts):
            available_unused.append({
                "spell_id": defensive.spell_id,
                "name": defensive.name,
                "category": defensive.category,
                "last_used_ts": last_cast,
                "ready_for_s": round(
                    death.ts - (last_cast + defensive.cooldown_s), 1
                ),
            })

    externals_available = _externals_available(
        death, casts_by_guid, players, data, deaths
    )
    return {
        "scored": True,
        "is_tank": data.is_tank_spec(spec_id),
        "spec_name": data.spec_names.get(spec_id),
        "available_unused": sorted(
            available_unused, key=lambda e: (e["category"], e["name"])
        ),
        "active_at_death": sorted(active_at_death, key=lambda e: e["ts"]),
        "never_used": never_used,
        "externals_available": externals_available,
        "mitigation_gap_s": _mitigation_gap(death, victim_casts, own),
        # True when the addon's spellbook capture covered this spec, so the
        # renderers know the never_used caveat has actually been resolved
        # rather than merely absent.
        "spellbook_known": bool(knowledge and spec_id in knowledge.known),
    }


def _externals_available(
    death: Any,
    casts_by_guid: dict[str, list[dict[str, Any]]],
    players: dict[str, Any],
    data: TankDefensiveData,
    deaths: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    """Externals a *groupmate* could have thrown on the victim and didn't.

    Same "must have cast it at least once" proof-of-possession rule as the
    victim's own defensives: a healer who never pressed Pain Suppression all
    run may not be Discipline-specced the way the table assumes, so their
    silence is not evidence.

    A groupmate lying dead when the victim died could not have thrown
    anything, and blaming them for an unused external is simply wrong --
    yet a healer who died first was listed as "had Pain Suppression ready"
    until 2026-09-23. A death before the victim's counts as still dead
    unless that player cast something after it (a battle res leaves no
    single reliable line of its own, but a living player casts).
    """
    out: list[dict[str, Any]] = []
    for guid, player in players.items():
        if guid == death.player_guid:
            continue
        if _dead_at(guid, death.ts, deaths, casts_by_guid.get(guid, [])):
            continue
        caster_spec = getattr(player, "spec_id", None)
        casts = casts_by_guid.get(guid, [])
        for external in data.externals_for_spec(caster_spec):
            if not any(c.get("spell_id") == external.spell_id for c in casts):
                continue  # never pressed this run -- no proof they have it
            last_cast = _last_cast_before(casts, external.spell_id, death.ts)
            if _ready_at(last_cast, external.cooldown_s, death.ts):
                out.append({
                    "spell_id": external.spell_id,
                    "name": external.name,
                    "caster": getattr(player, "name", "") or guid,
                    "last_used_ts": last_cast,
                })
    return sorted(out, key=lambda e: (e["caster"], e["name"]))


def _dead_at(guid: str, ts: float, deaths: Iterable[Any],
             casts: list[dict[str, Any]]) -> bool:
    """Was ``guid`` dead at ``ts``: their latest death before it has no
    cast of theirs after it (see _externals_available)."""
    last_death: Optional[float] = None
    for d in deaths:
        d_ts = getattr(d, "ts", None)
        if getattr(d, "player_guid", None) != guid or d_ts is None or d_ts >= ts:
            continue
        if last_death is None or d_ts > last_death:
            last_death = d_ts
    if last_death is None:
        return False
    return not any(
        c.get("ts") is not None and last_death < c["ts"] <= ts for c in casts
    )


def _mitigation_gap(
    death: Any, victim_casts: list[dict[str, Any]], own: list[Defensive]
) -> Optional[float]:
    """Seconds between the victim's last active-mitigation press and their
    death, or None when the spec has no tracked active mitigation or never
    pressed it inside the window.

    This is the "were you holding your rotational button" number -- the one
    every tank guide names as the difference between an average and a good
    tank -- and it is answerable from casts alone, with no health or absorb
    value needed.
    """
    mitigation_ids = {d.spell_id for d in own if d.category == "active_mitigation"}
    if not mitigation_ids:
        return None
    latest: Optional[float] = None
    for cast in victim_casts:
        cast_ts = cast.get("ts")
        if cast.get("spell_id") not in mitigation_ids or cast_ts is None:
            continue
        if cast_ts <= death.ts and (latest is None or cast_ts > latest):
            latest = cast_ts
    if latest is None or death.ts - latest > MITIGATION_GAP_WINDOW_S:
        return None
    return round(death.ts - latest, 1)


def annotate_deaths(
    stats: Any,
    data: Optional[TankDefensiveData],
    full_cast_timeline: bool,
    knowledge: Optional[TankKnowledge] = None,
    run_start_ts: Optional[float] = None,
) -> None:
    """Attach a ``tank_analysis`` dict to every death in ``stats``.

    A no-op when the table could not be loaded, so every caller can invoke
    this unconditionally and the section simply does not appear.
    """
    if data is None:
        return
    casts_by_guid: dict[str, list[dict[str, Any]]] = {}
    for cast in stats.cast_timeline:
        guid = cast.get("player_guid")
        if guid:
            casts_by_guid.setdefault(guid, []).append(cast)

    for death in stats.deaths:
        player = stats.players.get(death.player_guid)
        spec_id = getattr(player, "spec_id", None) if player is not None else None
        death.tank_analysis = analyze_tank_death(
            death, spec_id, casts_by_guid, stats.players, data, full_cast_timeline,
            knowledge=knowledge, run_start_ts=run_start_ts, deaths=stats.deaths,
        )
