"""The report pages must survive whatever an uploaded report says.

Every field in a report came out of a combat log, and on the public site
that upload is anonymous -- so a value the analyzer only ever writes as
0..3 can arrive as -1, and a float can arrive as Infinity. These tests
run the pages' own JS under node (the harness in test_report_escaping.py)
and check that one bad row or value degrades that value, not the page.
"""

from __future__ import annotations

import json
import shutil

import pytest

from postmortem.report.html import render_html
from postmortem.report.index import render_index

from test_report_escaping import _extract_script, _run, real_report  # noqa: F401 (fixture)

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="needs node to run the page's own JS")


def _feed_row(**over) -> dict:
    row = {
        "file": "run.json", "html": "run.html",
        "zone": "Ara-Kara", "level": 12,
        "start_ts": 1_700_000_000, "date": "2023-11-14 12:00",
        "completed": True, "timed": True, "duration_ms": 1_500_000,
        "wall_s": 1500.0, "deaths": 2, "death_cost_s": 10.0,
        "forces_pct": 100.0, "adherence_pct": 90.0,
        "kick_efficiency_pct": 80.0, "affixes": [],
        "party": [{"name": "Tank-Realm", "class": "WARRIOR", "role": "tank"}],
        "threshold": 2, "margin_ms": 60_000, "deaths_detail": [],
    }
    row.update(over)
    return row


def _embedded_json(page: str, data_id: str) -> str:
    start = page.index(f'id="{data_id}"')
    start = page.index(">", start) + 1
    end = page.index("</script>", start)
    return page[start:end]


@needs_node
class TestChestThresholdClamp:
    @pytest.mark.parametrize("threshold", [-1, 7, 2.5, "abc", 1e308])
    def test_feed_renders_with_an_out_of_range_threshold(self, threshold):
        """A threshold of -1 reached "★".repeat(-1), which throws -- and one
        such row blanked the /runs feed for every visitor."""
        rows = [_feed_row(threshold=threshold), _feed_row(zone="Other", start_ts=1_700_000_100)]
        out = _run(_extract_script(render_index(rows)), "runs-data",
                   json.dumps(rows), extra="render();")
        assert "Ara-Kara" in out and "Other" in out
        assert "★★★★" not in out

    def test_run_report_clamps_the_upgrade_label(self, real_report):
        report = json.loads(json.dumps(real_report))
        report["timer"] = {"par_ms": 1_800_000, "threshold_2_ms": 1_440_000,
                           "threshold_3_ms": 1_080_000, "margin_ms": 5000,
                           "threshold": -1}
        out = _run(_extract_script(render_html(report)), "report-data",
                   json.dumps(report), extra="render();")
        assert "beat timer by" in out
        assert "+-1" not in out


def _strict(payload: str):
    def reject(constant):
        raise AssertionError(f"non-standard JSON constant {constant!r} in the page")
    return json.loads(payload, parse_constant=reject)


def _templates() -> dict[str, str]:
    from postmortem.report.html import _TEMPLATE
    from postmortem.report.index import _INDEX_TEMPLATE
    from postmortem.report.snapshot import _TEMPLATE as _SNAPSHOT_TEMPLATE
    return {"report": _TEMPLATE, "index": _INDEX_TEMPLATE, "snapshot": _SNAPSHOT_TEMPLATE}


class TestTemplateEscapes:
    """The templates are ordinary (non-raw) Python strings holding CSS and
    JS, so a single backslash is a Python escape: CSS's " \\2605" became
    the octal escape \\260 -- a degree sign -- followed by "5"."""

    def test_stealable_star_survives_as_a_css_escape(self):
        page = render_html({"run": {"zone": "Z"}})
        assert 'content: " \\2605"' in page
        assert "°5" not in page

    @pytest.mark.parametrize("name", ["report", "index", "snapshot"])
    def test_no_control_characters_in_the_template(self, name):
        text = _templates()[name]
        bad = sorted({hex(ord(c)) for c in text if ord(c) < 0x20 and c not in "\n\t"})
        assert not bad, f"{name} template has escape-damaged control chars: {bad}"

    @pytest.mark.parametrize("name", ["report", "index", "snapshot"])
    def test_css_content_escapes_are_intact(self, name):
        """A mangled \\NNN escape lands as a Latin-1 character (\\260 -> the
        degree sign), which no generated-content string here means to use."""
        import re
        for value in re.findall(r'content:\s*"([^"]*)"', _templates()[name]):
            assert not any(0x80 <= ord(c) <= 0xFF for c in value), \
                f"{name}: CSS content {value!r} looks like a swallowed escape"


class TestNonFiniteNumbers:
    """Python's json.dumps writes Infinity/NaN, which JSON.parse rejects,
    so one non-finite float anywhere in a report blanked the whole page."""

    def test_feed_payload_is_strict_json(self):
        rows = [_feed_row(forces_pct=float("inf"), wall_s=float("nan"),
                          party=[{"name": "x", "dps": float("-inf")}])]
        parsed = _strict(_embedded_json(render_index(rows), "runs-data"))
        assert parsed[0]["forces_pct"] is None
        assert parsed[0]["wall_s"] is None
        assert parsed[0]["party"][0]["dps"] is None

    def test_report_payload_is_strict_json_and_keeps_the_script_guard(self):
        report = {"run": {"zone": "</script>", "wall_duration_s": float("inf")},
                  "pulls": [(1, float("nan"))]}
        payload = _embedded_json(render_html(report), "report-data")
        parsed = _strict(payload.replace("<\\/", "</"))
        assert parsed["run"]["wall_duration_s"] is None
        assert parsed["pulls"] == [[1, None]]
        assert "</script>" not in payload

    @needs_node
    def test_feed_renders_in_node_with_a_non_finite_value(self):
        rows = [_feed_row(forces_pct=float("inf"))]
        page = render_index(rows)
        out = _run(_extract_script(page), "runs-data",
                   _embedded_json(page, "runs-data"), extra="render();")
        assert "Ara-Kara" in out
