"""postmortem command line interface."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

from .analysis.avoidable import AvoidableData
from .analysis.interruptibility import InterruptibilityData
from .analysis.run_analyzer import analyze_run
from .chapters import write_chapter_files
from .clips import DEFAULT_PAD_S, FfmpegNotFoundError, clip_specs_for_chapters, cut_clips, load_chapters
from .combatlog.parser import parse_file
from .combatlog.segmenter import RunSegment, segment_runs
from .mdt.decode import MDTDecodeError, decode_mdt_string
from .mdt.dungeon_data import DungeonDataStore
from .mdt.extract import LuaLiteralParser, LuaParseError, _find_assignment, write_dungeon_data
from .mdt.route import Route
from .recorder import Recorder
from .report.html import render_html
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
    payload = write_dungeon_data(args.addon_path, args.output)
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
            "over timer" if s["completed"] else "incomplete")
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
    avoidable = _load_avoidable(args.avoidable_data)
    interrupt_data = _load_interruptibility(args.interrupt_data)
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
        pull_gap_seconds=args.pull_gap,
        full_cast_timeline=not args.no_cast_timeline,
        death_penalty_s=args.death_penalty,
        par_ms=par_ms,
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
            path = (out_dir or Path(".")) / f"{base}.html"
            path.write_text(html, encoding="utf-8")
            wrote.append(path)
            html_path = path
        else:
            raise SystemExit(f"error: unknown format {fmt!r} (text, json, html)")
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


def _write_recorded_reports(run, route, store, pull_gap_seconds: float = 5.0) -> None:
    """Analyze one recorded run's log slice and write its JSON/HTML/text
    reports, plus the chapters sidecars (``<run>.chapters.json`` /
    ``<run>.vtt`` -- see :mod:`mythic_analyzer.chapters`, WP-D2) next to
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
    """
    # list(...) is fine here: run.path is a per-run recorded slice
    # (Recorder opens a fresh file per CHALLENGE_MODE_START), so this
    # never holds more than one run's events regardless.
    segments = list(segment_runs(parse_file(run.path)))
    if not segments:
        return
    report = analyze_run(segments[-1], route=route, store=store,
                         pull_gap_seconds=pull_gap_seconds)
    base = run.path.with_suffix("")
    Path(f"{base}.json").write_text(json.dumps(report, indent=1),
                                    encoding="utf-8")
    Path(f"{base}.html").write_text(render_html(report), encoding="utf-8")
    write_chapter_files(report, run.started_at, base)
    print(render_text(report))
    print(f"wrote {base}.json / {base}.html / {base}.chapters.json / {base}.vtt",
          file=sys.stderr)


def cmd_record(args: argparse.Namespace) -> int:
    route = _load_route(args.route) if args.route else None
    store = _load_store(args.dungeon_data)
    out_dir = Path(args.out)

    def analyze_recorded(run) -> None:
        if not args.analyze:
            return
        try:
            _write_recorded_reports(run, route, store, pull_gap_seconds=args.pull_gap)
        except Exception as exc:  # keep recording even if analysis hiccups
            print(f"warning: auto-analysis failed: {exc}", file=sys.stderr)

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

    p = sub.add_parser("runs", help="list Mythic+ runs found in a combat log")
    p.add_argument("log", help="path to WoWCombatLog.txt")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("analyze", help="post-mortem analysis of a run")
    p.add_argument("log", help="path to WoWCombatLog.txt (or a recorded run slice)")
    p.add_argument("--run", default="last",
                   help="which run in the log: a number from `runs`, or 'last' (default)")
    p.add_argument("--route", help="MDT export string or file with one (intended route)")
    p.add_argument("--dungeon-data", help="extracted dungeon data JSON (see extract-data)")
    p.add_argument("--avoidable-data",
                   help="JSON file tagging avoidable-damage spell ids (community/"
                        "user-maintained; see docs/avoidable_spells.example.json) "
                        "to break out avoidable damage taken per player")
    p.add_argument("--interrupt-data",
                   help="JSON file of addon-captured spell-interruptibility "
                        "data (ground truth from the game client, not a "
                        "curated list; see `extract-interrupts`)")
    p.add_argument("--format", default="text",
                   help="comma-separated: text,json,html (default: text)")
    p.add_argument("--out", help="directory to write reports into (default: stdout/cwd)")
    p.add_argument("--pull-gap", type=float, default=5.0,
                   help="seconds of no-combat that separates two pulls (default 5)")
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
    p.add_argument("--pull-gap", type=float, default=5.0)
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
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
