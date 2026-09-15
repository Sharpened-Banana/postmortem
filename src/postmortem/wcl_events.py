"""What public Warcraft Logs events say about enemy spells -- the pure
half of ``postmortem build-event-data`` (cli.py; wcl.py does the
fetching).

Three of the analyzer's data files could not be built from anything
better than a guide or one account's own logs (2026-09-15):

- **stealable buffs** -- no addon or guide publishes a per-dungeon list,
  and Patch 12.1's Secret Values stop addons reading the live flag. But
  a Spellsteal or Purge that removed a buff is a ``dispel`` event with
  the removed buff in ``extraAbilityGameID``: every buff anyone has ever
  stolen is in the public logs.
- **interruptibility** -- the client flag is unreadable since 12.0 and
  the Method.gg source only says "worth kicking". A ``SPELL_INTERRUPT``
  is proof a cast is kickable; hundreds of begin-casts across many runs
  with never one interrupt is strong evidence it is not. This is
  analysis/interrupt_learning.py's reasoning applied to the whole
  population instead of one account.
- **dispellable debuffs** -- Method's list is hand-copied. A friendly
  ``dispel`` event names the debuff; which *school* it is follows from
  which dispel spells removed it (Remove Curse can only remove a curse).

Also kept, for a later report section: what dealt the killing blows.

``summarize_fight`` folds one fight's raw events into a small dict that
is what the samples file stores (raw events are large and WCL does not
freeze their shape); the ``aggregate_*`` functions fold many summaries
into the data files. Stdlib only, no I/O.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .analysis.dispels import SCHOOLS

#: Spellsteal proper. A buff removed by it is stealable by definition.
SPELLSTEAL_IDS: frozenset[int] = frozenset({30449})

#: Soothes: remove an enrage from an enemy. A buff removed by one of these
#: is an enrage, which is a different report question (crowd control) from
#: a buff worth stealing, so they are kept apart.
SOOTHE_IDS: dict[int, str] = {
    2908: "Soothe",
    19801: "Tranquilizing Shot",
    5938: "Shiv",
}

#: Friendly dispels and the debuff schools each can remove. A debuff's
#: school is the intersection of the schools of every spell seen removing
#: it; a single-school remover settles it outright. Ids from the live
#: client (spellbook), stable across expansions.
DISPELLER_SCHOOLS: dict[int, frozenset[str]] = {
    527: frozenset({"magic", "disease"}),        # Purify (Priest)
    213634: frozenset({"disease"}),              # Purify Disease (Shadow)
    32375: frozenset({"magic"}),                 # Mass Dispel
    4987: frozenset({"magic", "poison", "disease"}),   # Cleanse (Holy Paladin)
    213644: frozenset({"poison", "disease"}),    # Cleanse Toxins (Paladin)
    88423: frozenset({"magic", "curse", "poison"}),    # Nature's Cure (Resto Druid)
    2782: frozenset({"curse", "poison"}),        # Remove Corruption (Druid)
    77130: frozenset({"magic", "curse"}),        # Purify Spirit (Resto Shaman)
    51886: frozenset({"curse"}),                 # Cleanse Spirit (Shaman)
    115450: frozenset({"magic", "poison", "disease"}), # Detox (Mistweaver)
    218164: frozenset({"poison", "disease"}),    # Detox (other Monk)
    360823: frozenset({"magic", "poison"}),      # Naturalize (Preservation)
    365585: frozenset({"poison"}),               # Expunge (Evoker)
    475: frozenset({"curse"}),                   # Remove Curse (Mage)
    89808: frozenset({"magic"}),                 # Singe Magic (Warlock Imp)
    119905: frozenset({"magic"}),                # Singe Magic (Command Demon)
    1044: frozenset({"magic"}),                  # Blessing of Freedom -- see note
}
# Blessing of Freedom "removes" movement-impairing effects in the log as
# a dispel with the impaired debuff as the extra ability. Those are magic
# or physical roots/snares, so a debuff *only* ever removed by Freedom is
# left with candidates {"magic"} and the caller's minimum-remover rule
# (see aggregate_dispels) keeps one-off Freedom removals from tagging a
# physical snare as a magic dispel.

#: Enemy-buff removals that are purges, not steals: still worth a note in
#: the enemy-casts table ("purge this"), just not a Spellsteal star.
PURGE_IDS: dict[int, str] = {
    370: "Purge",
    528: "Dispel Magic",
    19505: "Devour Magic",
    278326: "Consume Magic",
    32375: "Mass Dispel",
    25046: "Arcane Torrent",
    50613: "Arcane Torrent",
    69179: "Arcane Torrent",
    80483: "Arcane Torrent",
    129597: "Arcane Torrent",
    155145: "Arcane Torrent",
    202719: "Arcane Torrent",
    232633: "Arcane Torrent",
}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bump(table: dict[str, int], key: Any) -> None:
    k = str(key)
    table[k] = table.get(k, 0) + 1


def summarize_fight(
    dispels: Iterable[dict[str, Any]],
    interrupts: Iterable[dict[str, Any]],
    begincasts: Iterable[dict[str, Any]],
    deaths: Iterable[dict[str, Any]],
    names: dict[int, str],
    actors: Optional[dict[int, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """One fight's raw events -> the compact record the samples file
    keeps. Counts are keyed by string ids (JSON-friendly):

    - ``stolen``  ``{buff_id: {remover_id: n}}`` -- enemy buffs removed
      by any friendly dispel (Spellsteal, purges, soothes alike; the
      aggregate sorts them by remover).
    - ``dispelled`` ``{debuff_id: {remover_id: n}}`` -- debuffs removed
      from friendly targets.
    - ``interrupted`` ``{spell_id: n}`` -- enemy casts stopped by a
      friendly interrupt.
    - ``begincasts`` ``{spell_id: n}`` -- enemy casts with a cast time
      (the denominator for "never interrupted").
    - ``killed_by`` ``{spell_id: n}`` -- friendly deaths per killing ability.
    - ``names`` ``{id: name}`` for every id above, and ``npcs``
      ``{buff_id: npc_name}`` for stolen buffs where the target actor is
      known.

    Field names are WCL's own; every read is tolerant, so a row missing
    a field is skipped rather than raising.
    """
    actors = actors or {}
    stolen: dict[str, dict[str, int]] = {}
    dispelled: dict[str, dict[str, int]] = {}
    interrupted: dict[str, int] = {}
    begincast_counts: dict[str, int] = {}
    killed_by: dict[str, int] = {}
    used: set[int] = set()
    npcs: dict[str, str] = {}

    for ev in dispels:
        remover = _int(ev.get("abilityGameID"))
        removed = _int(ev.get("extraAbilityGameID"))
        if remover is None or removed is None:
            continue
        target_friendly = ev.get("targetIsFriendly")
        if target_friendly is None:
            # older rows: a removed BUFF on a non-friendly is the enemy case
            target_friendly = not bool(ev.get("isBuff"))
        table = dispelled if target_friendly else stolen
        per = table.setdefault(str(removed), {})
        _bump(per, remover)
        used.update((remover, removed))
        if not target_friendly:
            actor = actors.get(_int(ev.get("targetID")) or -1)
            if actor and actor.get("name"):
                npcs.setdefault(str(removed), str(actor["name"]))

    for ev in interrupts:
        stopped = _int(ev.get("extraAbilityGameID"))
        if stopped is None:
            continue
        if ev.get("targetIsFriendly") is True:
            continue  # an enemy kicking us
        _bump(interrupted, stopped)
        used.add(stopped)

    for ev in begincasts:
        if ev.get("type") not in (None, "begincast"):
            continue
        if ev.get("sourceIsFriendly") is True:
            continue
        sid = _int(ev.get("abilityGameID"))
        if sid is None:
            continue
        _bump(begincast_counts, sid)
        used.add(sid)

    for ev in deaths:
        if ev.get("targetIsFriendly") is False:
            continue  # an enemy dying
        killer = _int(ev.get("killingAbilityGameID"))
        if killer is None:
            killer = _int(ev.get("abilityGameID"))
        if killer is None:
            continue
        _bump(killed_by, killer)
        used.add(killer)

    return {
        "stolen": stolen,
        "dispelled": dispelled,
        "interrupted": interrupted,
        "begincasts": begincast_counts,
        "killed_by": killed_by,
        "names": {str(i): names[i] for i in sorted(used) if names.get(i)},
        "npcs": npcs,
    }


def _fights(samples: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in (samples.get("fights") or {}).values() if isinstance(f, dict)]


def _name(samples_names: dict[str, str], sid: str) -> str:
    return samples_names.get(sid) or f"spell:{sid}"


def _all_names(samples: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for f in _fights(samples):
        for sid, name in (f.get("names") or {}).items():
            if name and not names.get(sid):
                names[sid] = str(name)
    return names


def aggregate_stealable(samples: dict[str, Any], min_removals: int = 2) -> dict[str, Any]:
    """The stealable-spells file (analysis/stealable.py's ``spells``
    list) plus an ``enrages`` list for soothes. A buff counts once it has
    been removed ``min_removals`` times across all sampled fights, so a
    single odd purge does not tag a spell forever. ``stealable`` is true
    when Spellsteal itself has removed it; a buff only ever purged is
    still listed (a Priest or Shaman wants it) but says so in its note."""
    names = _all_names(samples)
    stolen: dict[str, int] = {}
    purged: dict[str, int] = {}
    soothed: dict[str, int] = {}
    npcs: dict[str, str] = {}
    for f in _fights(samples):
        for buff, removers in (f.get("stolen") or {}).items():
            for remover, n in removers.items():
                rid = _int(remover)
                if rid in SPELLSTEAL_IDS:
                    stolen[buff] = stolen.get(buff, 0) + int(n)
                elif rid in SOOTHE_IDS:
                    soothed[buff] = soothed.get(buff, 0) + int(n)
                else:
                    purged[buff] = purged.get(buff, 0) + int(n)
        for buff, npc in (f.get("npcs") or {}).items():
            npcs.setdefault(buff, npc)

    spells = []
    for buff in sorted(set(stolen) | set(purged), key=lambda b: -(stolen.get(b, 0) + purged.get(b, 0))):
        total = stolen.get(buff, 0) + purged.get(buff, 0)
        if total < min_removals:
            continue
        parts = []
        if stolen.get(buff):
            parts.append(f"spellstolen {stolen[buff]}x")
        if purged.get(buff):
            parts.append(f"purged {purged[buff]}x")
        note = ", ".join(parts) + " in public Warcraft Logs keys"
        if npcs.get(buff):
            note += f" (cast by {npcs[buff]})"
        spells.append({
            "id": int(buff), "name": _name(names, buff), "note": note,
            "stealable": bool(stolen.get(buff)), "removals": total,
        })
    enrages = []
    for buff in sorted(soothed, key=lambda b: -soothed[b]):
        if soothed[buff] < min_removals:
            continue
        enrages.append({
            "id": int(buff), "name": _name(names, buff),
            "note": f"soothed {soothed[buff]}x in public Warcraft Logs keys"
                    + (f" (cast by {npcs[buff]})" if npcs.get(buff) else ""),
            "removals": soothed[buff],
        })
    return {"spells": spells, "enrages": enrages}


def aggregate_interrupt_evidence(
    samples: dict[str, Any], min_casts: int = 200, min_fights: int = 10,
) -> dict[str, Any]:
    """Two lists. ``proven``: ``{id: {name, interrupts}}`` -- every enemy
    spell that has ever been interrupted (kickable, full stop).
    ``never``: ``{id: {name, casts, fights}}`` -- spells that BEGAN a
    cast at least ``min_casts`` times across at least ``min_fights``
    fights and were never once interrupted. The bar is high on purpose:
    "nobody kicked it" is also how a low-priority cast looks, and
    cli._load_effective_interrupt_data explains why wrongly hiding a
    kickable cast is the costlier mistake."""
    names = _all_names(samples)
    interrupts: dict[str, int] = {}
    casts: dict[str, int] = {}
    fights_seen: dict[str, int] = {}
    for f in _fights(samples):
        for sid, n in (f.get("interrupted") or {}).items():
            interrupts[sid] = interrupts.get(sid, 0) + int(n)
        for sid, n in (f.get("begincasts") or {}).items():
            casts[sid] = casts.get(sid, 0) + int(n)
            fights_seen[sid] = fights_seen.get(sid, 0) + 1
    proven = {
        sid: {"name": _name(names, sid), "interrupts": n}
        for sid, n in sorted(interrupts.items(), key=lambda kv: -kv[1])
    }
    never = {
        sid: {"name": _name(names, sid), "casts": n, "fights": fights_seen.get(sid, 0)}
        for sid, n in sorted(casts.items(), key=lambda kv: -kv[1])
        if sid not in interrupts and n >= min_casts and fights_seen.get(sid, 0) >= min_fights
    }
    return {"proven": proven, "never": never}


def aggregate_dispels(samples: dict[str, Any], min_removals: int = 2) -> dict[str, Any]:
    """Dispellable debuffs as analysis/dispels.py's ``spells`` map:
    ``{id: {name, school, note, seen_dispelled}}``. The school is the
    intersection of what every remover seen could remove; a debuff whose
    removers leave more than one candidate is reported under
    ``ambiguous`` (``{id: {name, candidates, removals}}``) instead of
    guessed, and one removed fewer than ``min_removals`` times is
    skipped. Removers not in DISPELLER_SCHOOLS (a trinket, an
    unrecognised spell) contribute nothing to the school."""
    names = _all_names(samples)
    removals: dict[str, int] = {}
    candidates: dict[str, Optional[frozenset[str]]] = {}
    for f in _fights(samples):
        for debuff, removers in (f.get("dispelled") or {}).items():
            for remover, n in removers.items():
                schools = DISPELLER_SCHOOLS.get(_int(remover) or -1)
                removals[debuff] = removals.get(debuff, 0) + int(n)
                if schools is None:
                    continue
                prev = candidates.get(debuff)
                candidates[debuff] = schools if prev is None else (prev & schools or prev)
    spells: dict[str, Any] = {}
    ambiguous: dict[str, Any] = {}
    for debuff, n in sorted(removals.items(), key=lambda kv: -kv[1]):
        if n < min_removals:
            continue
        cand = candidates.get(debuff)
        if not cand:
            continue
        entry_name = _name(names, debuff)
        if len(cand) == 1:
            (school,) = tuple(cand)
            if school in SCHOOLS:
                spells[debuff] = {
                    "name": entry_name, "school": school,
                    "note": f"dispelled {n}x in public Warcraft Logs keys",
                    "seen_dispelled": True,
                }
            continue
        ambiguous[debuff] = {"name": entry_name, "candidates": sorted(cand), "removals": n}
    return {"spells": spells, "ambiguous": ambiguous}


def aggregate_killing_blows(samples: dict[str, Any]) -> dict[str, Any]:
    """``{spell_id: {name, deaths, levels: {level: deaths}}}`` sorted by
    deaths -- kept in the samples for a future "deadliest casts"
    section; nothing consumes it yet."""
    names = _all_names(samples)
    out: dict[str, Any] = {}
    for f in _fights(samples):
        level = str(f.get("level") or 0)
        for sid, n in (f.get("killed_by") or {}).items():
            entry = out.setdefault(sid, {"name": _name(names, sid), "deaths": 0, "levels": {}})
            entry["deaths"] += int(n)
            entry["levels"][level] = entry["levels"].get(level, 0) + int(n)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["deaths"]))
