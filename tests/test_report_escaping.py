"""Both report pages render untrusted data, so render them with hostile
data and check what actually comes out.

Every field in a report originates in an uploaded combat log. On the
public site that upload is anonymous, so a player name, a zone name, an
affix list -- and, because SQLite's typing is advisory, anything the
schema calls a number -- is attacker-chosen text. Two stored cross-site
scripting holes were found this way on 2026-09-11: the feed's row handler
was built as ``onclick="toggle('...')"`` around a key containing the zone
name, and the escape helper covered ``& < > "`` but not ``'``, so a zone
name with an apostrophe closed the JS string literal and everything after
it ran.

These tests execute the REAL renderer in Node against the REAL escape
helpers and assert on the HTML it produces, rather than checking the
source for patterns -- the previous hole was invisible at the source level
(every value was passed through ``esc``; ``esc`` was simply incomplete).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from postmortem.report.html import render_html
from postmortem.report.index import render_index

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="needs node to run the page's own JS")

# Each payload closes a different context and then starts a tag. If any of
# them survives into the rendered HTML unescaped, something interpolated
# report data without going through esc()/plain()/num().
PAYLOADS = {
    "squote": "x');alert(1);//",
    "dquote": 'x" onmouseover="alert(1)',
    "tag": "<img src=x onerror=alert(1)>",
    "backtick": "x`+alert(1)+`",
}


def _extract_script(html: str) -> str:
    m = re.search(r"<script>\n(.*)\n</script>", html, re.S)
    assert m, "inline render <script> block not found"
    return m.group(1)


def _run(script: str, data_id: str, payload_json: str, extra: str = "") -> str:
    """Execute the page's own script with a stubbed document and return the
    HTML it assigned to #app."""
    harness = f"""
class El {{
  constructor() {{ this.innerHTML = ""; this.textContent = ""; }}
  addEventListener() {{}}
}}
const __els = {{ {json.dumps(data_id)}: new El(), "app": new El() }};
__els[{json.dumps(data_id)}].textContent = {json.dumps(payload_json)};
global.document = {{ getElementById: id => __els[id] }};

{script}

{extra}
console.log(document.getElementById("app").innerHTML);
"""
    out = subprocess.run([NODE, "-e", harness], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


class _Executability(HTMLParser):
    """Walks the rendered HTML and records anything that could run code.

    Asserting on substrings is the wrong test here: once a payload is
    escaped, the TEXT "onerror=alert(1)" is still present on the page and
    is entirely harmless. What matters is whether a tag or an attribute
    was actually created, so parse the result and look at the structure.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.handlers = []
        self.uri_attrs = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts.append(tag)
        for name, value in attrs:
            if name.lower().startswith("on"):
                self.handlers.append((tag, name))
            if value and value.strip().lower().startswith(("javascript:", "data:text/html")):
                self.uri_attrs.append((tag, name, value[:40]))


def _assert_neutralised(rendered: str) -> None:
    """Nothing a report can say may become a tag, a handler or a URL."""
    parser = _Executability()
    parser.feed(rendered)
    assert not parser.scripts, "report data opened a <script> element"
    assert not parser.handlers, f"report data created event handlers: {parser.handlers}"
    assert not parser.uri_attrs, f"report data created executable URLs: {parser.uri_attrs}"
    # The payloads should still be VISIBLE -- escaped, not silently dropped,
    # so a real name that happens to contain a bracket still reads correctly.
    assert "alert(1)" in rendered, "payload vanished entirely; the test proves nothing"


def _hostile_feed_rows() -> list[dict]:
    return [{
        "file": "run.json", "html": "run.html",
        "zone": PAYLOADS["squote"],
        "level": PAYLOADS["tag"],
        "start_ts": 1_700_000_000, "date": "2023-11-14 12:00",
        "completed": True, "timed": True, "duration_ms": 1_500_000,
        "wall_s": 1500.0,
        "deaths": PAYLOADS["tag"],
        "death_cost_s": 0.0,
        "forces_pct": PAYLOADS["dquote"],
        "adherence_pct": PAYLOADS["tag"],
        "kick_efficiency_pct": PAYLOADS["backtick"],
        "affixes": [PAYLOADS["tag"]],
        "party": [{"name": PAYLOADS["tag"], "class": PAYLOADS["dquote"], "role": "tank"}],
        "threshold": None, "margin_ms": None,
        "deaths_detail": [{"t": 1.0, "player": PAYLOADS["tag"], "spell": PAYLOADS["squote"]}],
    }]


class TestFeedEscaping:
    @pytest.mark.parametrize("payload_name", sorted(PAYLOADS))
    def test_hostile_run_renders_without_executable_payload(self, payload_name):
        rows = _poison(_hostile_feed_rows(), PAYLOADS[payload_name])
        script = _extract_script(render_index(rows))
        out = _run(script, "runs-data", json.dumps(rows), extra="render();")
        _assert_neutralised(out)

    def test_open_row_detail_is_also_escaped(self):
        """The detail block is a separate template, rendered only when a row
        is open -- the original audit only reached the closed state."""
        rows = _hostile_feed_rows()
        script = _extract_script(render_index(rows))
        extra = 'openKey = runKey(RUNS[0]);\nrender();'
        out = _run(script, "runs-data", json.dumps(rows), extra=extra)
        _assert_neutralised(out)
        assert "deaths" in out, "the detail block did not actually render"

    def test_row_carries_no_inline_event_handler(self):
        """The fix replaced the inline handlers with one delegated listener.
        An inline handler is what made an incomplete escape exploitable, so
        keep them out rather than relying on the escape alone."""
        rows = _hostile_feed_rows()
        out = _run(_extract_script(render_index(rows)), "runs-data",
                   json.dumps(rows), extra="render();")
        assert "onclick=" not in out
        assert "onchange=" not in out
        assert 'data-key=' in out, "rows lost the attribute the listener reads"

    def test_a_number_field_holding_text_does_not_reach_the_page(self):
        rows = _hostile_feed_rows()
        out = _run(_extract_script(render_index(rows)), "runs-data",
                   json.dumps(rows), extra="render();")
        # Non-numeric "numbers" degrade to a placeholder instead of printing.
        assert "+?" in out or "—" in out


def _poison(value, payload):
    """Replace every leaf in a real report with the SAME hostile value, so
    one pass exercises every field the renderer touches. Parametrising over
    the payloads (rather than mixing them) keeps this deterministic: an
    earlier version picked a payload per field by hash(), which is salted
    per process and made the test pass or fail depending on the run."""
    if isinstance(value, dict):
        return {k: _poison(v, payload) for k, v in value.items()}
    if isinstance(value, list):
        return [_poison(v, payload) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (str, int, float)):
        # A "number" that is really text: exactly what SQLite's advisory
        # typing lets through the site's columns.
        return payload
    return value


@pytest.fixture()
def real_report(dungeon_data_file):
    """A genuine analyzed run, so the hostile version below has every key
    the renderer touches rather than only the ones I remembered."""
    from conftest import ROUTE_PRESET, build_run_log
    from postmortem.analysis.run_analyzer import analyze_run
    from postmortem.combatlog.parser import iter_events
    from postmortem.combatlog.segmenter import segment_runs
    from postmortem.mdt.dungeon_data import DungeonDataStore
    from postmortem.mdt.route import Route

    (run,) = list(segment_runs(iter_events(build_run_log().lines)))
    return analyze_run(run, route=Route.from_preset(ROUTE_PRESET),
                       store=DungeonDataStore.load(dungeon_data_file))


class TestRunReportEscaping:
    @pytest.mark.parametrize("payload_name", sorted(PAYLOADS))
    def test_hostile_report_renders_without_executable_payload(self, real_report, payload_name):
        report = _poison(real_report, PAYLOADS[payload_name])
        script = _extract_script(render_html(report))
        out = _run(script, "report-data", json.dumps(report), extra="render();")
        _assert_neutralised(out)

    def test_map_background_must_be_a_real_image_data_uri(self, real_report):
        """The background is spliced into an SVG image href. A non-image URI
        in that slot is dropped rather than rendered."""
        report = json.loads(json.dumps(real_report))
        report.setdefault("map", {}).setdefault("backgrounds", {})["1"] = \
            {"data_uri": "javascript:alert(1)"}
        script = _extract_script(render_html(report))
        out = _run(script, "report-data", json.dumps(report), extra="render();")
        assert "javascript:" not in out

    def test_embedded_json_cannot_close_its_own_script_tag(self, real_report):
        """Belt to the braces above: the report is also embedded as JSON, so
        a payload must not be able to end the <script> block either."""
        report = json.loads(json.dumps(real_report))
        report["run"]["zone"] = "</script><img src=x onerror=alert(1)>"
        html = render_html(report)
        assert "</script><img" not in html
        page_title = re.search(r"<title>(.*?)</title>", html, re.S)
        assert page_title and "<img" not in page_title.group(1)
