"""The pace line's benchmark: the player's own best timed run of this
dungeon, picked from the local history store (analysis/pace.py does the
maths).

Order: same dungeon, timed, not this run -> same key level -> nearest key
level (a higher one first at equal distance: matching a harder key's pace
is a fair bar) -> fastest. A candidate whose stored report cannot give
pace points (an old report without forces or encounters) is passed over
for the next one; with none left the timer is the benchmark.

Best-effort throughout: attaching pace must never cost a run its reports,
so any failure leaves the timer pace (or none) and is not raised.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

from ..analysis.pace import build_pace, progress_points

#: Two runs of one dungeon starting within this many seconds are the same run.
_SAME_RUN_S = 1.0


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def rank_candidates(report: dict, rows: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
    """History rows that could be this run's benchmark, best first."""
    run = report.get("run") if isinstance(report.get("run"), dict) else {}
    zone, level, start = run.get("zone"), _number(run.get("keystone_level")), _number(run.get("start_ts"))
    if not isinstance(zone, str) or level is None:
        return []
    usable = []
    for run_id, row in rows:
        row_level, duration = _number(row.get("level")), _number(row.get("duration_ms"))
        row_start = _number(row.get("start_ts"))
        if row.get("zone") != zone or row.get("timed") is not True:
            continue
        if row_level is None or duration is None or duration <= 0:
            continue
        if start is not None and row_start is not None and abs(row_start - start) < _SAME_RUN_S:
            continue  # this run itself, already in history
        distance = abs(row_level - level)
        usable.append(((distance, -row_level, duration), run_id, row))
    usable.sort(key=lambda item: item[0])
    return [(run_id, row) for _, run_id, row in usable]


def attach_pace(report: dict, db_path: Optional[str | Path]) -> dict:
    """Set ``report["pace"]`` against the best usable history run, else the
    timer, and return the report. Never raises."""
    benchmark, info = None, None
    try:
        if db_path is not None and Path(db_path).is_file():
            from .store import Store

            run = report.get("run") if isinstance(report.get("run"), dict) else {}
            with Store(db_path) as store:
                for run_id, row in rank_candidates(report, store.query_runs_with_ids()):
                    candidate = store.get_report(run_id)
                    if not isinstance(candidate, dict) or not progress_points(candidate):
                        continue
                    benchmark = candidate
                    info = {
                        "level": row.get("level"),
                        "duration_s": row["duration_ms"] / 1000.0,
                        "date": row.get("date") if isinstance(row.get("date"), str) else None,
                        "same_level": row.get("level") == run.get("keystone_level"),
                    }
                    break
    except Exception:
        benchmark, info = None, None
    try:
        report["pace"] = build_pace(report, benchmark, info)
    except Exception:
        report["pace"] = None
    return report
