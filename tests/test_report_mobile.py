"""Phase 3 of the site's mobile plan: the run report reflows on a phone.
Sections wrap in <details> with a sticky index, the pull timeline gains a
one-row-per-pull layout, and the widest tables carry the labels the card
CSS needs. Desktop markup and rules stay; these pin the switch."""
from __future__ import annotations

import json

import pytest

from postmortem.report.html import render_html
from postmortem.report.snapshot import _table

from test_report_escaping import NODE, _extract_script, _run, real_report  # noqa: F401

pytestmark = pytest.mark.skipif(NODE is None, reason="needs node to run the page's own JS")


def _rendered(report: dict) -> str:
    return _run(_extract_script(render_html(report)), "report-data", json.dumps(report),
                extra="render();")


def test_every_section_is_a_details_with_its_h2_as_summary(real_report):  # noqa: F811
    out = _rendered(real_report)
    assert out.count("<details class=\"sec\" open id=\"sec-") == out.count("<summary><h2>")
    assert out.count("<summary><h2>") >= 6
    # every top-level section h2 is a summary; sections may nest their own
    assert out.count("<h2>") >= out.count("<summary><h2>")
    assert "<summary><h2>Players</h2></summary>" in out


def test_index_has_one_chip_per_section_pointing_at_it(real_report):  # noqa: F811
    out = _rendered(real_report)
    n = out.count('<details class="sec" open id="sec-')
    assert out.count('<a href="#sec-') == n
    assert 'href="#sec-1">Pull timeline</a>' in out
    # the enemy-casts title drops its "(n kicks total)" tail in the chip
    assert ">Enemy casts — kicked vs got through</a>" in out


def test_timeline_has_a_row_per_pull(real_report):  # noqa: F811
    out = _rendered(real_report)
    assert out.count('<div class="tl-srow">') == len(real_report["pulls"])
    assert '<div class="tl-row">' in out  # the desktop strip stays


def test_wide_tables_are_labelled_cards(real_report):  # noqa: F811
    out = _rendered(real_report)
    assert '<table class="cards players">' in out
    assert 'data-l="Kick prev."' in out
    for label in ("Killing blow", "Last hits", "Kick rate"):
        assert f'data-l="{label}"' in out, label


def test_phone_rules_are_media_gated_and_desktop_hides_the_wrapper():
    css = render_html({"run": {}, "dungeon": {"name": "x"}})
    css = css[css.index("<style>"):css.index("</style>")]
    at = css.index("@media (max-width: 720px)")
    assert css.index("details.sec > summary { list-style: none; cursor: default; pointer-events: none; }") < at
    assert css.index(".sec-index { display: none; }") < at
    assert css.index(".tl-stack { display: none; }") < at
    for rule in ("table.cards tr:first-child { display: none; }",
                 ".tl-row { display: none; }", ".sec-index { display: flex;"):
        assert css.index(rule) > at, rule


def test_snapshot_tables_label_cells_and_go_cards_at_five_columns():
    narrow = _table([("A", False), ("B", True)], [["x", 1]])
    assert '<table><tr>' in narrow and 'data-l="B"' in narrow
    wide = _table([(h, False) for h in "ABCDE"], [list("vwxyz")])
    assert '<table class="cards">' in wide
