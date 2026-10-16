"""The pace line: how far ahead of or behind a benchmark the key was after
every pull, and what each pull gained or lost.

Like the verdict (analysis/verdict.py) this reads only fields a finished
report already carries -- pulls, the forces timeline, encounters, deaths,
the timer -- so it can be built for any stored report, and every field is
treated as hostile: on the public site a report is an anonymous upload.
Anything unreadable is skipped, and with nothing left there is no pace
(``None``) rather than an exception.

Runs are compared by progress, not by pull number, so different routes
and pull orders line up fairly:

* **progress** = 0.5 x forces done + 0.5 x bosses killed / total bosses.
  A half that is unknown (no dungeon data for forces; no boss total for an
  unfinished run) is dropped and the other half counts fully.
* **game time** = seconds since the key started + the death penalty for
  every death so far -- what the in-game timer read.
* **benchmark** = the player's own best timed run (its own points,
  interpolated) or, without one, the timer spread evenly over progress.

``delta_s`` is positive when behind. ``change_s`` is what that one pull
gained (negative) or lost (positive).
"""

from __future__ import annotations

from typing import Any, Optional

from .verdict import _dict, _int, _list, _num

PACE_VERSION = 1


def _kills(report: dict) -> list[tuple[float, Any]]:
    """(end time, identity) of every boss kill, in report order."""
    out = []
    for enc in _list(report.get("encounters")):
        enc = _dict(enc)
        if enc.get("kill") is not True:
            continue
        start, length = _num(enc.get("t")), _num(enc.get("duration_s"))
        if start is None:
            continue
        ident = _int(enc.get("encounter_id"))
        if ident is None:
            ident = enc.get("name") if isinstance(enc.get("name"), str) else None
        if ident is None:
            continue
        out.append((start + (length or 0.0), ident))
    return out


def has_forces(report: dict) -> bool:
    """Whether the report can measure forces (it was analysed with dungeon
    data). Two runs are only compared when both can, or neither: a
    forces+bosses run against a bosses-only benchmark biases every delta."""
    required = _num(_dict(report.get("forces")).get("required"))
    return required is not None and required > 0


def boss_count(report: dict) -> Optional[int]:
    """Distinct bosses killed, but only for a completed run -- where that
    is every boss the dungeon has. None when unknown."""
    if _dict(report.get("run")).get("completed") is not True:
        return None
    n = len({ident for _, ident in _kills(report)})
    return n or None


def progress_points(report: dict, total_bosses: Optional[int] = None) -> list[dict]:
    """One ``{"pull", "t_end", "progress", "game_s"}`` per pull with a
    readable end time, in pull-end order."""
    total = total_bosses if total_bosses and total_bosses > 0 else boss_count(report)

    forces = _dict(report.get("forces"))
    required = _num(forces.get("required"))
    timeline = []
    if required and required > 0:
        for entry in _list(forces.get("timeline")):
            entry = _dict(entry)
            t, f = _num(entry.get("t")), _num(entry.get("forces"))
            if t is not None and f is not None:
                timeline.append((t, f))
        timeline.sort()
        # A finished key had 100% forces in game. When the dungeon data's
        # counts fall short of that (real Kings' Rest +17: 96.5%), the
        # run's own final total is what 100% was.
        if _dict(report.get("run")).get("completed") is True and timeline:
            final = max(f for _, f in timeline)
            if 0 < final < required:
                required = final
    else:
        required = None

    kills = sorted(end for end, _ in _kills(report))
    deaths = sorted(t for t in (_num(_dict(d).get("t")) for d in _list(report.get("deaths")))
                    if t is not None)
    per_death = _num(_dict(report.get("death_cost")).get("per_death_s")) or 0.0

    if required is None and not total:
        return []

    out = []
    for pull in _list(report.get("pulls")):
        pull = _dict(pull)
        t_end = _num(pull.get("t_end"))
        number = _int(pull.get("pull"))
        if t_end is None or number is None:
            continue
        halves = []
        if required is not None:
            done = 0.0
            for t, f in timeline:
                if t > t_end:
                    break
                done = f
            halves.append(min(1.0, max(0.0, done / required)))
        if total:
            killed = sum(1 for end in kills if end <= t_end)
            halves.append(min(1.0, killed / total))
        died = sum(1 for t in deaths if t <= t_end)
        out.append({
            "pull": number,
            "t_end": round(t_end, 1),
            "progress": sum(halves) / len(halves),
            "game_s": t_end + died * per_death,
        })
    out.sort(key=lambda p: p["t_end"])
    return out


def _curve(benchmark: dict, total_bosses: Optional[int]) -> Optional[list[tuple[float, float]]]:
    """The benchmark's (progress, game time) curve from (0, 0) to
    (1, its final time), progress made non-decreasing."""
    final = _num(_dict(benchmark.get("run")).get("duration_ms"))
    pts = progress_points(benchmark, total_bosses)
    if final is None or final <= 0 or not pts:
        return None
    curve = [(0.0, 0.0)]
    best = 0.0
    for p in pts:
        best = max(best, p["progress"])
        if p["game_s"] >= curve[-1][1]:
            curve.append((best, p["game_s"]))
    curve.append((1.0, max(final / 1000.0, curve[-1][1])))
    return curve


def _at(curve: list[tuple[float, float]], progress: float) -> float:
    """Game time at which the curve reached ``progress`` (linear)."""
    for (p0, t0), (p1, t1) in zip(curve, curve[1:]):
        if progress <= p1:
            if p1 <= p0:
                return t0
            return t0 + (t1 - t0) * (progress - p0) / (p1 - p0)
    return curve[-1][1]


def build_pace(report: dict, benchmark: Optional[dict] = None,
               benchmark_info: Optional[dict] = None) -> Optional[dict]:
    """The report's ``pace`` block, or None when it cannot be measured.

    ``benchmark`` is the full report of the run to compare against (the
    player's own best); without one, or when it has no usable points, the
    dungeon timer is the benchmark. ``benchmark_info`` (level, duration_s,
    date, same_level) is copied into the output for the caption."""
    report = _dict(report)
    curve = None
    total = boss_count(report)
    if isinstance(benchmark, dict):
        bench_total = boss_count(benchmark)
        total = total or bench_total
        curve = _curve(benchmark, bench_total or total)

    points = progress_points(report, total)
    if not points:
        return None

    if curve is not None:
        bench_of = lambda p: _at(curve, p)  # noqa: E731
        info = {}
        for key, value in _dict(benchmark_info).items():
            if key in ("level", "duration_s") and _num(value) is not None:
                info[key] = _num(value) if key == "duration_s" else _int(value)
            elif key == "date" and isinstance(value, str):
                info[key] = value[:10]
            elif key == "same_level" and isinstance(value, bool):
                info[key] = value
        head = {"kind": "own_best", **info}
    else:
        par_ms = _num(_dict(report.get("timer")).get("par_ms"))
        if par_ms is None or par_ms <= 0:
            return None
        par = par_ms / 1000.0
        bench_of = lambda p: par * p  # noqa: E731
        head = {"kind": "timer", "par_s": round(par, 1)}

    out_points = []
    previous = 0.0
    for p in points:
        bench = bench_of(p["progress"])
        delta = p["game_s"] - bench
        out_points.append({
            "pull": p["pull"],
            "t_end": p["t_end"],
            "progress": round(p["progress"], 3),
            "game_s": round(p["game_s"], 1),
            "bench_s": round(bench, 1),
            "delta_s": round(delta, 1),
            "change_s": round(delta - previous, 1),
        })
        previous = delta
    return {
        "version": PACE_VERSION,
        "benchmark": head,
        "points": out_points,
        "final_delta_s": out_points[-1]["delta_s"],
    }
