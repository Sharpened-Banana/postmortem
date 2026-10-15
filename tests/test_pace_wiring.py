"""Every newly analysed report carries a pace block (CLI analyze, Watch
Live's recorded reports), and rendering builds one for reports stored
before it existed."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import build_run_log

from postmortem.cli import _write_recorded_reports, main
from postmortem.history.store import ingest
from postmortem.recorder import Recorder
from postmortem.report.html import render_html


def _prior_murder_row(start_ts: float) -> dict:
    """An earlier timed Murder Row +10 with enough data for pace points."""
    return {
        "run": {"zone": "Murder Row", "challenge_map_id": 587, "keystone_level": 10,
                "start_ts": start_ts, "end_ts": start_ts + 500, "completed": True,
                "timed": True, "duration_ms": 500_000, "wall_duration_s": 500.0},
        "death_cost": {"per_death_s": 15.0},
        "forces": {"required": 100, "timeline": [{"t": 200.0, "forces": 100}]},
        "encounters": [{"encounter_id": 3001, "name": "Big Boss", "kill": True,
                        "t": 300.0, "duration_s": 150.0}],
        "pulls": [{"pull": 1, "t_end": 200.0}, {"pull": 2, "t_end": 450.0}],
        "deaths": [],
    }


def _analyze_json(log_file, dungeon_data_file, tmp_path, *extra) -> dict:
    out = tmp_path / "out"
    assert main(["analyze", str(log_file), "--dungeon-data", str(dungeon_data_file),
                 "--format", "json", "--out", str(out), *extra]) == 0
    (path,) = out.glob("*.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_analyze_with_history_db_attaches_own_best_pace(log_file, dungeon_data_file, tmp_path):
    db = tmp_path / "history.db"
    ingest(_prior_murder_row(1_000_000.0), db)
    report = _analyze_json(log_file, dungeon_data_file, tmp_path, "--history-db", str(db))
    assert report["pace"]["benchmark"]["kind"] == "own_best"
    assert report["pace"]["benchmark"]["same_level"] is True


def test_analyze_without_history_gets_timer_pace(log_file, dungeon_data_file, tmp_path):
    # the fixture's Murder Row (challenge map 587) has a bundled par of 34:00
    report = _analyze_json(log_file, dungeon_data_file, tmp_path)
    assert report["pace"]["benchmark"]["kind"] == "timer"
    assert report["pace"]["points"]


def test_render_builds_pace_for_an_old_report():
    old = {**_prior_murder_row(1_000_000.0), "timer": {"par_ms": 2_040_000}}
    html = render_html(old)
    assert '"pace": {"version": 1' in html or '"pace":{"version":1' in html


def test_render_survives_a_report_pace_cannot_read():
    html = render_html({"run": {"zone": "Murder Row"}, "pulls": "x", "forces": "x",
                        "timer": {"par_ms": "x"}, "deaths": 5})
    assert "<html" in html.lower()


def test_recorded_reports_carry_pace(tmp_path):
    log = tmp_path / "WoWCombatLog.txt"
    log.write_text(build_run_log().text(), encoding="utf-8")
    rec = Recorder(log_path=log, out_dir=tmp_path / "runs", from_start=True,
                   echo=lambda s: None)
    (run,) = rec.watch(stop_after_runs=1)
    db = tmp_path / "history.db"
    ingest(_prior_murder_row(1_000_000.0), db)
    report = _write_recorded_reports(run, route=None, store=None, history_db_path=db)
    saved = json.loads(Path(f"{run.path.with_suffix('')}.json").read_text(encoding="utf-8"))
    assert report["pace"] is not None
    assert saved["pace"] == report["pace"]
