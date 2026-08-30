"""mythic-analyzer command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

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


def _pick_run(segments: list[RunSegment], selector: str) -> RunSegment:
    if not segments:
        raise SystemExit("error: no Mythic+ runs (CHALLENGE_MODE_START) found in this log")
    if selector == "last":
        return segments[-1]
    try:
        idx = int(selector)
    except ValueError:
        raise SystemExit(f"error: --run must be a number or 'last', got {selector!r}")
    if not 1 <= idx <= len(segments):
        raise SystemExit(f"error: --run {idx} out of range (log has {len(segments)} runs)")
    return segments[idx - 1]


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
    segments = list(segment_runs(parse_file(args.log)))
    if not segments:
        print("no Mythic+ runs found in this log")
        return 1
    for i, seg in enumerate(segments, start=1):
        s = seg.summary()
        state = "timed" if s["timed"] else (
            "over timer" if s["completed"] else "incomplete")
        mins = s["wall_duration_s"] / 60
        print(f"{i:>3}. {s['zone']} +{s['keystone_level']}  [{state}]"
              f"  {mins:.1f} min, {s['event_count']} events")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    route = _load_route(args.route) if args.route else None
    store = _load_store(args.dungeon_data)
    segments = list(segment_runs(parse_file(args.log)))
    segment = _pick_run(segments, args.run)

    report = analyze_run(
        segment,
        route=route,
        store=store,
        pull_gap_seconds=args.pull_gap,
        full_cast_timeline=not args.no_cast_timeline,
    )

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


def cmd_record(args: argparse.Namespace) -> int:
    route = _load_route(args.route) if args.route else None
    store = _load_store(args.dungeon_data)
    out_dir = Path(args.out)

    def analyze_recorded(run) -> None:
        if not args.analyze:
            return
        try:
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
    p.add_argument("--format", default="text",
                   help="comma-separated: text,json,html (default: text)")
    p.add_argument("--out", help="directory to write reports into (default: stdout/cwd)")
    p.add_argument("--pull-gap", type=float, default=5.0,
                   help="seconds of no-combat that separates two pulls (default 5)")
    p.add_argument("--no-cast-timeline", action="store_true",
                   help="omit the full per-cast timeline from JSON output")
    p.set_defaults(func=cmd_analyze)

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
    p.set_defaults(func=cmd_record)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
