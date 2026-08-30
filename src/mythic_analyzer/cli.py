"""mythic-analyzer command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, Optional

from .analysis.avoidable import AvoidableData
from .analysis.run_analyzer import analyze_run
from .combatlog.parser import parse_file
from .combatlog.segmenter import RunSegment, segment_runs
from .mdt.decode import MDTDecodeError, decode_mdt_string
from .mdt.dungeon_data import DungeonDataStore
from .mdt.extract import write_dungeon_data
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
              "— create it with `mythic-analyzer extract-data`)")
    return 0


def cmd_extract_data(args: argparse.Namespace) -> int:
    payload = write_dungeon_data(args.addon_path, args.output)
    n = len(payload["dungeons"])
    print(f"extracted {n} dungeons -> {args.output}")
    for d in sorted(payload["dungeons"].values(), key=lambda d: d["dungeon_idx"]):
        print(f"  [{d['dungeon_idx']:>3}] {d['name']}: {len(d['enemies'])} enemy types")
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


def cmd_record(args: argparse.Namespace) -> int:
    route = _load_route(args.route) if args.route else None
    store = _load_store(args.dungeon_data)
    out_dir = Path(args.out)

    def analyze_recorded(run) -> None:
        if not args.analyze:
            return
        try:
            # list(...) is fine here: run.path is a per-run recorded slice
            # (Recorder opens a fresh file per CHALLENGE_MODE_START), so this
            # never holds more than one run's events regardless.
            segments = list(segment_runs(parse_file(run.path)))
            if not segments:
                return
            report = analyze_run(segments[-1], route=route, store=store,
                                 pull_gap_seconds=args.pull_gap)
            base = run.path.with_suffix("")
            Path(f"{base}.json").write_text(json.dumps(report, indent=1),
                                            encoding="utf-8")
            Path(f"{base}.html").write_text(render_html(report), encoding="utf-8")
            print(render_text(report))
            print(f"wrote {base}.json / {base}.html", file=sys.stderr)
        except Exception as exc:  # keep recording even if analysis hiccups
            print(f"warning: auto-analysis failed: {exc}", file=sys.stderr)

    recorder = Recorder(
        log_path=Path(args.log),
        out_dir=out_dir,
        from_start=args.from_start,
        on_run_complete=analyze_recorded,
        on_start_cmd=args.on_run_start,
        on_end_cmd=args.on_run_end,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mythic-analyzer",
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

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
