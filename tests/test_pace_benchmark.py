"""Picking the pace benchmark from local history (history/benchmark.py)."""

from __future__ import annotations

from postmortem.history.benchmark import attach_pace, rank_candidates
from postmortem.history.store import Store, ingest

KR = "Kings' Rest"
KR_MAP = 249  # bundled par 33:00, so ingest keeps these runs "timed"
DAY = 1_791_000_000.0


def _run(level, duration_s, start, zone=KR, data=True, timed=True):
    """A small completed report; ``data=False`` strips what pace needs."""
    r = {
        "run": {"zone": zone, "challenge_map_id": KR_MAP if zone == KR else 999,
                "keystone_level": level, "start_ts": start, "end_ts": start + duration_s,
                "completed": True, "timed": timed,
                "duration_ms": int(duration_s * 1000) if timed else 2_400_000,
                "wall_duration_s": duration_s},
        "timer": {"par_ms": 1_980_000},
        "death_cost": {"per_death_s": 15.0},
        "deaths": [],
    }
    if data:
        r.update({
            "forces": {"required": 100, "timeline": [{"t": duration_s / 2, "forces": 100}]},
            "encounters": [{"encounter_id": 1, "name": "A", "kill": True,
                            "t": duration_s - 100, "duration_s": 90.0}],
            "pulls": [{"pull": 1, "t_end": duration_s / 2},
                      {"pull": 2, "t_end": duration_s - 10}],
        })
    return r


def _db(tmp_path, *reports):
    db = tmp_path / "history.db"
    for r in reports:
        ingest(r, db)
    return db


def _ranked_levels_and_durations(db, report):
    with Store(db) as store:
        rows = store.query_runs_with_ids()
    return [(row["level"], row["duration_ms"] // 1000) for _, row in rank_candidates(report, rows)]


def test_same_level_beats_faster_other_levels(tmp_path):
    db = _db(tmp_path, _run(17, 1700, DAY), _run(15, 1500, DAY + 1e5))
    ranked = _ranked_levels_and_durations(db, _run(17, 1800, DAY + 9e5))
    assert ranked[0] == (17, 1700)


def test_nearest_level_prefers_higher_on_a_tie(tmp_path):
    db = _db(tmp_path, _run(16, 1500, DAY), _run(18, 1700, DAY + 1e5), _run(12, 1000, DAY + 2e5))
    ranked = _ranked_levels_and_durations(db, _run(17, 1800, DAY + 9e5))
    assert [lvl for lvl, _ in ranked] == [18, 16, 12]


def test_fastest_within_the_chosen_level(tmp_path):
    db = _db(tmp_path, _run(17, 1750, DAY), _run(17, 1650, DAY + 1e5), _run(17, 1700, DAY + 2e5))
    ranked = _ranked_levels_and_durations(db, _run(17, 1800, DAY + 9e5))
    assert [d for _, d in ranked] == [1650, 1700, 1750]


def test_excludes_untimed_other_dungeons_and_itself(tmp_path):
    this = _run(17, 1600, DAY + 9e5)
    db = _db(tmp_path, this, _run(17, 1500, DAY, timed=False),
             _run(17, 1400, DAY + 1e5, zone="Murder Row"))
    assert _ranked_levels_and_durations(db, this) == []


def test_attach_uses_own_best_and_names_it(tmp_path):
    db = _db(tmp_path, _run(16, 1500, DAY))
    report = attach_pace(_run(17, 1800, DAY + 9e5), db)
    bench = report["pace"]["benchmark"]
    assert bench["kind"] == "own_best"
    assert bench["level"] == 16
    assert bench["same_level"] is False
    assert bench["duration_s"] == 1500
    assert len(bench["date"]) == 10


def test_attach_skips_a_benchmark_without_progress_data(tmp_path):
    db = _db(tmp_path, _run(17, 1400, DAY, data=False), _run(17, 1600, DAY + 1e5))
    report = attach_pace(_run(17, 1800, DAY + 9e5), db)
    assert report["pace"]["benchmark"]["duration_s"] == 1600

    only_bad = tmp_path / "only-bad"
    only_bad.mkdir()
    db2 = _db(only_bad, _run(17, 1400, DAY, data=False))
    assert attach_pace(_run(17, 1800, DAY + 9e5), db2)["pace"]["benchmark"]["kind"] == "timer"


def test_attach_without_history_is_timer(tmp_path):
    for db in (None, tmp_path / "missing.db"):
        report = attach_pace(_run(17, 1800, DAY), db)
        assert report["pace"]["benchmark"]["kind"] == "timer"
    assert not (tmp_path / "missing.db").exists()  # never created


def test_attach_never_raises_on_a_broken_database(tmp_path):
    bad = tmp_path / "history.db"
    bad.write_text("not a database")
    report = attach_pace(_run(17, 1800, DAY), bad)
    assert report["pace"]["benchmark"]["kind"] == "timer"


def test_a_benchmark_measured_differently_is_passed_over(tmp_path):
    # The fastest +17 was analysed without dungeon data (no forces), so its
    # progress is bosses-only while this run's is forces + bosses: comparing
    # the two biased every delta (final review, 2026-10-15).
    no_forces = _run(17, 1400, DAY)
    no_forces["forces"] = {"required": None}
    db = _db(tmp_path, no_forces, _run(17, 1600, DAY + 1e5))
    report = attach_pace(_run(17, 1800, DAY + 9e5), db)
    assert report["pace"]["benchmark"]["duration_s"] == 1600


def test_pace_against_takes_any_run_source():
    # The site has no local history file: it hands over its own rows (the
    # signed-in account's uploads) and a loader for their reports.
    from postmortem.history.benchmark import pace_against

    stored = {7: _run(17, 1600, DAY), 8: _run(17, 1500, DAY + 1e5, data=False)}
    rows = [(7, {"zone": KR, "level": 17, "timed": True, "duration_ms": 1_600_000,
                 "start_ts": DAY, "date": "2026-10-01 20:00"}),
            (8, {"zone": KR, "level": 17, "timed": True, "duration_ms": 1_500_000,
                 "start_ts": DAY + 1e5, "date": "2026-10-02 20:00"})]
    report = _run(17, 1800, DAY + 9e5)
    pace = pace_against(report, rows, stored.get)
    # run 8 is faster but has no progress data, so run 7 is the benchmark
    assert pace["benchmark"]["kind"] == "own_best"
    assert pace["benchmark"]["duration_s"] == 1600
    assert pace["benchmark"]["date"] == "2026-10-01"


def test_pace_against_survives_a_loader_that_raises():
    from postmortem.history.benchmark import pace_against

    def boom(_run_id):
        raise RuntimeError("db gone")

    rows = [(7, {"zone": KR, "level": 17, "timed": True, "duration_ms": 1_600_000,
                 "start_ts": DAY, "date": "2026-10-01 20:00"})]
    assert pace_against(_run(17, 1800, DAY + 9e5), rows, boom)["benchmark"]["kind"] == "timer"
