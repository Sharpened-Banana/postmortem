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

    report = analyze_run(
        segment,
        route=route,
        store=store,
        avoidable=avoidable,
        pull_gap_seconds=args.pull_gap,
        full_cast_timeline=not args.no_cast_timeline,
        death_penalty_s=args.death_penalty,
    )

    if args.raiderio:
        from .raiderio import enrich_report
        n = enrich_report(report, args.raiderio)
        print(f"raider.io: enriched {n} players", file=sys.stderr)

    formats = [f.strip() for f in args.format.split(",") if f.strip()]
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    base = _report_basename(report)

    wrote = []
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
            else:
                print(payload)
        elif fmt == "html":
            html = render_html(report)
            path = (out_dir or Path(".")) / f"{base}.html"
            path.write_text(html, encoding="utf-8")
            wrote.append(path)
        else:
            raise SystemExit(f"error: unknown format {fmt!r} (text, json, html)")
    for path in wrote:
        print(f"wrote {path}", file=sys.stderr)
    return 0


def _report_basename(report: dict) -> str:
    import re as _re
    import time as _time

    zone = report["run"].get("zone") or "run"
    zone = _re.sub(r"[^A-Za-z0-9]+", "", zone)
    level = report["run"].get("keystone_level") or "x"
    stamp = _time.strftime("%Y%m%d-%H%M%S", _time.localtime(report["run"]["start_ts"]))
    return f"{stamp}_{zone}_{level}"


def cmd_index(args: argparse.Namespace) -> int:
    from .report.index import build_index, collect_reports

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
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser(
        "index",
        help="build a historical index.html over a directory of saved reports",
    )
    p.add_argument("directory", help="directory containing report .json/.html files")
    p.add_argument("-o", "--output",
                   help="where to write the page (default: <directory>/index.html)")
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

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
