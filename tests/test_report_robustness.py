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
