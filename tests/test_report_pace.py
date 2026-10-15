"""The pace line on the report page: a Pace section with its caption and
chart, and a Pace column in the Pulls table (report/html.py)."""

from __future__ import annotations

import copy
import json
import re

import pytest

from postmortem.report.html import render_html

from test_report_escaping import NODE, _extract_script, _run, real_report  # noqa: F401

pytestmark = pytest.mark.skipif(NODE is None, reason="needs node to run the page's own JS")


def _rendered(report: dict) -> str:
    return _run(_extract_script(render_html(report)), "report-data", json.dumps(report),
                extra="render();")


def _with_pace(report: dict, benchmark: dict, points=None) -> dict:
    r = copy.deepcopy(report)
    r["run"]["keystone_level"] = 17
    r["pace"] = {
        "version": 1,
        "benchmark": benchmark,
        "points": points if points is not None else [
            {"pull": 1, "t_end": 60.0, "progress": 0.3, "game_s": 60.0, "bench_s": 39.0,
             "delta_s": 21.0, "change_s": 21.0},
            {"pull": 2, "t_end": 140.0, "progress": 0.7, "game_s": 140.0, "bench_s": 131.0,
             "delta_s": 9.0, "change_s": -12.0},
        ],
        "final_delta_s": 9.0,
    }
    return r


OWN_17 = {"kind": "own_best", "level": 17, "duration_s": 1600.0, "date": "2026-09-28",
          "same_level": True}


def test_pace_section_caption_own_best(real_report):  # noqa: F811
    out = _rendered(_with_pace(real_report, OWN_17))
    assert 'class="sec-title">Pace<' in out
    assert "vs your best +17 (26:40, Sep 28)" in out


def test_pace_caption_nearest_level(real_report):  # noqa: F811
    bench = {**OWN_17, "level": 16, "same_level": False}
    out = _rendered(_with_pace(real_report, bench))
    assert "vs your best +16 (no timed +17 yet)" in out


def test_pace_caption_timer(real_report):  # noqa: F811
    out = _rendered(_with_pace(real_report, {"kind": "timer", "par_s": 1800.0}))
    assert "vs the timer (no timed run of this dungeon yet)" in out
    assert len(re.findall(r'class="pace-chest', out)) == 2  # +2 and +3 lines


def test_pulls_table_pace_column(real_report):  # noqa: F811
    out = _rendered(_with_pace(real_report, OWN_17))
    pulls = out[out.index('class="sec-title">Pulls'):]
    assert 'class="num">Pace</th>' in pulls
    assert re.search(r'<td class="num dev-off"[^>]*>\+21s</td>', pulls)
    assert re.search(r'<td class="num ok"[^>]*>−12s</td>', pulls)


def test_no_pace_no_section(real_report):  # noqa: F811
    r = copy.deepcopy(real_report)
    r["pace"] = {"version": 1, "benchmark": {"kind": "timer"}, "points": []}
    out = _rendered(r)
    assert 'class="sec-title">Pace<' not in out
    assert ">Pace</th>" not in out


def test_hostile_pace_is_escaped(real_report):  # noqa: F811
    evil = "</script><img src=x onerror=alert(1)>"
    bench = {"kind": "own_best", "level": evil, "duration_s": evil, "date": evil,
             "same_level": evil}
    points = [{"pull": evil, "t_end": "60", "delta_s": evil, "change_s": "x",
               "bench_s": None}, "junk", None]
    out = _rendered(_with_pace(real_report, bench, points))
    assert "<img" not in out
    assert "onerror=alert" not in out.replace("onerror=alert(1)&gt;", "")
