"""The pace line (analysis/pace.py): ahead of / behind a benchmark after
every pull, from a finished report's own fields."""

from __future__ import annotations

import pytest

from postmortem.analysis.pace import TIMER_START_DELAY_S, boss_count, build_pace, progress_points

PAR_MS = 1_800_000


def _report(**over):
    """A completed key: 2 bosses, 100 forces required, three pulls."""
    r = {
        "run": {"completed": True, "keystone_level": 10, "duration_ms": 1_700_000},
        "timer": {"par_ms": PAR_MS},
        "death_cost": {"per_death_s": 15.0},
        "forces": {"required": 100, "timeline": [
            {"t": 100.0, "forces": 25}, {"t": 400.0, "forces": 50},
            {"t": 900.0, "forces": 100}]},
        "encounters": [
            {"encounter_id": 1, "name": "A", "kill": True, "t": 450.0, "duration_s": 90.0},
            {"encounter_id": 2, "name": "B", "kill": True, "t": 1500.0, "duration_s": 150.0},
        ],
        "pulls": [{"pull": 1, "t_end": 120.0}, {"pull": 2, "t_end": 560.0},
                  {"pull": 3, "t_end": 1660.0}],
        "deaths": [],
    }
    r.update(over)
    return r


def _point(pace, pull):
    return next(p for p in pace["points"] if p["pull"] == pull)


def test_progress_halves_forces_and_bosses():
    # pull 2 ends at 560: 50 of 100 forces, boss A (450 + 90 = 540) dead
    pts = progress_points(_report())
    assert pts[1]["progress"] == pytest.approx(0.5)


def test_forces_are_capped_at_one():
    r = _report(forces={"required": 100, "timeline": [{"t": 100.0, "forces": 110}]},
                encounters=[])
    # no bosses killed in a completed run -> bosses half dropped, forces alone
    assert progress_points(r)[0]["progress"] == pytest.approx(1.0)
    r["encounters"] = _report()["encounters"]
    assert progress_points(r)[0]["progress"] == pytest.approx(0.5)


def test_bosses_only_without_dungeon_data():
    r = _report(forces={"required": None, "timeline": []},
                encounters=[{"encounter_id": i, "name": str(i), "kill": True,
                             "t": 50.0 + 500 * i, "duration_s": 10.0} for i in range(4)])
    # pull 1 ends at 120: only boss 0 (50+10) is dead, of 4
    assert progress_points(r)[0]["progress"] == pytest.approx(0.25)


def test_unfinished_run_without_total_drops_bosses():
    r = _report(run={"completed": False})
    # pull 2: 50% forces; bosses half unknown -> forces alone
    assert progress_points(r)[1]["progress"] == pytest.approx(0.5)
    # with a total of 4 bosses from elsewhere: 0.5*0.5 + 0.5*1/4
    assert progress_points(r, total_bosses=4)[1]["progress"] == pytest.approx(0.375)


def test_game_time_adds_the_death_penalty_so_far():
    r = _report(deaths=[{"t": 100.0}, {"t": 500.0}, {"t": 600.0}])
    pts = progress_points(r)
    d = TIMER_START_DELAY_S
    assert pts[0]["game_s"] == pytest.approx(120.0 - d + 15)
    assert pts[1]["game_s"] == pytest.approx(560.0 - d + 30)  # the 600s death is later


def test_game_time_starts_after_the_countdown():
    # 73 of 78 real keys: in-game = wall - 8.4s - deaths x penalty
    assert TIMER_START_DELAY_S == pytest.approx(8.4)
    r = _report(pulls=[{"pull": 1, "t_end": 5.0}, {"pull": 2, "t_end": 120.0}])
    pts = progress_points(r)
    assert pts[0]["game_s"] == 0.0  # never negative
    assert pts[1]["game_s"] == pytest.approx(120.0 - 8.4)


def test_timer_pace():
    pace = build_pace(_report())
    assert pace["benchmark"]["kind"] == "timer"
    p2 = _point(pace, 2)  # progress 0.5, game 560
    assert p2["bench_s"] == pytest.approx(900.0)
    assert p2["delta_s"] == pytest.approx(560.0 - TIMER_START_DELAY_S - 900.0)


def test_change_is_the_difference_between_deltas():
    # timer pace at par 1800: bench = 1800 * progress
    pace = build_pace(_report())
    d = [p["delta_s"] for p in pace["points"]]
    c = [p["change_s"] for p in pace["points"]]
    assert c[0] == pytest.approx(d[0])
    assert c[1] == pytest.approx(d[1] - d[0])
    assert c[2] == pytest.approx(d[2] - d[1])
    assert pace["final_delta_s"] == pytest.approx(d[-1])


def _bench():
    """Benchmark whose pulls sit at progress 0.4 (600s) and 0.8 (1200s),
    finishing in 1500s. Bosses only, 5 of them, to make progress exact."""
    return {
        "run": {"completed": True, "keystone_level": 10, "duration_ms": 1_500_000},
        "death_cost": {"per_death_s": 15.0},
        "forces": {"required": None},
        "encounters": [{"encounter_id": i, "name": str(i), "kill": True,
                        "t": t, "duration_s": 10.0}
                       for i, t in enumerate([100, 500, 900, 1100, 1400])],
        "pulls": [{"pull": 1, "t_end": 600.0}, {"pull": 2, "t_end": 1200.0}],
        "deaths": [],
    }


def test_own_best_interpolates_including_the_endpoints():
    # this run: bosses only, 5 bosses; pull 1 at 3/5 = 0.6, pull 2 at 1.0
    run = _bench()
    run["encounters"] = [{"encounter_id": i, "name": str(i), "kill": True,
                          "t": t, "duration_s": 10.0}
                         for i, t in enumerate([50, 100, 150, 700, 800])]
    run["pulls"] = [{"pull": 1, "t_end": 200.0}, {"pull": 2, "t_end": 900.0}]
    info = {"level": 10, "duration_s": 1500.0, "date": "2026-09-28", "same_level": True}
    pace = build_pace(run, benchmark=_bench(), benchmark_info=info)
    assert pace["benchmark"]["kind"] == "own_best"
    assert pace["benchmark"]["level"] == 10
    assert pace["benchmark"]["date"] == "2026-09-28"
    assert pace["benchmark"]["same_level"] is True
    # halfway between the benchmark's pulls, which end at 600 and 1200 on
    # the wall clock -- 8.4s less each on the in-game timer
    assert _point(pace, 1)["bench_s"] == pytest.approx(900.0 - TIMER_START_DELAY_S)
    assert _point(pace, 2)["bench_s"] == pytest.approx(1500.0)  # the final endpoint


def test_unfinished_run_takes_the_boss_total_from_the_benchmark():
    run = _report(run={"completed": False}, timer={})
    bench = _report()
    pace = build_pace(run, benchmark=bench, benchmark_info={})
    # pull 2: 0.5*0.5 + 0.5*(1/2) -> 0.5, not forces alone
    assert _point(pace, 2)["progress"] == pytest.approx(0.5)
    assert boss_count(bench) == 2
    assert boss_count(run) is None


BAD = ["x", float("nan"), True, None, [], {}]


@pytest.mark.parametrize("bad", BAD)
@pytest.mark.parametrize("field", ["pulls", "deaths", "forces", "timer", "encounters",
                                   "death_cost", "run"])
def test_hostile_values_never_raise(field, bad):
    r = _report()
    r[field] = bad
    pace = build_pace(r)
    assert pace is None or isinstance(pace, dict)
    build_pace(_report(), benchmark=r, benchmark_info={"level": bad})


@pytest.mark.parametrize("bad", BAD)
def test_bad_values_inside_lists_are_skipped(bad):
    r = _report()
    r["pulls"] = [{"pull": 1, "t_end": bad}, {"pull": 2, "t_end": 560.0}, bad]
    r["deaths"] = [{"t": bad}, bad]
    r["forces"]["timeline"].append({"t": bad, "forces": bad})
    r["encounters"].append({"kill": True, "t": bad, "duration_s": bad})
    pts = progress_points(r)
    assert [p["pull"] for p in pts] == [2]


def test_unsorted_forces_timeline_is_read_in_time_order():
    r = _report()
    r["forces"]["timeline"].reverse()
    assert progress_points(r)[1]["progress"] == pytest.approx(0.5)


def test_no_progress_source_means_no_pace():
    r = _report(run={"completed": False}, forces={"required": None})
    assert build_pace(r) is None


def test_no_timer_and_no_benchmark_means_no_pace():
    assert build_pace(_report(timer={})) is None


def test_a_completed_run_reaches_full_forces_even_when_the_data_counts_short():
    # Real Kings' Rest +17 (2026-10-07): the bundled counts summed to 96.5%
    # of required, but a finished key always had 100% in game.
    r = _report()
    r["forces"]["timeline"][-1]["forces"] = 96.5
    pts = progress_points(r)
    assert pts[-1]["progress"] == pytest.approx(1.0)
    assert pts[1]["progress"] == pytest.approx(0.5 * 50 / 96.5 + 0.5 * 0.5)


def test_an_unfinished_run_keeps_the_data_count():
    r = _report(run={"completed": False})
    r["forces"]["timeline"][-1]["forces"] = 96.5
    assert progress_points(r, total_bosses=2)[-1]["progress"] == pytest.approx(
        0.5 * 0.965 + 0.5)
