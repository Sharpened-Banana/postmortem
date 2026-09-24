"""Collapsible report sections: every section heading carries a real
<button> that folds the section (aria-expanded / aria-controls, keyboard
reachable), the report has Collapse all / Expand all, sections default to
open, and a folded section is remembered in localStorage by its id.

The page's own script runs under node with the same stubbed document the
escaping tests use; the fold/persist handlers are exercised by handing the
delegated listeners hand-built fake elements."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from postmortem.report.html import render_html

from test_report_escaping import NODE, _extract_script, _run, real_report  # noqa: F401

pytestmark = pytest.mark.skipif(NODE is None, reason="needs node to run the page's own JS")


def _rendered(report: dict, extra: str = "render();") -> str:
    return _run(_extract_script(render_html(report)), "report-data", json.dumps(report),
                extra=extra)


class _Sections(HTMLParser):
    """Pairs each details.sec with its toggle button and body div."""

    def __init__(self):
        super().__init__()
        self.sections: list[dict] = []
        self._open: list[str] = []
        self.controls: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "details" and "sec" in (a.get("class") or "").split():
            self._open.append(a["id"])
            self.sections.append({"id": a["id"], "open": "open" in a})
        elif tag == "button" and a.get("class") == "sec-toggle":
            self.sections[-1]["button"] = a
        elif tag == "div" and a.get("class") == "sec-body":
            self.sections[-1]["body"] = a.get("id")
        elif tag == "button" and a.get("class") == "sec-all":
            self.controls.append(a.get("data-all"))
            assert not self.sections, "controls must come before the first section"


def _parse(out: str) -> _Sections:
    p = _Sections()
    p.feed(out)
    return p


def test_every_section_has_an_accessible_toggle_wired_to_its_body(real_report):  # noqa: F811
    p = _parse(_rendered(real_report))
    assert len(p.sections) >= 6
    for s in p.sections:
        b = s["button"]
        assert b["type"] == "button", s["id"]
        assert b["aria-expanded"] == "true"
        assert b["aria-controls"] == s["id"] + "-body" == s["body"]
        assert b["aria-label"].startswith("Collapse ")
        assert s["open"], "sections default to expanded"


def test_section_ids_are_slugged_titles_and_unique(real_report):  # noqa: F811
    ids = [s["id"] for s in _parse(_rendered(real_report)).sections]
    assert len(ids) == len(set(ids))
    assert ids[0] == "sec-pull-timeline"
    assert "sec-players" in ids and "sec-deaths" in ids
    assert all(re.fullmatch(r"sec-[a-z0-9-]+", i) for i in ids), ids
    # the enemy-casts "(n kicks total)" tail never reaches the id
    assert "sec-enemy-casts-kicked-vs-got-through" in ids


def test_collapse_all_and_expand_all_sit_above_the_sections(real_report):  # noqa: F811
    out = _rendered(real_report)
    assert _parse(out).controls == ["collapse", "expand"]
    # the phone index sits right under the verdict, ahead of the numbers;
    # the controls sit between the numbers and the first section
    assert out.index('class="verdict') < out.index('class="sec-index"') < out.index('class="kpis"')
    assert out.index('class="kpis"') < out.index('class="sec-controls"') < out.index('<details class="sec"')
    assert ">Collapse all</button>" in out and ">Expand all</button>" in out


def test_a_section_folded_last_time_renders_folded(real_report):  # noqa: F811
    extra = """
    global.localStorage = { getItem: k => k === "postmortem.report.collapsed"
      ? JSON.stringify({"sec-players": 1, "sec-nope": 1}) : null, setItem() {} };
    render();
    """
    p = _parse(_rendered(real_report, extra))
    by_id = {s["id"]: s for s in p.sections}
    assert not by_id["sec-players"]["open"]
    assert by_id["sec-players"]["button"]["aria-expanded"] == "false"
    assert by_id["sec-players"]["button"]["aria-label"] == "Expand Players"
    assert by_id["sec-deaths"]["open"]


@pytest.mark.parametrize("storage", [
    "undefined",                                     # no localStorage at all
    "{ getItem() { throw new Error('denied'); } }",  # access throws (private mode)
    "{ getItem: () => '{not json' }",                # corrupt value
    "{ getItem: () => '[1,2]' }",                    # wrong shape
])
def test_broken_storage_still_renders_everything_open(real_report, storage):  # noqa: F811
    out = _rendered(real_report, f"global.localStorage = {storage}; render();")
    p = _parse(out)
    assert p.sections and all(s["open"] for s in p.sections)


_FAKE_DOM = """
const store = {};
global.localStorage = { getItem: k => store[k] ?? null,
  setItem: (k, v) => { store[k] = v; } };
const app = document.getElementById("app");
app._l = {};
app.addEventListener = (type, fn) => { app._l[type] = fn; };
sectionsWired = false;  // the page already rendered once at load with the stub
render();
const mkBtn = () => ({ attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } });
const mkSec = (id, open) => {
  const btn = mkBtn();
  const d = { id, open, tagName: "DETAILS", classList: { contains: c => c === "sec" },
    btn, querySelector: sel => sel === ".sec-toggle" ? btn : { textContent: " Players " } };
  return d;
};
const secs = [mkSec("sec-players", true), mkSec("sec-deaths", true)];
app.querySelectorAll = sel => sel === "details.sec" ? secs : [];
// an event whose target is `el`; closest() walks a hand-built chain
const evt = (el) => ({ target: el, prevented: false, preventDefault() { this.prevented = true; } });
const node = (cls, parent) => {
  const n = { cls, parent, getAttribute: k => n.attrs[k], attrs: {} };
  n.closest = sel => {
    for (let x = n; x; x = x.parent) if (x.matches && x.matches(sel)) return x;
    return null;
  };
  return n;
};
"""


def test_toggle_button_click_folds_its_section_once_and_persists(real_report):  # noqa: F811
    extra = _FAKE_DOM + """
    const d = secs[0];
    d.matches = sel => sel === "details.sec";
    const btn = node("sec-toggle", d);
    btn.matches = sel => sel === ".sec-toggle";
    const e = evt(btn);
    app._l.click(e);
    console.log("CLICK=" + d.open + "|" + e.prevented);
    // the browser fires "toggle" after open changes; that is where aria and storage follow
    app._l.toggle({ target: d });
    console.log("TOGGLE=" + d.btn.attrs["aria-expanded"] + "|" + d.btn.attrs["aria-label"]
      + "|" + store["postmortem.report.collapsed"]);
    d.open = true; app._l.toggle({ target: d });
    console.log("REOPEN=" + d.btn.attrs["aria-expanded"] + "|" + store["postmortem.report.collapsed"]);
    """
    out = _rendered(real_report, extra)
    assert "CLICK=false|true" in out          # folded, and the summary's own toggle suppressed
    assert 'TOGGLE=false|Expand Players|{"sec-players":1}' in out
    assert "REOPEN=true|{}" in out            # expanding forgets it again


def test_collapse_all_and_expand_all_drive_every_section(real_report):  # noqa: F811
    extra = _FAKE_DOM + """
    const b = node("sec-all", null); b.matches = sel => sel === ".sec-all";
    b.attrs["data-all"] = "collapse"; app._l.click(evt(b));
    console.log("ALL1=" + secs.map(s => s.open).join(","));
    b.attrs["data-all"] = "expand"; app._l.click(evt(b));
    console.log("ALL2=" + secs.map(s => s.open).join(","));
    """
    out = _rendered(real_report, extra)
    assert "ALL1=false,false" in out
    assert "ALL2=true,true" in out


def test_index_chip_still_opens_its_section(real_report):  # noqa: F811
    extra = _FAKE_DOM + """
    const d = secs[1]; d.open = false;
    __els["sec-deaths"] = d;
    const chip = node("chip", null); chip.matches = sel => sel === ".sec-index a[href^='#']";
    chip.attrs["href"] = "#sec-deaths";
    app._l.click(evt(chip));
    console.log("CHIP=" + d.open);
    """
    assert "CHIP=true" in _rendered(real_report, extra)


def test_handlers_attach_once_across_renders(real_report):  # noqa: F811
    extra = """
    const app = document.getElementById("app");
    let n = 0; app.addEventListener = () => { n++; };
    sectionsWired = false;  // the page already rendered once at load with the stub
    render(); render();
    console.log("LISTENERS=" + n);
    """
    assert "LISTENERS=2" in _rendered(real_report, extra)  # click + toggle, not 4


def test_toggle_css_is_not_media_gated_and_the_marker_is_hidden():
    page = render_html({"run": {}, "dungeon": {"name": "x"}})
    css = page[page.index("<style>"):page.index("</style>")]
    at = css.index("@media (max-width: 720px)")
    for rule in (".sec-toggle::before { content: \"\\25BE\"; }",
                 "details.sec:not([open]) > summary .sec-toggle::before { content: \"\\25B8\"; }",
                 "details.sec > summary::-webkit-details-marker { display: none; }",
                 ".sec-controls {"):
        assert css.index(rule) < at, rule
    assert "pointer-events: none" not in css   # the heading is clickable everywhere now
    # brand: the buttons are mono pills that go gold on hover, nothing new
    assert ".sec-toggle:hover, .sec-all:hover { color: var(--accent);" in css
