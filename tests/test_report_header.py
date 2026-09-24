"""Run report redesign (2026-09-23): the verdict band under the title,
four primary KPI tiles plus a secondary strip, affix chips by name, class
coloured player names, the phone defaults, and the pulls / timeline /
legend polish. Runs the page's real JS under node like the other report
tests."""

from __future__ import annotations

import json
import re

import pytest

from postmortem.analysis.verdict import build_verdict
from postmortem.report import affixes
from postmortem.report.html import _TEMPLATE, render_html
from postmortem.report.index import _INDEX_TEMPLATE

from test_report_escaping import NODE, _extract_script, _run, real_report  # noqa: F401
from test_verdict import _report

needs_node = pytest.mark.skipif(NODE is None, reason="needs node to run the page's own JS")


def _rendered(report: dict, extra: str = "render();") -> str:
    return _run(_extract_script(render_html(report)), "report-data", json.dumps(report), extra=extra)


# --- verdict band ----------------------------------------------------------


def test_render_html_builds_one_for_an_old_report(real_report):  # noqa: F811
    old = json.loads(json.dumps(real_report))
    del old["verdict"]
    page = render_html(old)
    assert '"verdict": {' in page
    assert "verdict" not in old, "render_html must not mutate the caller's report"


@needs_node
def test_band_renders_under_the_title(real_report):  # noqa: F811
    report = json.loads(json.dumps(real_report))
    report["verdict"] = build_verdict(_report())
    out = _rendered(report)
    assert out.index("</h1>") < out.index('<section class="verdict timed"')
    assert "Timed +1 with 1:12 to spare" in out
    assert '<span class="v-s">-1:01</span>' in out  # the downtime line
    assert 'aria-label="1 of 3 chests"' in out


@needs_node
def test_band_treats_a_stored_verdict_as_hostile(real_report):  # noqa: F811
    report = json.loads(json.dumps(real_report))
    report["verdict"] = {"outcome": 'x" onclick="alert(1)', "headline": "<img src=x onerror=alert(1)>",
                         "chests": "abc", "lines": [{"kind": "<b>", "seconds": "12", "text": "t"}, "junk"],
                         "notes": "nope"}
    out = _rendered(report)
    assert '<section class="verdict completed"' in out
    assert "<img" not in out and "onclick" not in out
    assert '<span class="v-s none">—</span>' in out  # "12" is not a number of seconds
    assert "v-stars" not in out


# --- KPI tiles -------------------------------------------------------------


@needs_node
def test_four_primary_tiles_at_most_and_the_rest_in_the_strip(real_report):  # noqa: F811
    out = _rendered(real_report)
    kpis = out[out.index('<div class="kpis">'):out.index('<div class="kpi2">')]
    assert 1 <= kpis.count('<div class="stat">') <= 4
    assert "player deaths" in kpis or "player death" in kpis
    strip = out[out.index('<div class="kpi2">'):out.index('class="sec-controls"')]
    assert "pulls</span>" in strip and "combat</span>" in strip


# --- affixes -----------------------------------------------------------------


def _js_table(template: str, name: str) -> dict[str, str]:
    body = re.search(r"const " + name + r" = \{(.*?)\};", template, re.S).group(1)
    return {k: v for k, v in re.findall(r"""(\d+):\s*"((?:[^"\\]|\\.)*)\"""", body)}


@pytest.mark.parametrize("name", ["AFFIXES", "AFFIX_SHORT", "AFFIX_KIND"])
def test_feed_and_report_share_the_same_affix_tables(name):
    """report/index.py keeps its own copy (another page, another owner);
    this keeps it and report/affixes.py identical."""
    shared = {str(k): v for k, v in getattr(affixes, name).items()}
    assert _js_table(_INDEX_TEMPLATE, name) == shared
    assert f"const {name} = " in _TEMPLATE and "__AFFIX_JS__" not in _TEMPLATE


@needs_node
def test_affixes_render_as_named_chips(real_report):  # noqa: F811
    report = json.loads(json.dumps(real_report))
    report["run"]["affixes"] = [9, 147, 999]
    out = _rendered(report)
    assert '<span class="affix base" title="Tyrannical">Tyrannical</span>' in out
    assert "<span class=\"affix harsh\" title=\"Xal&#39;atath&#39;s Guile\">" in out
    assert '<span class="affix" title="Affix #999">Affix #999</span>' in out
    assert "affixes 9" not in out


# --- player names ----------------------------------------------------------


def _lum(h: str) -> float:
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def test_class_colours_meet_wcag_aa_on_every_dark_surface():
    body = re.search(r"const CLASS_TEXT = \{(.*?)\};", _TEMPLATE, re.S).group(1)
    colours = dict(re.findall(r'(\w+): "(#[0-9A-Fa-f]{6})"', body))
    assert len(colours) == 13
    for surface in ("#0E0D0C", "#141210", "#1A1714"):
        for cls, col in colours.items():
            hi, lo = sorted((_lum(col), _lum(surface)), reverse=True)
            assert (hi + 0.05) / (lo + 0.05) >= 4.5, (cls, col, surface)


@needs_node
def test_names_are_class_coloured_with_realm_dimmed_and_region_dropped(real_report):  # noqa: F811
    report = json.loads(json.dumps(real_report))
    p = report["players"][0]
    p["name"], p["class"] = "Kolt-Moonrunner-US", "Shaman"
    report["deaths"][0]["player"] = "Kolt-Moonrunner-US"
    out = _rendered(report)
    tag = ('<span class="pn" style="color:#2484E2" title="Kolt-Moonrunner-US">Kolt</span>'
           '<span class="realm">-Moonrunner</span>')
    assert out.count(tag) >= 2  # players table and deaths table
    assert ">Kolt-Moonrunner-US<" not in out


@needs_node
def test_hostile_class_names_pick_no_colour(real_report):  # noqa: F811
    report = json.loads(json.dumps(real_report))
    report["players"][0]["class"] = "__proto__"
    out = _rendered(report)
    assert "[object Object]" not in out


# --- phone layout ------------------------------------------------------------


_PHONE = """
global.window = { matchMedia: q => ({ matches: q.includes("720px") }) };
render();
"""


@needs_node
def test_long_tables_start_closed_on_a_phone_only(real_report):  # noqa: F811
    phone = _rendered(real_report, extra=_PHONE)
    desktop = _rendered(real_report)
    for sid in ("sec-pulls", "sec-enemy-casts-kicked-vs-got-through"):
        assert f'<details class="sec" id="{sid}">' in phone, sid
        assert f'<details class="sec" open id="{sid}">' in desktop, sid
    assert '<details class="sec" open id="sec-players">' in phone


@needs_node
def test_a_phone_reader_who_opened_a_section_gets_it_open(real_report):  # noqa: F811
    extra = """
    global.localStorage = { getItem: () => JSON.stringify({"sec-pulls": 0}), setItem() {} };
    """ + _PHONE
    assert '<details class="sec" open id="sec-pulls">' in _rendered(real_report, extra=extra)


@needs_node
def test_player_cards_mark_the_numbers_a_phone_hides(real_report):  # noqa: F811
    out = _rendered(real_report)
    assert '<td class="num xtra" data-l="Damage">' in out
    assert '<td class="num" data-l="Taken">' in out and '<td class="num" data-l="Kicks">' in out
    assert '<div class="p-more">' in out
    css = _TEMPLATE[_TEMPLATE.index("@media (max-width: 720px)"):]
    assert "table.cards.players td.xtra { display: none; }" in css


# --- pulls, timeline, legends -----------------------------------------------


@needs_node
def test_pull_rows_with_deaths_and_long_packs(real_report):  # noqa: F811
    report = json.loads(json.dumps(real_report))
    p = report["pulls"][0]
    p["player_deaths"] = 2
    p["npcs"] = [{"n": 1, "name": f"Mob {i}"} for i in range(7)]
    out = _rendered(report)
    assert '<tr class="died">' in out
    assert '<details class="more"><summary>+4 more</summary>' in out


@needs_node
def test_timeline_bars_are_tappable_and_legends_are_chips(real_report):  # noqa: F811
    out = _rendered(real_report)
    assert '<div class="tl-detail" aria-live="polite"></div>' in out
    assert "hover a bar for pack details" not in out  # the old hover-only line
    assert 'class="tl-pull' in out and 'data-pull="1"' in out
    assert '<span class="chip"><i class="ring"></i>route deviation</span>' in out
    assert 'content: "Tap or hover a bar for pack details."' in _TEMPLATE
