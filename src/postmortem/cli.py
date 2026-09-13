"""postmortem command line interface."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

from .analysis.avoidable import AvoidableData
from .analysis.dispels import SCHOOLS as DISPEL_SCHOOLS, DispelData
from .analysis.interruptibility import InterruptibilityData
from .analysis.pulls import DEFAULT_PULL_GAP_S
from .analysis.run_analyzer import analyze_run
from .analysis.stealable import StealableData
from .chapters import write_chapter_files
from .clips import DEFAULT_PAD_S, FfmpegNotFoundError, clip_specs_for_chapters, cut_clips, load_chapters
from .combatlog.events import extra_spell_info, is_hostile_npc, spell_info
from .combatlog.parser import parse_file
from .combatlog.segmenter import RunSegment, segment_runs
from .mdt.decode import MDTDecodeError, decode_mdt_string
from .mdt.dungeon_data import DungeonDataStore
from .mdt.extract import LuaLiteralParser, LuaParseError, _find_assignment, write_dungeon_data
from .mdt.route import Route
from .recorder import Recorder
from .report.html import render_html
from .console import make_streams_safe, safe_print
from .report.text import render_text


def _load_route(route_arg: str) -> Route:
    """Accept an MDT export string directly or a path to a file holding one."""
    text = route_arg
    p = Path(route_arg)
    if p.exists() and p.is_file():
        text = p.read_text(encoding="utf-8").strip()
    try:
        preset = decode_mdt_string(text)
    except MDTDecodeError as exc:
        raise SystemExit(f"error: could not decode MDT string: {exc}")
    return Route.from_preset(preset)


def _load_store(path: Optional[str]) -> Optional[DungeonDataStore]:
    if not path:
        return None
    try:
        return DungeonDataStore.load(path)
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"error: could not load dungeon data {path}: {exc}")


def _load_avoidable(path: Optional[str]) -> Optional[AvoidableData]:
    """Load --avoidable-data. Like --dungeon-data, an explicitly-passed
    path that fails to load is a clear CLI error (SystemExit) -- the user
    typed a path, and silently ignoring a typo there would just be
    confusing. Omitting the flag entirely just skips avoidable-damage
    tagging (see analyze_run)."""
    if not path:
        return None
    try:
        return AvoidableData.load(path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: could not load avoidable-damage data {path}: {exc}")


def _load_dispel(path: Optional[str]) -> Optional[DispelData]:
    """Load --dispel-data (same posture as --avoidable-data: a typed path
    that fails to load is a clear error; None means "use the bundled
    list", which analyze_run resolves itself)."""
    if not path:
        return None
    try:
        return DispelData.load(path)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: could not load dispel data {path}: {exc}")


def _load_interruptibility(path: Optional[str]) -> Optional[InterruptibilityData]:
    """Load --interrupt-data. Like --avoidable-data/--dungeon-data, an
    explicitly-passed path that fails to load is a clear CLI error
    (SystemExit) -- the user typed a path, and silently ignoring a typo
    there would just be confusing. Omitting the flag entirely just skips
    addon-captured interruptibility data (see analyze_run)."""
    if not path:
        return None
    try:
        return InterruptibilityData.load(path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: could not load interrupt data {path}: {exc}")


def _load_effective_interrupt_data(
    path: Optional[str], learned_path: Optional[str | Path] = None
) -> Optional[InterruptibilityData]:
    """The interruptibility answers to analyze with: what this account's
    own logs have learned, with a loaded file (the bundled curated
    database, or an explicit --interrupt-data) layered on top.

    The curated file wins on conflict. The two sources can only ever
    disagree one way -- the file states a spell IS interruptible while
    the logs concluded it is not -- and of those two the file is the more
    trustworthy: it reflects how the ability was designed, whereas
    "we attempted it and never succeeded" also describes a group that is
    simply always a beat too late. Being wrong in that direction is also
    the kinder failure: showing a kickable cast that is being missed is
    the point of the report, while wrongly hiding one buries exactly the
    insight worth having. Real example (2026-09-05): Interrupting
    Cloudburst, guide-listed as interruptible, 4 failed attempts and no
    successes across 13 runs.

    Everything the logs learned that the file says nothing about -- which
    is every confirmed-uninterruptible spell outside the curated list --
    still applies. Either source may be absent; None means "no data at
    all", and callers fall back to their heuristic exactly as before.
    """
    from .analysis.interrupt_learning import InterruptObservations

    base = _load_interruptibility(path)
    if not learned_path:
        return base
    learned = InterruptObservations.load(learned_path).to_interrupt_data()
    if not learned["spells"]:
        return base
    learned_data = InterruptibilityData(spells={
        int(sid): dict(entry) for sid, entry in learned["spells"].items()
    })
    return learned_data if base is None else learned_data.merge(base)


def _load_community_spell_damage() -> Optional["SpellDamageData"]:
    """The bundled Warcraft Logs-derived per-spell damage-per-cast data
    (see analysis/spell_damage.py), or None when it was never built --
    it is a fallback for the kick-value estimate, never a requirement."""
    from .analysis.spell_damage import SpellDamageData
    from .bundled import bundled_spell_damage_path
    data = SpellDamageData.load(bundled_spell_damage_path())
    return data or None


def _load_stealable(path: Optional[str]) -> Optional[StealableData]:
    """Load --stealable-data. Like --avoidable-data, an explicitly-passed
    path that fails to load is a clear CLI error (SystemExit). Omitting
    the flag entirely just skips stealable-buff tagging (see
    analyze_run) -- there's no bundled/extracted fallback for this one,
    same posture as avoidable-damage tagging (see stealable.py's module
    docstring for why)."""
    if not path:
        return None
    try:
        return StealableData.load(path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: could not load stealable-spell data {path}: {exc}")


def _resolve_timer_par_ms(args: argparse.Namespace, challenge_map_id: Optional[int]) -> Optional[int]:
    """Resolve a par time (ms) for this run's dungeon, for the ``timer``
    report block (WP-C2) -- or None to omit that block entirely.

    Opt-in, like --avoidable-data/--dungeon-data: only attempted when
    --timer-data was given explicitly, or --raiderio already opted this
    invocation into network access (in which case a live static-data
    fetch is additionally attempted, but only if --expansion-id was also
    given -- we don't guess a "current" expansion id, see raiderio.py).
    Either way, a live fetch that fails or comes back unusable falls back
    to --timer-data / the bundled data/timers.json example seed rather
    than silently omitting the block -- this whole resolution step is a
    best-effort fallback source, not a user-typed path whose failure
    should be a CLI error (contrast --avoidable-data/--dungeon-data).
    """
    if not (args.timer_data or args.raiderio):
        return None

    from .raiderio import _default_fetcher as _rio_default_fetcher
    from .raiderio import resolve_timer_map

    if args.raiderio_no_cache:
        fetcher = _rio_default_fetcher
    else:
        from .cache import cached_fetcher
        fetcher = cached_fetcher(_rio_default_fetcher, filename="raiderio_static.json")

    timers = resolve_timer_map(
        expansion_id=args.expansion_id if args.raiderio else None,
        fetcher=fetcher,
        fallback_path=args.timer_data,
    )
    return timers.get(challenge_map_id)


def _pick_run(segments: Iterable[RunSegment], selector: str) -> RunSegment:
    """Select one run out of a (lazy) stream of segments.

    ``segments`` is driven incrementally rather than materialized up front,
    so a log with many runs never needs to hold more than one or two
    RunSegments' event lists in memory at a time:

    - ``--run N``: stops driving the generator the moment the Nth segment
      is yielded (later runs in the log are never parsed at all). Segments
      passed over on the way there have their ``events`` dropped as soon
      as they're superseded.
    - ``'last'``: still has to consume the whole log to know which segment
      is last, but only ever keeps the current best candidate's events —
      each earlier candidate's events are dropped the moment a newer
      segment arrives (this also means an abandoned run followed by a
      real completed run still resolves to the completed one, matching
      segment_runs's abandoned-run handling).
    """
    it = iter(segments)
    try:
        first = next(it)
    except StopIteration:
        raise SystemExit("error: no Mythic+ runs (CHALLENGE_MODE_START) found in this log")

    if selector == "last":
        candidate = first
        for seg in it:
            candidate.events = []
            candidate = seg
        return candidate

    try:
        idx = int(selector)
    except ValueError:
        raise SystemExit(f"error: --run must be a number or 'last', got {selector!r}")

    count = 1
    picked: Optional[RunSegment] = first if idx == count else None
    if picked is None:
        first.events = []
    for seg in it:
        count += 1
        if count == idx:
            picked = seg
            break
        seg.events = []
    if picked is None:
        for seg in it:  # exhaust the rest just to report an accurate total
            count += 1
            seg.events = []
        raise SystemExit(f"error: --run {idx} out of range (log has {count} runs)")
    return picked


def cmd_import_route(args: argparse.Namespace) -> int:
    route = _load_route(args.route)
    store = _load_store(args.dungeon_data)
    data = store.by_dungeon_idx(route.dungeon_idx) if store else None
    summary = route.summary(data)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(f"Route: {summary['name']}")
    print(f"Dungeon idx: {summary['dungeon_idx']}"
          + (f" ({summary.get('dungeon')})" if summary.get("dungeon") else ""))
    print(f"Pulls: {summary['pull_count']}")
    if summary.get("required_forces"):
        print(f"Planned forces: {summary['planned_forces']:.0f} / "
              f"{summary['required_forces']:.0f} ({summary.get('planned_forces_pct')}%)")
    for pull in summary["pulls"]:
        if "enemies" in pull:
            mobs = ", ".join(f"{e['n']}x {e['name']}" for e in pull["enemies"])
            pct = pull.get("forces_pct_cumulative")
            print(f"  pull {pull['pull']:>3} ({pct}%): {mobs}" if pct is not None
                  else f"  pull {pull['pull']:>3}: {mobs}")
        else:
            mobs = ", ".join(f"enemy#{k}x{v}" for k, v in pull["enemy_indices"].items())
            print(f"  pull {pull['pull']:>3}: {mobs}")
    if not store:
        print("\n(hint: pass --dungeon-data mdt_data.json to resolve enemy names "
              "— create it with `postmortem extract-data`)")
    return 0


def cmd_extract_data(args: argparse.Namespace) -> int:
    try:
        payload = write_dungeon_data(args.addon_path, args.output)
    except (OSError, ValueError, KeyError) as exc:
        # Every sibling subcommand reports a bad path or an unwritable
        # output this way; this one printed a stack trace.
        raise SystemExit(f"error: could not extract dungeon data: {exc}")
    n = len(payload["dungeons"])
    print(f"extracted {n} dungeons -> {args.output}")
    for d in sorted(payload["dungeons"].values(), key=lambda d: d["dungeon_idx"]):
        print(f"  [{d['dungeon_idx']:>3}] {d['name']}: {len(d['enemies'])} enemy types")
    return 0


def cmd_extract_interrupts(args: argparse.Namespace) -> int:
    """Extract the addon's PostmortemSpellDB SavedVariables table into
    the JSON shape InterruptibilityData.load() reads.

    Unlike extract-data (which walks a whole MDT addon folder of
    per-dungeon files), this reads one specific SavedVariables file --
    the addon declares two SavedVariables tables in one .toc
    (PostmortemDB, PostmortemSpellDB), so WoW writes both as
    separate top-level assignments into the same
    .../SavedVariables/Postmortem.lua file. We locate the
    PostmortemSpellDB assignment specifically and ignore
    PostmortemDB.
    """
    path = Path(args.savedvariables_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"error: could not read {path}: {exc}")

    pos = _find_assignment(text, r"PostmortemSpellDB\s*=\s*")
    if pos is None:
        raise SystemExit(
            f"error: no PostmortemSpellDB assignment found in {path} "
            "(wrong SavedVariables file? or the addon hasn't recorded "
            "any casts yet)"
        )

    warnings: list[str] = []
    parser = LuaLiteralParser(text, warnings)
    try:
        raw = parser.parse_value_at(pos)
    except LuaParseError as exc:
        raise SystemExit(
            f"error: could not parse PostmortemSpellDB in {path}: {exc}"
        )

    global_table = raw.get("global") if isinstance(raw, dict) else None
    if not isinstance(global_table, dict):
        global_table = {}

    spells: dict[str, Any] = {}
    n_interruptible = 0
    n_not = 0
    for spell_id, entry in global_table.items():
        if not isinstance(spell_id, int) or not isinstance(entry, dict):
            continue
        interruptible = bool(entry.get("interruptible"))
        spells[str(spell_id)] = {
            "name": entry.get("name") if isinstance(entry.get("name"), str)
            else f"spell:{spell_id}",
            "interruptible": interruptible,
        }
        if interruptible:
            n_interruptible += 1
        else:
            n_not += 1

    payload = {"spells": spells}
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    print(f"extracted {len(spells)} spells -> {args.output}")
    print(f"  {n_interruptible} known interruptible, {n_not} known uninterruptible")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def _read_savedvariables_table(path: Path, global_name: str) -> dict[Any, Any]:
    """The ``.global`` sub-table of one named SavedVariables assignment
    in a WoW SavedVariables .lua file (several addon tables share one
    file, so the assignment is located by name and the rest ignored).
    Raises SystemExit with a clear message on a missing file, a missing
    assignment, or unparseable Lua."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"error: could not read {path}: {exc}")

    pos = _find_assignment(text, global_name + r"\s*=\s*")
    if pos is None:
        raise SystemExit(
            f"error: no {global_name} assignment found in {path} "
            "(wrong SavedVariables file? or the addon hasn't recorded "
            "anything yet -- it writes at the end of a key, and WoW only "
            "saves SavedVariables on logout or /reload)"
        )

    warnings: list[str] = []
    parser = LuaLiteralParser(text, warnings)
    try:
        raw = parser.parse_value_at(pos)
    except LuaParseError as exc:
        raise SystemExit(f"error: could not parse {global_name} in {path}: {exc}")

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    global_table = raw.get("global") if isinstance(raw, dict) else None
    return global_table if isinstance(global_table, dict) else {}


def _challenge_map_to_dungeon_idx(dungeon_data: Optional[str]) -> dict[int, int]:
    """challenge-map id -> MDT dungeon_idx, from an explicit dungeon-data
    file or the bundled one. Empty (no per-dungeon grouping, spells still
    extracted) when neither exists -- the bridge is a nicety, not a
    requirement."""
    from .bundled import bundled_dungeon_data_path
    path = Path(dungeon_data) if dungeon_data else bundled_dungeon_data_path()
    if dungeon_data:
        store = _load_store(dungeon_data)
    elif path.is_file():
        try:
            store = DungeonDataStore.load(path)
        except (OSError, ValueError, KeyError):
            store = None
    else:
        store = None
    if store is None:
        return {}
    return {
        d.map_id: d.dungeon_idx
        for d in store.dungeons.values() if d.map_id is not None
    }


def cmd_extract_avoidable(args: argparse.Namespace) -> int:
    """Extract the addon's PostmortemAvoidableDB SavedVariables table --
    the avoidable-damage spell ids it harvests from Blizzard's own damage
    meter at the end of every key (see addon/Postmortem/
    AvoidableDatabase.lua) -- into the JSON shape AvoidableData.load()
    reads, and bundle it so every consumer picks it up automatically.

    The addon records, per spell id: name, the challenge-map ids of the
    dungeons it was seen in, how many keys it was seen in, and the total
    damage it did. The output's optional ``dungeons`` grouping is keyed by
    MDT dungeon_idx (what avoidable.py documents), so map ids are bridged
    through dungeon data when available.

    Merges into an existing output file by default: the list is meant to
    grow across accounts and machines, and a capture from one PC must
    never wipe spells only another has seen. ``--no-merge`` rebuilds from
    this one SavedVariables file alone.
    """
    path = Path(args.savedvariables_path)
    global_table = _read_savedvariables_table(path, "PostmortemAvoidableDB")
    map_to_idx = _challenge_map_to_dungeon_idx(args.dungeon_data)

    spells: dict[int, dict[str, Any]] = {}
    dungeons: dict[int, set[int]] = {}
    unmapped_maps: set[int] = set()

    if not args.no_merge and Path(args.output).is_file():
        try:
            existing = AvoidableData.load(args.output)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SystemExit(
                f"error: could not load existing {args.output} to merge into: {exc} "
                "(pass --no-merge to overwrite it)"
            )
        for sid, entry in existing.spells.items():
            spells[sid] = {"name": entry["name"], "note": entry.get("note")}
        for idx, ids in existing.dungeons.items():
            dungeons.setdefault(idx, set()).update(ids)
        merged_from = len(existing.spells)
    else:
        merged_from = 0

    n_captured = 0
    for spell_id, entry in global_table.items():
        if not isinstance(spell_id, int) or not isinstance(entry, dict):
            continue
        n_captured += 1
        name = entry.get("name") if isinstance(entry.get("name"), str) else None
        keys = entry.get("keys") if isinstance(entry.get("keys"), int) else None
        note = "captured from Blizzard's damage meter"
        if keys:
            note += f" (seen in {keys} key{'s' if keys != 1 else ''})"
        current = spells.get(spell_id)
        if current is None:
            spells[spell_id] = {"name": name or f"spell:{spell_id}", "note": note}
        else:
            # Keep a real name over a placeholder; keep whatever note the
            # existing file had unless it was ours (so re-runs refresh
            # the key count without clobbering a hand-written note).
            if name and current["name"].startswith("spell:"):
                current["name"] = name
            if not current.get("note") or str(current["note"]).startswith("captured from"):
                current["note"] = note

        maps = entry.get("maps")
        if isinstance(maps, dict):
            for map_id in maps:
                if not isinstance(map_id, int):
                    continue
                idx = map_to_idx.get(map_id)
                if idx is None:
                    unmapped_maps.add(map_id)
                    continue
                dungeons.setdefault(idx, set()).add(spell_id)

    payload = {
        "_comment": "Avoidable-damage spell ids, as classified by Blizzard's own "
                    "damage meter and captured in-game by the Postmortem addon "
                    "(PostmortemAvoidableDB). Built by `postmortem extract-avoidable`; "
                    "safe to hand-edit -- re-runs merge, they don't overwrite.",
        "spells": [
            {"id": sid, "name": spells[sid]["name"], "note": spells[sid].get("note")}
            for sid in sorted(spells)
        ],
        "dungeons": {
            str(idx): sorted(ids) for idx, ids in sorted(dungeons.items())
        },
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    print(f"extracted {n_captured} captured spells -> {args.output} "
          f"({len(spells)} total"
          + (f", merged with {merged_from} already in the file" if merged_from else "")
          + ")")
    if dungeons:
        print(f"  grouped under {len(dungeons)} dungeon(s)")
    if unmapped_maps:
        print(f"warning: {len(unmapped_maps)} challenge-map id(s) not in the dungeon "
              f"data ({', '.join(str(m) for m in sorted(unmapped_maps))}) -- their spells "
              "are in the list but not grouped by dungeon; refresh dungeon data "
              "(extract-data) and re-run", file=sys.stderr)

    if not args.no_bundle:
        from .bundled import bundled_avoidable_data_path
        bundled_path = bundled_avoidable_data_path()
        bundled_path.parent.mkdir(parents=True, exist_ok=True)
        if Path(args.output).resolve() != bundled_path.resolve():
            shutil.copyfile(args.output, bundled_path)
        print(f"also copied to the bundled package location: {bundled_path}")
    return 0


def _add_interrupt_spell(spells: dict[str, Any], spell_id: int, name: str) -> bool:
    """Record one confirmed-interruptible spell. Returns True if this was
    a *conflicting* duplicate -- the same id already recorded under a
    different name, which would be a real data problem (spell ids don't
    get reused for unrelated effects). The first name wins either way, so
    a conflict is reported, never fatal. The same id arriving twice under
    the same name is just an ability shared by several NPCs: not a
    conflict, silently ignored."""
    existing = spells.get(str(spell_id))
    if existing is None:
        spells[str(spell_id)] = {"name": name, "interruptible": True}
        return False
    return existing["name"] != name


def cmd_build_interrupt_data(args: argparse.Namespace) -> int:
    """Convert a community "which mechanics matter in this dungeon" source
    file (schema: albvar/mplus-interrupts, MIT-licensed -- see
    ``docs/THIRD_PARTY_NOTICES.md``) into the JSON shape
    ``InterruptibilityData.load()`` reads, and (unless --no-bundle) also
    writes it to this package's own ``data/interrupt_data.json`` so every
    consumer (CLI, desktop app, Watch Live, the public site) picks it up
    with zero configuration -- see bundled.py.

    The addon's own live capture (``extract-interrupts``) is permanently
    dead: Patch 12.0.0 made the client's interruptible flag unreadable by
    addon code (see InterruptDatabase.lua's own comment) -- this is its
    replacement data source. Deliberately reads a LOCAL file, not a live
    network fetch: like extract-data, refreshing this is a manual,
    once-per-season step (the source repo's own README describes the same
    per-season refresh cadence), not something this project's offline,
    stdlib-only runtime does for itself.

    Source-format nuance worth knowing (see interruptibility.py's module
    docstring, and CLI help below): this source only ever tags an ability
    as "here's an interrupt worth using" -- it never says "this cannot be
    interrupted". So every spell this produces is written with
    ``interruptible: true``; nothing ever gets ``false`` from this path.
    That's still a real improvement over the plain "kicked at least once"
    heuristic (see analysis/run_analyzer.py's _enemy_cast_summary): a
    documented interrupt that lands zero kicks all key now correctly
    counts against kick efficiency instead of being invisible.
    """
    try:
        with open(args.source, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise SystemExit(f"error: could not read {args.source}: {exc}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {args.source} is not valid JSON: {exc}")

    dungeons = payload.get("dungeons")
    if not isinstance(dungeons, list):
        raise SystemExit(
            f"error: {args.source} doesn't look like an mplus-interrupts "
            "export (no top-level \"dungeons\" list)"
        )

    # Optional: resolve ability NAMES against spell ids actually seen in
    # real combat logs, instead of trusting the source's own ids.
    # Confirmed necessary (2026-09-05): a guide listed Fel Missiles as
    # 1216570 -- the damage component -- where the interruptible cast is
    # 1216571, so a naive import silently matches nothing. Names are what
    # a guide gets reliably right; ids are what the log gets right.
    #
    # Resolving by name alone is not enough either. One guide name routinely
    # maps to several ids in the logs, and only some of them are the
    # interruptible cast: Arc Lightning resolved to 1297778 (12 kicks) AND
    # 1305810 (32 casts, never once kicked); Envenom, Toxic Atrophy and
    # Mirror Images likewise (2026-09-06, 17 real runs). Guides also flag
    # things as "interrupt" whose own prose says "use a defensive", which in
    # every observed case meant nobody has ever kicked it. So a resolved id
    # is only accepted if the logs contain at least one SPELL_INTERRUPT that
    # actually stopped it -- proof, the same standard interrupt_learning.py
    # uses. An id that has never been kicked is omitted, not guessed; the
    # analyzer's own "kicked at least once" fallback picks it up the first
    # time somebody lands one, and the next refresh will then include it.
    name_to_ids: dict[str, set[int]] = {}
    kicked_ids: set[int] = set()
    for log in args.resolve_from or []:
        try:
            for event in parse_file(log):
                if event.name == "SPELL_INTERRUPT":
                    stopped = extra_spell_info(event)
                    if stopped is not None:
                        kicked_ids.add(stopped.spell_id)
                    continue
                if event.name not in ("SPELL_CAST_START", "SPELL_CAST_SUCCESS"):
                    continue
                if not is_hostile_npc(event.source_flags):
                    continue
                sp = spell_info(event)
                if sp is not None:
                    name_to_ids.setdefault(sp.spell_name.casefold(), set()).add(sp.spell_id)
        except OSError as exc:
            print(f"warning: skipping {log}: {exc}", file=sys.stderr)

    spells: dict[str, Any] = {}
    skipped_conflicts = 0
    unresolved: list[str] = []
    never_kicked: list[str] = []
    for dungeon in dungeons:
        if not isinstance(dungeon, dict):
            continue
        for ability in dungeon.get("abilities", []) or []:
            if not isinstance(ability, dict) or ability.get("category") != "interrupt":
                continue
            name = ability.get("spell_name") or ""
            if name_to_ids:
                # Every id this ability's NAME actually had in the logs --
                # plural on purpose, since the same ability cast by
                # different NPCs legitimately carries different ids.
                seen = sorted(name_to_ids.get(name.casefold(), ()))
                if not seen:
                    unresolved.append(f"{dungeon.get('name', '?')}: {name}")
                    continue
                resolved = [sid for sid in seen if sid in kicked_ids]
                for sid in seen:
                    if sid not in kicked_ids:
                        never_kicked.append(f"{dungeon.get('name', '?')}: {name} ({sid})")
                if not resolved:
                    continue
            else:
                spell_id = ability.get("spell_id")
                if not isinstance(spell_id, int):
                    continue
                resolved = [spell_id]
            for spell_id in resolved:
                label = name or f"spell:{spell_id}"
                if _add_interrupt_spell(spells, spell_id, label):
                    skipped_conflicts += 1
                    print(
                        f"warning: spell {spell_id} tagged as both "
                        f"{spells[str(spell_id)]['name']!r} and {label!r} "
                        f"-- keeping the first", file=sys.stderr,
                    )
            continue

    out_payload = {
        "spells": spells,
        # Carried through for traceability -- which season/source/version
        # this bundled file was built from, same spirit as dungeon_data.json's
        # own "source"/"generated_at" fields.
        "source": payload.get("source"),
        "season": payload.get("season"),
        "source_version": payload.get("version"),
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(out_payload, fh, indent=1)
    for miss in unresolved:
        print(f"warning: no spell id in the logs for {miss} -- omitted rather "
              f"than guessed", file=sys.stderr)
    for miss in never_kicked:
        print(f"warning: {miss} was cast in the logs but never once "
              f"interrupted -- omitted until a kick on it is observed",
              file=sys.stderr)
    print(f"built {len(spells)} confirmed-interruptible spells -> {args.output}"
          + (f" ({skipped_conflicts} conflicting duplicate(s) skipped)" if skipped_conflicts else ""))

    if not args.no_bundle:
        from .bundled import bundled_interrupt_data_path
        bundled_path = bundled_interrupt_data_path()
        bundled_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.output, bundled_path)
        print(f"also copied to the bundled package location: {bundled_path}")
    return 0


def cmd_build_dispel_data(args: argparse.Namespace) -> int:
    """Build the dispellable-debuff list (analysis/dispels.py) from the
    same Method.gg-derived source file build-interrupt-data reads: its
    ``dispel-<school>`` rows. Like that command, ``--resolve-from`` real
    logs resolves each ability's NAME to the id(s) that actually landed
    on group players as a DEBUFF -- Method's published ids often point at
    the damage component, not the debuff -- and, when logs are given,
    only names seen in them are kept. Without logs the published ids are
    used as-is. Bundled unless --no-bundle."""
    try:
        with open(args.source, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: could not read {args.source}: {exc}")
    dungeons = payload.get("dungeons") if isinstance(payload, dict) else None
    if not isinstance(dungeons, list):
        raise SystemExit(f"error: {args.source} has no 'dungeons' list")

    from .combatlog.events import is_group_player
    name_to_ids: dict[str, set[int]] = {}
    dispelled_ids: set[int] = set()
    for log in args.resolve_from or []:
        try:
            for event in parse_file(log):
                if event.name == "SPELL_DISPEL":
                    removed = extra_spell_info(event)
                    if removed is not None and not is_hostile_npc(event.dest_flags):
                        dispelled_ids.add(removed.spell_id)
                    continue
                if event.name != "SPELL_AURA_APPLIED":
                    continue
                if not is_hostile_npc(event.source_flags) or not is_group_player(event.dest_flags):
                    continue
                if len(event.params) <= 11 or event.params[11] != "DEBUFF":
                    continue
                sp = spell_info(event)
                if sp is not None:
                    name_to_ids.setdefault(sp.spell_name.casefold(), set()).add(sp.spell_id)
        except OSError as exc:
            print(f"warning: skipping {log}: {exc}", file=sys.stderr)

    spells: dict[str, Any] = {}
    unresolved: list[str] = []
    for dungeon in dungeons:
        if not isinstance(dungeon, dict):
            continue
        for ability in dungeon.get("abilities", []) or []:
            if not isinstance(ability, dict):
                continue
            category = str(ability.get("category") or "")
            if not category.startswith("dispel-"):
                continue
            school = category[len("dispel-"):].lower()
            if school not in DISPEL_SCHOOLS:
                continue
            name = ability.get("spell_name") or ""
            if name_to_ids:
                resolved = sorted(name_to_ids.get(name.casefold(), ()))
                if not resolved:
                    unresolved.append(f"{dungeon.get('name', '?')}: {name}")
                    continue
            else:
                spell_id = ability.get("spell_id")
                if not isinstance(spell_id, int):
                    continue
                resolved = [spell_id]
            for spell_id in resolved:
                key = str(spell_id)
                if key in spells and spells[key]["school"] != school:
                    print(f"warning: spell {spell_id} ({name}) tagged as both "
                          f"{spells[key]['school']} and {school} -- keeping the first",
                          file=sys.stderr)
                    continue
                spells.setdefault(key, {
                    "name": name or f"spell:{spell_id}", "school": school,
                    "note": ability.get("notes"),
                    "seen_dispelled": spell_id in dispelled_ids,
                })

    out_payload = {
        "spells": spells,
        "source": payload.get("source"),
        "season": payload.get("season"),
        "source_version": payload.get("version"),
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(out_payload, fh, indent=1)
    for miss in unresolved:
        print(f"warning: no debuff named {miss} landed on a player in the logs "
              "-- omitted rather than guessed", file=sys.stderr)
    by_school = {}
    for e in spells.values():
        by_school[e["school"]] = by_school.get(e["school"], 0) + 1
    print(f"built {len(spells)} dispellable debuffs -> {args.output} "
          + ", ".join(f"{n} {sch}" for sch, n in sorted(by_school.items())))

    if not args.no_bundle:
        from .bundled import bundled_dispel_data_path
        bundled_path = bundled_dispel_data_path()
        bundled_path.parent.mkdir(parents=True, exist_ok=True)
        if Path(args.output).resolve() != bundled_path.resolve():
            shutil.copyfile(args.output, bundled_path)
        print(f"also copied to the bundled package location: {bundled_path}")
    return 0


def cmd_build_talent_data(args: argparse.Namespace) -> int:
    """Build the talent-node -> talent-name map from Blizzard's own
    talent-tree API and bundle it (see talents.py for what reads it).

    Needs BNET_CLIENT_ID/BNET_CLIENT_SECRET in the environment. One
    request per specialization plus one for the index -- a few dozen
    calls, well inside Blizzard's rate limits.
    """
    from .blizzardapi import BlizzardApi, BlizzardApiError, build_talent_nodes, credentials_from_env

    try:
        client_id, client_secret = credentials_from_env()
        api = BlizzardApi(client_id, client_secret, region=args.region)
        print(f"fetching talent trees from the {args.region} Game Data API…")
        payload = build_talent_nodes(api, echo=print)
    except BlizzardApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not payload["nodes"]:
        print("error: no talent nodes were returned", file=sys.stderr)
        return 1

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    choice_nodes = sum(1 for n in payload["nodes"].values() if len(n["options"]) > 1)
    print(f"wrote {args.output}: {payload['node_count']} nodes "
          f"({choice_nodes} of them choice nodes)")

    if not args.no_bundle:
        from .bundled import bundled_talent_data_path
        bundled_path = bundled_talent_data_path()
        bundled_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.output, bundled_path)
        print(f"also copied to the bundled package location: {bundled_path}")
    return 0


def cmd_build_spell_damage(args: argparse.Namespace) -> int:
    """Sample public Warcraft Logs keystone runs for per-spell damage per
    completed cast, by key level, and bundle it -- see wcl.py for the
    client and analysis/spell_damage.py for what the data is for.

    Two files: ``--samples`` holds raw per-fight totals and is the unit
    of resumability (a fight already in it is never fetched again, so a
    build cut short by the hourly points budget just continues on the
    next run); ``--output`` is the aggregated, bundled result rebuilt
    from the samples every time. Damage per cast is
    ``sum(damage) / sum(casts)`` over every sampled fight at that level,
    so one unusual fight cannot dominate.
    """
    import os
    from .analysis.spell_damage import SpellDamageData
    from .wcl import (WCLClient, WCLError, fetch_fight_tables, find_mplus_zone,
                      iter_keystone_fights)

    samples_path = Path(args.samples)
    try:
        samples = json.loads(samples_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        samples = {"fights": {}}
    fights: dict[str, Any] = samples.setdefault("fights", {})
    zone_id, zone_name = samples.get("zone_id"), str(samples.get("zone_name") or "")
    fetched = skipped = 0
    stopped_for_budget = False
    client = None

    if args.offline:
        if not fights:
            raise SystemExit(f"error: --offline needs an existing samples file; "
                             f"{samples_path} has no fights")
        print(f"offline: re-aggregating {len(fights)} sampled fight(s) from {samples_path}")
    else:
        try:
            client = WCLClient(os.environ.get("WCL_CLIENT_ID", ""),
                               os.environ.get("WCL_CLIENT_SECRET", ""))
        except WCLError as exc:
            raise SystemExit(f"error: {exc} -- register an API client at "
                             "https://www.warcraftlogs.com/api/clients/ and export both")

    try:
      if client is not None:
        if args.zone_id:
            zone_id, zone_name = int(args.zone_id), ""
        else:
            zone_id, zone_name = find_mplus_zone(client)
        if samples.get("zone_id") not in (None, zone_id):
            print(f"warning: samples file is for zone {samples.get('zone_id')}, "
                  f"now sampling zone {zone_id} -- keeping both", file=sys.stderr)
        samples["zone_id"] = zone_id
        if zone_name:
            samples["zone_name"] = zone_name
        print(f"zone {zone_id} {zone_name}".rstrip())

        # how many fights we already hold per (encounter, level)
        have: dict[tuple[int, int], int] = {}
        for f in fights.values():
            key = (int(f.get("encounter_id") or 0), int(f.get("level") or 0))
            have[key] = have.get(key, 0) + 1

        for fight in iter_keystone_fights(client, zone_id, max_pages=args.max_pages):
            if not (args.min_level <= fight["level"] <= args.max_level):
                continue
            key = (fight["encounter_id"], fight["level"])
            fkey = f"{fight['code']}:{fight['fight_id']}"
            if fkey in fights:
                skipped += 1
                continue
            if have.get(key, 0) >= args.per_level:
                continue
            left = client.points_left()
            if left is not None and left < args.reserve_points:
                stopped_for_budget = True
                break
            tables = fetch_fight_tables(client, fight["code"], fight["fight_id"])
            fights[fkey] = {
                "level": fight["level"],
                "encounter_id": fight["encounter_id"],
                "encounter": fight["encounter"],
                "kill": fight["kill"],
                "damage": {str(k): v for k, v in tables["damage"].items()},
                "healing": {str(k): v for k, v in tables["healing"].items()},
                "casts": {str(k): v for k, v in tables["casts"].items()},
                "names": {str(k): v for k, v in tables["names"].items()},
            }
            have[key] = have.get(key, 0) + 1
            fetched += 1
            # save as we go: the budget can run out mid-loop
            samples_path.write_text(json.dumps(samples, indent=0), encoding="utf-8")
    except WCLError as exc:
        samples_path.write_text(json.dumps(samples, indent=0), encoding="utf-8")
        raise SystemExit(f"error: {exc} (samples so far kept in {samples_path})")

    samples_path.write_text(json.dumps(samples, indent=0), encoding="utf-8")

    keep: Optional[set[int]] = None
    if not args.all_spells:
        from .bundled import bundled_interrupt_data_path
        keep = set(InterruptibilityData.load(bundled_interrupt_data_path()).spells) \
            if bundled_interrupt_data_path().is_file() else set()

    data = aggregate_spell_damage_samples(samples, keep)
    data.source = "warcraftlogs.com"
    data.season = zone_name or f"zone {zone_id}"
    data.version = __import__("datetime").date.today().isoformat()
    data.save(args.output, comment=(
        "Built by `postmortem build-spell-damage` from public Warcraft Logs "
        "reports -- see analysis/spell_damage.py and wcl.py. Per enemy spell "
        "and keystone level: total damage taken by the group / enemy healing "
        "and completed-cast count over the sampled fights, so the kick-value "
        "estimate has a number for a spell that never landed in a run."))
    levels = sorted({lv for e in data.spells.values() for lv in e["levels"]})
    if client is not None:
        print(f"fetched {fetched} new fight(s), {skipped} already sampled; "
              f"{len(fights)} sampled fights total")
    print(f"built {len(data.spells)} spell(s) across key levels "
          f"{levels[0] if levels else '-'}..{levels[-1] if levels else '-'} -> {args.output}")
    if client is not None and client.rate_limit:
        rl = client.rate_limit
        print(f"API points: {rl.get('pointsSpentThisHour')}/{rl.get('limitPerHour')} "
              f"used this hour, reset in {rl.get('pointsResetIn')}s")
    if stopped_for_budget:
        print("stopped early to stay inside the hourly points budget -- rerun "
              "after the reset to keep sampling (already-sampled fights are skipped)")
    if not args.no_bundle:
        from .bundled import bundled_spell_damage_path
        bundled_path = bundled_spell_damage_path()
        bundled_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.output, bundled_path)
        print(f"also copied to the bundled package location: {bundled_path}")
    return 0


def aggregate_spell_damage_samples(samples: dict[str, Any],
                                   keep: Optional[set[int]] = None) -> "SpellDamageData":
    """Fold a build-spell-damage samples file into SpellDamageData: per
    spell and key level, total damage/healing and completed casts summed
    over every sampled fight. ``keep`` restricts to those spell ids (None
    keeps all). A spell with casts but no damage row still counts (it
    did zero damage), a damage row with no cast count is skipped --
    without a denominator there is no per-cast number."""
    from .analysis.spell_damage import SpellDamageData

    data = SpellDamageData()
    for f in (samples.get("fights") or {}).values():
        if not isinstance(f, dict):
            continue
        try:
            level = int(f.get("level") or 0)
        except (TypeError, ValueError):
            continue
        names = f.get("names") or {}
        damage = f.get("damage") or {}
        healing = f.get("healing") or {}
        casts = f.get("casts") or {}
        # A cast and its damage can log under different ids with the same
        # name (Fel Missiles: cast 1216571, damage 1216570 -- see
        # stats._estimate_kick_value). When the cast id has no damage row
        # of its own, take the same-named damage/healing row instead.
        dmg_by_name: dict[str, str] = {}
        heal_by_name: dict[str, str] = {}
        for sid in damage:
            if sid not in casts and names.get(sid):
                dmg_by_name.setdefault(str(names[sid]).casefold(), sid)
        for sid in healing:
            if sid not in casts and names.get(sid):
                heal_by_name.setdefault(str(names[sid]).casefold(), sid)
        for sid, n_casts in casts.items():
            try:
                spell_id = int(sid)
                n = int(n_casts)
            except (TypeError, ValueError):
                continue
            if n <= 0 or (keep is not None and spell_id not in keep):
                continue
            name = str(names.get(sid) or f"spell:{spell_id}")
            dmg = int(damage.get(sid) or 0)
            heal = int(healing.get(sid) or 0)
            if not dmg:
                dmg = int(damage.get(dmg_by_name.get(name.casefold(), ""), 0) or 0)
            if not heal:
                heal = int(healing.get(heal_by_name.get(name.casefold(), ""), 0) or 0)
            data.add(spell_id, name, level, damage=dmg, healing=heal, casts=n)
    return data


def cmd_learn_interrupts(args: argparse.Namespace) -> int:
    """Fold one or more combat logs into an accumulated interruptibility
    file learned from real play (see analysis/interrupt_learning.py).

    Exists so an existing pile of logs can seed the file in one go
    instead of only learning from runs analyzed after the feature
    landed. Re-running over the same log is safe in the sense that it
    only ever adds evidence -- but it does double-count that log's
    observations, so point it at each log once.
    """
    from .analysis.interrupt_learning import InterruptObservations, observe_events

    acc = InterruptObservations.load(args.output)
    before = len(acc.spells)
    for path in args.logs:
        try:
            for segment in segment_runs(parse_file(path)):
                acc.merge(observe_events(segment.events))
        except OSError as exc:
            print(f"warning: skipping {path}: {exc}", file=sys.stderr)
            continue
        print(f"  read {path}")
    acc.save(args.output)

    data = acc.to_interrupt_data(min_attempts=args.min_attempts)["spells"]
    yes = sum(1 for v in data.values() if v["interruptible"])
    no = len(data) - yes
    print(f"learned from {len(args.logs)} log(s) -> {args.output}")
    print(f"  {len(acc.spells)} spells observed ({len(acc.spells) - before} new)")
    print(f"  {yes} confirmed interruptible, {no} confirmed NOT interruptible "
          f"(>= {args.min_attempts} failed attempts, never once interrupted)")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    # Consumed one segment at a time (no list(...)): each RunSegment's
    # events are only needed for summary() and are eligible for GC as soon
    # as the next run starts, instead of the whole file's runs being held
    # in memory at once.
    found = False
    for i, seg in enumerate(segment_runs(parse_file(args.log)), start=1):
        found = True
        s = seg.summary()
        seg.events = []
        state = "timed" if s["timed"] else (
            "over timer" if s["completed"] else
            # A size-capped run only looks abandoned; say which it is.
            ("partial (size limit)" if s.get("truncated") else "incomplete"))
        mins = s["wall_duration_s"] / 60
        print(f"{i:>3}. {s['zone']} +{s['keystone_level']}  [{state}]"
              f"  {mins:.1f} min, {s['event_count']} events")
    if not found:
        print("no Mythic+ runs found in this log")
        return 1
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    route = _load_route(args.route) if args.route else None
    store = _load_store(args.dungeon_data)
    # An explicit --avoidable-data wins; otherwise the bundled list built
    # from the addon's damage-meter capture (None if never built).
    avoidable = _load_avoidable(args.avoidable_data) or AvoidableData.load_bundled()
    interrupt_data = _load_interruptibility(args.interrupt_data)
    stealable = _load_stealable(args.stealable_data)
    dispel_data = _load_dispel(getattr(args, "dispel_data", None))
    # Stream segments directly into _pick_run rather than list(...)-ing them
    # all up front — for a numeric --run it stops parsing once the wanted
    # run is found, and never retains other runs' event lists either way.
    segment = _pick_run(segment_runs(parse_file(args.log)), args.run)

    par_ms = _resolve_timer_par_ms(args, segment.challenge_map_id)

    report = analyze_run(
        segment,
        route=route,
        store=store,
        avoidable=avoidable,
        interrupt_data=interrupt_data,
        stealable=stealable,
        dispel_data=dispel_data,
        pull_gap_seconds=args.pull_gap,
        full_cast_timeline=not args.no_cast_timeline,
        death_penalty_s=args.death_penalty,
        par_ms=par_ms,
        spell_damage_history_path=args.spell_damage_history,
        community_spell_damage=_load_community_spell_damage(),
    )

    if args.raiderio:
        from .raiderio import _default_fetcher, enrich_report

        if args.raiderio_no_cache:
            fetcher = _default_fetcher
        else:
            from .cache import cached_fetcher
            fetcher = cached_fetcher(_default_fetcher)
        n = enrich_report(report, args.raiderio, fetcher=fetcher)
        print(f"raider.io: enriched {n} players", file=sys.stderr)

    formats = [f.strip() for f in args.format.split(",") if f.strip()]
    # Validate the WHOLE list first. Checking inside the write loop meant
    # "--format json,bogus" wrote the JSON, then exited with an error --
    # half the work done, with no way to tell from the exit code.
    unknown = [f for f in formats if f not in ("text", "json", "html")]
    if unknown:
        raise SystemExit(f"error: unknown format {unknown[0]!r} (text, json, html)")
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    base = _report_basename(report)

    wrote = []
    json_path = None
    html_path = None
    for fmt in formats:
        if fmt == "text":
            text = render_text(report)
            if out_dir:
                path = out_dir / f"{base}.txt"
                path.write_text(text, encoding="utf-8")
                wrote.append(path)
            else:
                print(text)
        elif fmt == "json":
            payload = json.dumps(report, indent=1)
            if out_dir:
                path = out_dir / f"{base}.json"
                path.write_text(payload, encoding="utf-8")
                wrote.append(path)
                json_path = path
            else:
                print(payload)
        elif fmt == "html":
            html = render_html(report)
            if out_dir:
                path = out_dir / f"{base}.html"
                path.write_text(html, encoding="utf-8")
                wrote.append(path)
                html_path = path
            else:
                # Was always written to ./<base>.html, silently clobbering
                # any file of that name; text and json both print instead.
                print(html)
    for path in wrote:
        print(f"wrote {path}", file=sys.stderr)

    if args.history_db:
        from .history.store import ingest as ingest_history

        run_id = ingest_history(
            report, args.history_db, source_path=json_path, html_path=html_path,
        )
        print(f"history: run {run_id} -> {args.history_db}", file=sys.stderr)

    if args.upload:
        # Local import: keeps upload.py's urllib/secrets usage (and its
        # first-use token-file write) off the hot path for every other
        # `analyze` invocation that doesn't pass --upload.
        from .upload import upload_report

        result = upload_report(report, args.upload, token=args.upload_token)
        if result.get("ok"):
            print(f"uploaded: {args.upload.rstrip('/')}{result['url']}")
        else:
            # Uploading is a best-effort bonus step, same philosophy as
            # --raiderio enrichment above: a failure here (offline, the
            # server rejected it, ...) never changes cmd_analyze's exit
            # code or blocks its normal output -- analysis succeeding is
            # the primary outcome.
            print(f"upload failed: {result.get('error')}", file=sys.stderr)

    return 0


def _report_basename(report: dict) -> str:
    import re as _re
    import time as _time

    zone = report["run"].get("zone") or "run"
    zone = _re.sub(r"[^A-Za-z0-9]+", "", zone)
    level = report["run"].get("keystone_level") or "x"
    stamp = _time.strftime("%Y%m%d-%H%M%S", _time.localtime(report["run"]["start_ts"]))
    return f"{stamp}_{zone}_{level}"


def _scan_report_files(directory: str | Path) -> Iterable[tuple[Path, dict]]:
    """Find report JSON files under ``directory``, yielding ``(path, report)``.

    Mirrors ``report.index.collect_reports``'s own scan (same glob, same
    tolerant skip of unreadable/malformed/foreign JSON) so `index --db`
    ingests exactly the files a plain JSON-scan `index` run would have
    picked up. Kept here rather than in report/index.py so that module's
    scan loop isn't duplicated *and* modified in two places at once.
    """
    root = Path(directory)
    for path in sorted(root.rglob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, ValueError):
            continue
        # Skip non-dict top-level JSON (e.g. the recorder's
        # <run>.chapters.json sidecar, a top-level list) before .get() --
        # see the matching guard in report.index.collect_reports for why.
        if not isinstance(report, dict):
            continue
        run = report.get("run")
        if not isinstance(run, dict) or "zone" not in run:
            continue  # not one of our reports
        yield path, report


def cmd_index(args: argparse.Namespace) -> int:
    from .report.index import build_index, collect_reports, render_index

    if args.db:
        from .history.store import Store

        n_ingested = 0
        with Store(args.db) as store:
            for path, report in _scan_report_files(args.directory):
                html_sibling = path.with_suffix(".html")
                store.ingest(
                    report,
                    source_path=path,
                    html_path=html_sibling if html_sibling.exists() else None,
                )
                n_ingested += 1
            rows = store.query_runs()

        out = Path(args.output) if args.output else Path(args.directory) / "index.html"
        out.write_text(render_index(rows), encoding="utf-8")
        print(f"indexed {n_ingested} runs into {args.db} -> {out}")
        if not rows:
            print("(no report JSON files found — analyze runs with "
                  "--format json,html first)", file=sys.stderr)
        return 0

    rows = collect_reports(args.directory)
    out = build_index(args.directory, args.output)
    print(f"indexed {len(rows)} runs -> {out}")
    if not rows:
        print("(no report JSON files found — analyze runs with "
              "--format json,html first)", file=sys.stderr)
    return 0


def _write_recorded_reports(
    run, route, store, pull_gap_seconds: float = DEFAULT_PULL_GAP_S, avoidable=None,
    interrupt_data=None, stealable=None, learned_path=None, enrich=None,
    spell_damage_history_path=None,
) -> Optional[dict]:
    """Analyze one recorded run's log slice and write its JSON/HTML/text
    reports, plus the chapters sidecars (``<run>.chapters.json`` /
    ``<run>.vtt`` -- see :mod:`postmortem.chapters`, WP-D2) next to
    the recorded ``.txt`` slice. ``run.started_at`` (the wall-clock moment
    the recorder started this run, essentially simultaneous with a shell
    hook or native OBS actually starting to record -- see
    ``recorder.RecordedRun``) is used as the chapters' video-start
    reference.

    Chapters/VTT are written unconditionally alongside the other
    ``--analyze`` outputs (not gated on ``--obs``/``--on-run-start`` being
    configured): they're harmless even with no matching video, consistent
    with how JSON/HTML/text are already all written together with no
    individual opt-out, and a later per-pull clip-cutting work package
    needs this file regardless of how (or whether, at record time) the
    video was actually produced.

    Kept as a standalone module-level function (rather than inline in
    ``cmd_record``) so it can be exercised directly in tests without
    driving the recorder's blocking ``watch()`` loop through a full CLI
    invocation -- see ``TestRecorder`` in ``tests/test_cli_and_tools.py``.

    Returns the analyzed report dict (so callers -- ``cmd_record``'s
    ``--upload`` handling, the desktop app's watch mode -- can act on it
    further without re-parsing the slice), or ``None`` if it contained no
    complete Mythic+ run segment (shouldn't normally happen, since
    ``Recorder`` only calls ``on_run_complete`` after a real
    ``CHALLENGE_MODE_END``, but every other "parse a log" path in this
    codebase makes the same defensive check).
    """
    # list(...) is fine here: run.path is a per-run recorded slice
    # (Recorder opens a fresh file per CHALLENGE_MODE_START), so this
    # never holds more than one run's events regardless.
    segments = list(segment_runs(parse_file(run.path)))
    if not segments:
        return None
    if learned_path:
        # Fold this run's own evidence into the accumulated
        # interruptibility file (see analysis/interrupt_learning.py).
        # Best-effort, like the enrich hook below: a cache that can't be
        # written must never cost the run its reports.
        try:
            from .analysis.interrupt_learning import update_from_events
            update_from_events(segments[-1].events, learned_path)
        except Exception as exc:
            print(f"warning: could not update learned interrupts: {exc}", file=sys.stderr)
    # Zero-config avoidable-damage tagging: the bundled list (built by
    # `extract-avoidable` from the addon's damage-meter capture) unless
    # the caller loaded its own.
    if avoidable is None:
        avoidable = AvoidableData.load_bundled()
    report = analyze_run(segments[-1], route=route, store=store,
                         avoidable=avoidable, interrupt_data=interrupt_data,
                         stealable=stealable, pull_gap_seconds=pull_gap_seconds,
                         spell_damage_history_path=spell_damage_history_path,
                         community_spell_damage=_load_community_spell_damage())
    if enrich is not None:
        # A caller-supplied pass over the finished report before anything
        # is rendered or written -- the desktop app uses it to embed the
        # dungeon's map art from the user's own MDT install (mapart.py),
        # which has to land in the dict *before* render_html() below sees
        # it. Best-effort by contract: a raising hook must not cost the
        # run its reports.
        try:
            enrich(report)
        except Exception as exc:
            print(f"warning: report enrichment failed: {exc}", file=sys.stderr)
    base = run.path.with_suffix("")
    Path(f"{base}.json").write_text(json.dumps(report, indent=1),
                                    encoding="utf-8")
    Path(f"{base}.html").write_text(render_html(report), encoding="utf-8")
    write_chapter_files(report, run.started_at, base)
    # never let a console that can't show "≈" fail a run whose reports are
    # already on disk (see postmortem.console)
    safe_print(render_text(report))
    safe_print(f"wrote {base}.json / {base}.html / {base}.chapters.json / {base}.vtt",
               file=sys.stderr)
    return report


def cmd_record(args: argparse.Namespace) -> int:
    if args.upload and not args.analyze:
        # There is nothing to upload without a report: the analysis step
        # returns immediately, so the flag was accepted and ignored.
        raise SystemExit(
            "error: --upload needs --analyze (there is no report to upload "
            "without it)"
        )
    route = _load_route(args.route) if args.route else None
    store = _load_store(args.dungeon_data)
    out_dir = Path(args.out)

    def analyze_recorded(run) -> None:
        if not args.analyze:
            return
        try:
            report = _write_recorded_reports(
                run, route, store, pull_gap_seconds=args.pull_gap,
            )
        except Exception as exc:  # keep recording even if analysis hiccups
            print(f"warning: auto-analysis failed: {exc}", file=sys.stderr)
            return

        if report is not None and args.upload:
            # Same best-effort philosophy as cmd_analyze's own --upload
            # handling: a failed upload is printed as a warning and never
            # interrupts the recording session.
            from .upload import upload_report

            result = upload_report(report, args.upload, token=args.upload_token)
            if result.get("ok"):
                print(f"uploaded: {args.upload.rstrip('/')}{result['url']}")
            else:
                print(f"upload failed: {result.get('error')}", file=sys.stderr)

    recorder = Recorder(
        log_path=Path(args.log),
        out_dir=out_dir,
        from_start=args.from_start,
        on_run_complete=analyze_recorded,
        on_start_cmd=args.on_run_start,
        on_end_cmd=args.on_run_end,
        obs_url=args.obs,
        obs_password=args.obs_password,
        obs_replay_on_death=args.obs_replay_on_death,
    )
    recorder.watch()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .history.serve import make_server

    server = make_server(args.directory, port=args.port, bind=args.bind)
    host, port = server.server_address[:2]
    print(f"serving {args.directory} on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    finally:
        server.server_close()
    return 0


def cmd_clips(args: argparse.Namespace) -> int:
    # Checked up front (rather than letting subprocess.run raise
    # FileNotFoundError partway through) so a missing ffmpeg is always a
    # clean, single-line message -- matching _load_avoidable/_load_store's
    # SystemExit convention for a clear, expected CLI error.
    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "error: ffmpeg not found on PATH -- install it to use the clips command"
        )

    video = Path(args.video)
    report_path = Path(args.report)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"error: could not load report {report_path}: {exc}")

    chapters = load_chapters(report_path, report)
    out_dir = Path(args.out) if args.out else video.parent / "clips"
    out_dir.mkdir(parents=True, exist_ok=True)

    specs = clip_specs_for_chapters(chapters, out_dir, pad=args.pad)
    if not specs:
        print("no pull/death chapters found in the report -- nothing to cut",
              file=sys.stderr)
        return 0

    try:
        written = cut_clips(video, specs)
    except FfmpegNotFoundError:
        # Defensive: shutil.which already checked above, but cut_clips
        # re-checks (it's also usable standalone), so handle this the
        # same clean way if PATH somehow changed in between.
        raise SystemExit(
            "error: ffmpeg not found on PATH -- install it to use the clips command"
        )

    for path in written:
        print(f"wrote {path}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="postmortem",
        description="Mythic+ route post-mortem: MDT route vs. what actually happened.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-route", help="decode an MDT export string")
    p.add_argument("route", help="MDT export string, or path to a file containing it")
    p.add_argument("--dungeon-data", help="extracted dungeon data JSON (see extract-data)")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.set_defaults(func=cmd_import_route)

    p = sub.add_parser(
        "extract-data",
        help="extract dungeon/enemy data from a Mythic Dungeon Tools addon folder",
    )
    p.add_argument("addon_path",
                   help="path to Interface/AddOns/MythicDungeonTools (or a checkout)")
    p.add_argument("-o", "--output", default="mdt_data.json")
    p.set_defaults(func=cmd_extract_data)

    p = sub.add_parser(
        "extract-interrupts",
        help="extract addon-captured spell-interruptibility data from the "
             "Postmortem addon's SavedVariables file",
    )
    p.add_argument("savedvariables_path",
                   help="path to the Postmortem SavedVariables file "
                        "(e.g. WTF/Account/<ACCOUNT>/SavedVariables/"
                        "Postmortem.lua) -- both PostmortemDB and "
                        "PostmortemSpellDB live in this one file; "
                        "only PostmortemSpellDB is read")
    p.add_argument("-o", "--output", default="interrupt_data.json")
    p.set_defaults(func=cmd_extract_interrupts)

    p = sub.add_parser(
        "extract-avoidable",
        help="extract the avoidable-damage spell list the Postmortem addon "
             "captures from Blizzard's damage meter (PostmortemAvoidableDB in "
             "its SavedVariables file), and bundle it so every consumer tags "
             "avoidable damage automatically",
    )
    p.add_argument("savedvariables_path",
                   help="path to the Postmortem SavedVariables file "
                        "(e.g. WTF/Account/<ACCOUNT>/SavedVariables/"
                        "Postmortem.lua); only PostmortemAvoidableDB is read")
    p.add_argument("-o", "--output", default="avoidable_spells.json")
    p.add_argument("--dungeon-data",
                   help="dungeon data file used to group spells by dungeon "
                        "(default: the bundled one)")
    p.add_argument("--no-merge", action="store_true",
                   help="rebuild the output from this file alone instead of "
                        "merging into an existing output file")
    p.add_argument("--no-bundle", action="store_true",
                   help="don't also copy the result into the package's bundled "
                        "data folder")
    p.set_defaults(func=cmd_extract_avoidable)

    p = sub.add_parser(
        "build-dispel-data",
        help="build the dispellable-debuff list (dispel efficiency) from the "
             "same Method.gg-derived source file as build-interrupt-data, "
             "and bundle it",
    )
    p.add_argument("source", help="the mplus-interrupts-schema source JSON")
    p.add_argument("-o", "--output", default="dispel_data.json")
    p.add_argument("--resolve-from", nargs="+", metavar="LOG",
                   help="combat logs to resolve ability names to the debuff "
                        "ids that actually landed on players")
    p.add_argument("--no-bundle", action="store_true",
                   help="don't also copy the result into the package's bundled "
                        "data folder")
    p.set_defaults(func=cmd_build_dispel_data)

    p = sub.add_parser(
        "build-interrupt-data",
        help="convert a community mechanics source (albvar/mplus-interrupts "
             "schema) into interrupt-data JSON, and bundle it into this "
             "package by default -- see cli.py's cmd_build_interrupt_data "
             "for why this replaces extract-interrupts as the primary source",
    )
    p.add_argument("source",
                    help="path to a local mplus-interrupts-schema JSON file "
                         "(download the latest from "
                         "https://github.com/albvar/mplus-interrupts -- "
                         "this command reads a local file, it does not "
                         "fetch over the network itself)")
    p.add_argument("-o", "--output", default="interrupt_data.json")
    p.add_argument("--resolve-from", nargs="+", metavar="LOG",
                    help="resolve ability NAMES to spell ids using real "
                         "combat logs instead of trusting the source's own "
                         "ids (strongly recommended -- guide ids often point "
                         "at an ability's damage component rather than its "
                         "interruptible cast). Only ids the logs show being "
                         "interrupted at least once are kept; a name that "
                         "was cast but never kicked is omitted with a warning")
    p.add_argument("--no-bundle", action="store_true",
                    help="don't also copy the result into this package's "
                         "own data/ folder (see bundled.py)")
    p.set_defaults(func=cmd_build_interrupt_data)

    p = sub.add_parser(
        "build-talent-data",
        help="build the talent-node -> talent-name map from Blizzard's own "
             "talent-tree API, so a run's logged talent picks can be shown "
             "by name -- needs BNET_CLIENT_ID/BNET_CLIENT_SECRET in the "
             "environment (see blizzardapi.py)",
    )
    p.add_argument("-o", "--output", default="talent_data.json")
    p.add_argument("--region", default="us",
                    help="Game Data API region to read the trees from "
                         "(talent trees are identical across regions; "
                         "default: us)")
    p.add_argument("--no-bundle", action="store_true",
                    help="don't also copy the result into this package's "
                         "own data/ folder (see bundled.py)")
    p.set_defaults(func=cmd_build_talent_data)

    p = sub.add_parser(
        "build-spell-damage",
        help="sample public Warcraft Logs Mythic+ reports for damage per "
             "completed cast of each kickable enemy spell, per key level, "
             "and bundle the result as the kick-value estimate's community "
             "fallback -- needs WCL_CLIENT_ID/WCL_CLIENT_SECRET in the "
             "environment (see wcl.py)",
    )
    p.add_argument("-o", "--output", default="spell_damage.json")
    p.add_argument("--samples", metavar="JSON", default="spell_damage_samples.json",
                    help="raw per-fight samples, kept between runs so an "
                         "interrupted build resumes where it stopped and a "
                         "later run only fetches fights it hasn't seen")
    p.add_argument("--zone-id", type=int,
                    help="Warcraft Logs zone id of the Mythic+ season "
                         "(default: the newest zone named 'Mythic+')")
    p.add_argument("--per-level", type=int, default=5, metavar="N",
                    help="fights to sample per (dungeon, key level) (default 5)")
    p.add_argument("--min-level", type=int, default=2)
    p.add_argument("--max-level", type=int, default=30)
    p.add_argument("--max-pages", type=int, default=50,
                    help="pages of 40 reports to scan at most, across as many "
                         "time windows as needed (default 50)")
    p.add_argument("--reserve-points", type=float, default=200,
                    help="stop when fewer than this many API points remain "
                         "this hour (default 200); rerun later to resume")
    p.add_argument("--offline", action="store_true",
                    help="don't contact Warcraft Logs; just re-aggregate the "
                         "existing --samples file (no credentials needed)")
    p.add_argument("--all-spells", action="store_true",
                    help="keep every enemy ability, not just the ones in the "
                         "bundled interrupt database")
    p.add_argument("--no-bundle", action="store_true",
                    help="don't also copy the result into this package's "
                         "own data/ folder (see bundled.py)")
    p.set_defaults(func=cmd_build_spell_damage)

    p = sub.add_parser(
        "learn-interrupts",
        help="learn which enemy casts are (and aren't) interruptible from "
             "your own combat logs -- see analysis/interrupt_learning.py",
    )
    p.add_argument("logs", nargs="+", help="combat logs or recorded run slices")
    p.add_argument("-o", "--output", default="learned_interrupts.json",
                    help="accumulated observations file to create/update "
                         "(the desktop app keeps its own in the app data "
                         "folder and updates it after every analyzed run)")
    p.add_argument("--min-attempts", type=int, default=3,
                    help="failed interrupt attempts, with zero successes "
                         "ever, before a spell counts as uninterruptible "
                         "(default 3; raise it for more confidence)")
    p.set_defaults(func=cmd_learn_interrupts)

    p = sub.add_parser("runs", help="list Mythic+ runs found in a combat log")
    p.add_argument("log", help="path to WoWCombatLog.txt")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("analyze", help="post-mortem analysis of a run")
    p.add_argument("log", help="path to WoWCombatLog.txt (or a recorded run slice)")
    p.add_argument("--run", default="last",
                   help="which run in the log: a number from `runs`, or 'last' (default)")
    p.add_argument("--route", help="MDT export string or file with one (intended route)")
    p.add_argument("--dungeon-data", help="extracted dungeon data JSON (see extract-data)")
    p.add_argument("--dispel-data",
                   help="JSON file tagging dispellable enemy debuffs by school "
                        "(see build-dispel-data); default: the bundled list")
    p.add_argument("--avoidable-data",
                   help="JSON file tagging avoidable-damage spell ids (community/"
                        "user-maintained; see docs/avoidable_spells.example.json) "
                        "to break out avoidable damage taken per player")
    p.add_argument("--spell-damage-history", metavar="JSON",
                    help="this account's accumulated per-spell damage-per-cast "
                         "file (see analysis/spell_damage.py): read as a "
                         "fallback for kicks on spells that never landed this "
                         "run, then updated with this run's own landed casts. "
                         "The desktop app keeps one automatically; the bare "
                         "CLI only uses one when told to")
    p.add_argument("--interrupt-data",
                   help="JSON file of spell-interruptibility data: either "
                        "the bundled community database (see "
                        "`build-interrupt-data`; this is what the desktop "
                        "app and Watch Live fall back to automatically) or "
                        "an old addon-captured extraction (see "
                        "`extract-interrupts` -- the live capture path "
                        "itself is permanently dead as of Patch 12.0.0)")
    p.add_argument("--stealable-data",
                   help="JSON file tagging spellsteal-worthy buff spell ids "
                        "(community/user-maintained, like --avoidable-data; "
                        "see docs/stealable_spells.example.json) to "
                        "highlight them in the enemy-casts table")
    p.add_argument("--format", default="text",
                   help="comma-separated: text,json,html (default: text)")
    p.add_argument("--out", help="directory to write reports into (default: stdout/cwd)")
    p.add_argument("--pull-gap", type=float, default=DEFAULT_PULL_GAP_S,
                   help="seconds with no enemy engaged that separates two pulls "
                        f"(default {DEFAULT_PULL_GAP_S})")
    p.add_argument("--no-cast-timeline", action="store_true",
                   help="omit the full per-cast timeline from JSON output")
    p.add_argument("--death-penalty", type=float, default=15.0,
                   help="seconds the keystone timer loses per death (default 15)")
    p.add_argument("--raiderio", metavar="REGION",
                   help="enrich players with Raider.io scores (us/eu/kr/tw/cn); "
                        "needs internet access")
    p.add_argument("--raiderio-no-cache", action="store_true",
                   help="bypass the on-disk Raider.io lookup cache for this run "
                        "(always fetch fresh; only matters with --raiderio)")
    p.add_argument("--timer-data", metavar="PATH",
                   help="JSON file mapping challenge_map_id -> par time in ms "
                        "(same shape as the bundled data/timers.json) for "
                        "keystone-timer margin/threshold reporting; used "
                        "automatically (falling back to the bundled example "
                        "seed) whenever --raiderio is also given, or on its "
                        "own to use a specific file with no network access")
    p.add_argument("--expansion-id", type=int, metavar="N",
                   help="expansion_id to query Raider.io's live "
                        "mythic-plus/static-data endpoint for dungeon par "
                        "times (only used together with --raiderio; if "
                        "omitted, --timer-data / the bundled example seed is "
                        "used directly with no live fetch attempted -- we "
                        "don't guess a 'current' expansion id)")
    p.add_argument("--history-db", metavar="PATH",
                   help="also append this run to a SQLite run-history database "
                        "at PATH (created if missing) — see `index --db`")
    p.add_argument("--upload", metavar="URL",
                   help="also upload this run's report to a public "
                        "postmortem site at URL (e.g. "
                        "https://postmortem.fly.dev) so it's browsable "
                        "there; needs internet access, and never fails the "
                        "analysis itself if the upload doesn't go through")
    p.add_argument("--upload-token", metavar="TOKEN",
                   help="upload token to use with --upload, overriding the "
                        "one auto-generated and stored locally on first use "
                        "(only needed to use a specific/shared token)")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser(
        "index",
        help="build a historical index.html over a directory of saved reports",
    )
    p.add_argument("directory", help="directory containing report .json/.html files")
    p.add_argument("-o", "--output",
                   help="where to write the page (default: <directory>/index.html)")
    p.add_argument("--db", metavar="PATH",
                   help="ingest scanned reports into a SQLite database at PATH "
                        "(created if missing, idempotent) and build the page from "
                        "it instead of a fresh JSON scan")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("record", help="watch the combat log live and record each run")
    p.add_argument("log", help="path to WoWCombatLog.txt")
    p.add_argument("--out", default="runs", help="directory for recorded runs")
    p.add_argument("--route", help="MDT export string/file for auto-analysis")
    p.add_argument("--dungeon-data", help="extracted dungeon data JSON")
    p.add_argument("--analyze", action="store_true",
                   help="auto-analyze each run when it completes")
    p.add_argument("--from-start", action="store_true",
                   help="also process runs already in the log, not just new ones")
    p.add_argument("--pull-gap", type=float, default=DEFAULT_PULL_GAP_S)
    p.add_argument("--on-run-start", metavar="CMD",
                   help="shell command to run when a key starts (e.g. "
                        "'obs-cmd recording start' for video capture); "
                        "MA_ZONE/MA_LEVEL/MA_PATH are set in its environment")
    p.add_argument("--on-run-end", metavar="CMD",
                   help="shell command to run when the key ends "
                        "(e.g. 'obs-cmd recording stop')")
    p.add_argument("--obs", nargs="?", const="ws://127.0.0.1:4455", default=None,
                   metavar="URL",
                   help="natively drive OBS via its own WebSocket v5 API "
                        "(no obs-cmd/third-party tool needed) -- start/stop "
                        "recording with each key. Pass a URL (e.g. "
                        "ws://127.0.0.1:4455, OBS's default) or just '--obs' "
                        "alone to use that same default. If --on-run-start/"
                        "--on-run-end are ALSO given, those shell hooks take "
                        "precedence for that event and the native client is "
                        "not additionally invoked for it, to avoid "
                        "double-triggering OBS; any native-OBS failure is "
                        "only ever a warning, recording continues regardless")
    p.add_argument("--obs-password", metavar="PASSWORD",
                   help="OBS WebSocket server password, if one is set "
                        "(Tools -> WebSocket Server Settings in OBS)")
    p.add_argument("--obs-replay-on-death", action="store_true",
                   help="save the OBS replay buffer (SaveReplayBuffer) every "
                        "time a player death is detected; independent of "
                        "shell-hook precedence -- always uses the native OBS "
                        "client when --obs is set (there's no equivalent "
                        "shell hook for this event to conflict with)")
    p.add_argument("--upload", metavar="URL",
                   help="with --analyze, also upload each completed run's "
                        "report to a public postmortem site at URL as soon "
                        "as it's analyzed -- automatic, no separate command "
                        "per run; same best-effort semantics as `analyze "
                        "--upload` (never fails the recording session if a "
                        "given upload doesn't go through)")
    p.add_argument("--upload-token", metavar="TOKEN",
                   help="upload token to use with --upload, overriding the "
                        "one auto-generated and stored locally on first use")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser(
        "serve",
        help="serve a directory of saved reports locally, rebuilding the "
             "index whenever report files change",
    )
    p.add_argument("directory", help="directory containing report .json/.html files")
    p.add_argument("--port", type=int, default=8765,
                   help="port to listen on (default: 8765)")
    p.add_argument("--bind", default="127.0.0.1",
                   help="address to bind to (default: 127.0.0.1, loopback-only)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "clips",
        help="cut one video clip per pull and per death via ffmpeg",
    )
    p.add_argument("video", help="path to the recorded video file")
    p.add_argument("report", metavar="REPORT_JSON",
                   help="analyzed run report JSON (see analyze/record --analyze); "
                        "clip offsets prefer a <report>.chapters.json sidecar next "
                        "to it if one exists, else are recomputed assuming the "
                        "video starts exactly at run start")
    p.add_argument("--out", metavar="DIR",
                   help="directory to write clips into (default: a 'clips' "
                        "subdirectory next to the video)")
    p.add_argument("--pad", type=float, default=DEFAULT_PAD_S,
                   help=f"seconds of padding before/after each clip "
                        f"(default {DEFAULT_PAD_S:.0f})")
    p.set_defaults(func=cmd_clips)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    make_streams_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
