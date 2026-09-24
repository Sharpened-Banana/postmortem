"""The verdict band: "where the key was lost" (analysis/verdict.py).

Built only from fields a finished report already carries, so every case
here is a hand-made report dict. The hostile cases matter as much as the
happy ones: on the public site a report is an anonymous upload, and a
field the schema calls a number can be text, a bool or NaN.
"""

from __future__ import annotations

import json
import math

import pytest

from postmortem.analysis.verdict import build_verdict

from test_report_escaping import (  # noqa: F401
    PAYLOADS, _poison, real_report,
)

PAR = 1_800_000


def _report(**over):
    """A completed key with a timer, 3 deaths, a failed boss attempt, one
    long idle gap and an unplanned pull."""
    r = {
        "run": {"zone": "Murder Row", "keystone_level": 10, "completed": True,
                "timed": True, "duration_ms": PAR - 72_000, "wall_duration_s": 1700.0},
        "timer": {"par_ms": PAR, "threshold_2_ms": 1_440_000, "threshold_3_ms": 1_080_000,
                  "margin_ms": 72_000, "threshold": 1},
        "death_cost": {"deaths": 3, "per_death_s": 15.0, "total_s": 45.0},
        "deaths": [
            {"player": "A-Realm-US", "pull": 7, "killing_blow": {"spell": "Cosmic Ascension", "spell_id": 111}},
            {"player": "B-Realm-US", "pull": 7, "killing_blow": {"spell": "Cosmic Ascension", "spell_id": 111}},
            {"player": "C-Realm-US", "pull": 2, "killing_blow": {"spell": "Melee", "spell_id": 1}},
        ],
        "encounters": [{"name": "Boss", "kill": False, "duration_s": 30.0},
                       {"name": "Boss", "kill": True, "duration_s": 120.0}],
        "downtime": {"windows": [{"seconds": s, "after_pull": i, "before_pull": i + 1}
                                 for i, s in enumerate([8, 9, 10, 7, 11, 70], start=1)]},
        "pulls": [{"pull": 1, "duration_s": 60.0}, {"pull": 2, "duration_s": 25.0, "boss": None}],
        "comparison": {"pulls": [{"actual_pull": 1, "primary_plan_pull": 1},
                                 {"actual_pull": 2, "primary_plan_pull": None}]},
        "enemy_casts": {"spells": [{"name": "Cosmic Ascension", "spell_id": 111, "got_through": 3,
                                    "kicked": 5, "interruptible": True}]},
        "forces": {"pct": 101.0},
    }
    r.update(over)
    return r


def _all_text(v: dict) -> str:
    return json.dumps(v, ensure_ascii=False)


def test_timed_headline_stars_and_ranked_lines():
    v = build_verdict(_report())
    assert v["outcome"] == "timed"
    assert v["headline"] == "Timed +1 with 1:12 to spare"
    assert v["chests"] == 1
    kinds = [l["kind"] for l in v["lines"]]
    # ranked by seconds: downtime 70-9.5=60.5s, deaths 45s, wipe 30s (route 25s is 4th)
    assert kinds == ["downtime", "deaths", "wipe"]
    secs = [l["seconds"] for l in v["lines"]]
    assert secs == sorted(secs, reverse=True)
    deaths = v["lines"][1]["text"]
    assert deaths.startswith("Deaths cost 45s (3 × 15s penalty)")
    assert "30s of it on pull 7" in deaths
    assert "typical" in v["lines"][0]["text"]


def test_a_better_chest_is_measured_against_its_own_threshold():
    r = _report(timer={"par_ms": PAR, "threshold_2_ms": 1_440_000, "threshold_3_ms": 1_080_000,
                       "margin_ms": PAR - 1_368_000, "threshold": 2})
    v = build_verdict(r)
    assert v["headline"] == "Timed +2 with 1:12 to spare"
    assert v["chests"] == 2


def test_near_miss_of_the_next_chest_is_mentioned():
    r = _report(timer={"par_ms": PAR, "threshold_2_ms": 1_440_000, "threshold_3_ms": 1_080_000,
                       "margin_ms": PAR - 1_500_000, "threshold": 1})
    assert build_verdict(r)["context"] == "1:00 short of a +2."


def test_over_explained_by_the_death_penalty():
    r = _report(timer={"par_ms": PAR, "margin_ms": -14_000, "threshold": 0})
    r["run"]["timed"] = False
    v = build_verdict(r)
    assert v["outcome"] == "over" and v["headline"] == "Over by 14s"
    assert v["chests"] == 0
    assert "more than the 14s" in v["context"]


def test_over_by_more_than_the_deaths_explain():
    r = _report(timer={"par_ms": PAR, "margin_ms": -300_000, "threshold": 0})
    v = build_verdict(r)
    assert v["headline"] == "Over by 5:00"
    assert v["context"] == "The death penalty explains 45s of the 5:00."


def test_depleted_by_deaths_only():
    r = {"run": {"completed": True}, "timer": {"par_ms": PAR, "margin_ms": -20_000},
         "death_cost": {"deaths": 4, "per_death_s": 15, "total_s": 60},
         "deaths": [{"pull": 3}, {"pull": 3}, {"pull": 3}, {"pull": 3}]}
    v = build_verdict(r)
    assert v["lines"] == [{"kind": "deaths", "seconds": 60.0,
                           "text": "Deaths cost 1:00 (4 × 15s penalty), all on pull 3"}]
    assert "more than the 20s" in v["context"]


def test_abandoned_reads_forces():
    r = _report()
    r["run"]["completed"] = False
    r["forces"] = {"pct": 62}
    v = build_verdict(r)
    assert v["outcome"] == "abandoned"
    assert v["headline"] == "Abandoned at 62% forces"
    assert v["chests"] is None


def test_abandoned_without_forces_uses_the_clock_and_truncated_is_partial():
    v = build_verdict({"run": {"completed": False, "wall_duration_s": 754}})
    assert v["headline"] == "Abandoned after 12:34"
    assert build_verdict({"run": {"truncated": True}})["outcome"] == "partial"


def test_completed_with_no_known_timer():
    v = build_verdict({"run": {"completed": True, "timed": None, "duration_ms": 1_725_000}})
    assert v["outcome"] == "completed" and v["headline"] == "Completed in 28:45"


def test_interruptible_cast_that_killed_is_a_note_without_seconds():
    v = build_verdict(_report())
    spell = [n for n in v["notes"] if n["kind"] == "spell"]
    assert spell and spell[0]["text"] == (
        "Cosmic Ascension got through 3 of 8 casts and landed 2 killing blows")
    assert "seconds" not in spell[0]


def test_unknown_penalty_quotes_no_seconds():
    r = _report(death_cost=None, encounters=[], comparison=None)
    line = next(l for l in build_verdict(r)["lines"] if l["kind"] == "deaths")
    assert line["seconds"] is None and "unknown" in line["text"]


def test_steady_downtime_is_said_when_nothing_stood_out():
    r = _report(downtime={"windows": [{"seconds": s} for s in (8, 9, 10, 12)]})
    notes = [n["text"] for n in build_verdict(r)["notes"]]
    assert "Downtime between pulls was steady (longest gap 12s)" in notes


@pytest.mark.parametrize("report", [{}, None, [], "x", {"run": None}, {"run": {"completed": True}}])
def test_missing_fields_never_raise(report):
    v = build_verdict(report)
    assert isinstance(v["headline"], str) and v["headline"]
    assert v["lines"] == [] or all(isinstance(l["text"], str) for l in v["lines"])


HOSTILE = ["12", "abc", True, float("nan"), float("inf"), [], {}, None, -5]


@pytest.mark.parametrize("bad", HOSTILE, ids=repr)
def test_hostile_types_do_not_crash_or_print_nan(bad):
    r = _report()
    r["timer"] = {k: bad for k in r["timer"]}
    r["death_cost"] = {k: bad for k in r["death_cost"]}
    r["forces"] = {"pct": bad}
    r["run"]["completed"] = bad
    r["run"]["wall_duration_s"] = bad
    for d in r["deaths"]:
        d["pull"] = bad
        d["killing_blow"] = bad
    r["encounters"] = [{"kill": False, "duration_s": bad, "name": bad}]
    r["downtime"] = {"windows": [{"seconds": bad}] * 5}
    r["pulls"] = [{"pull": bad, "duration_s": bad}]
    r["comparison"] = {"pulls": [{"actual_pull": bad, "primary_plan_pull": None}]}
    r["enemy_casts"] = {"spells": [{"got_through": bad, "kicked": bad, "spell_id": bad}]}
    v = build_verdict(r)
    text = _all_text(v).lower()
    assert "nan" not in text and "inf" not in text
    for line in v["lines"]:
        assert line["seconds"] is None or math.isfinite(line["seconds"])


@pytest.mark.parametrize("payload_name", sorted(PAYLOADS))
def test_a_poisoned_real_report_still_builds(real_report, payload_name):  # noqa: F811
    v = build_verdict(_poison(real_report, PAYLOADS[payload_name]))
    assert isinstance(v["headline"], str)
    assert "nan" not in _all_text(v).lower()


def test_analyze_run_attaches_a_verdict(real_report):  # noqa: F811
    assert real_report["verdict"]["version"] == 1
    assert real_report["verdict"]["headline"]

