"""Dungeon timer table (WP-C2): Raider.io static-data fetch/parse, the
bundled data/timers.json fallback, +2/+3 keystone-threshold margin math,
and the resulting `timer` report block's CLI wiring and rendering.

Like TestRaiderIO's test_enrich_report (WP-C0/C1), every "live fetch"
test here uses a fake in-process fetcher callable -- never a real network
call -- since neither this project's fictional dungeons nor a real
expansion_id can be checked against Raider.io's actual API shape.
"""

from __future__ import annotations

import json

import pytest
from conftest import build_run_log

from mythic_analyzer.analysis.run_analyzer import analyze_run
from mythic_analyzer.cli import main
from mythic_analyzer.combatlog.parser import iter_events
from mythic_analyzer.combatlog.segmenter import segment_runs
from mythic_analyzer.raiderio import (
    fetch_static_data,
    load_fallback_timers,
    parse_static_timers,
    resolve_timer_map,
    static_data_url,
)
from mythic_analyzer.report.html import render_html
from mythic_analyzer.report.text import render_text


@pytest.fixture()
def run_segment():
    (run,) = list(segment_runs(iter_events(build_run_log().lines)))
    return run


# --- fetch / parse (mocked fetcher only, no real network) ------------------


class TestStaticDataUrl:
    def test_includes_expansion_id(self):
        url = static_data_url(5)
        assert "static-data" in url
        assert "expansion_id=5" in url


class TestFetchStaticData:
    def test_none_expansion_id_skips_fetch_entirely(self):
        calls = []
        result = fetch_static_data(None, fetcher=lambda url: calls.append(url) or {})
        assert result is None
        assert calls == []  # never even called the fetcher

    def test_expansion_id_calls_fetcher_with_the_right_url(self):
        calls = []

        def fake(url):
            calls.append(url)
            return {"dungeons": [{"id": 1, "par_time_ms": 10}]}

        result = fetch_static_data(5, fetcher=fake)
        assert result == {"dungeons": [{"id": 1, "par_time_ms": 10}]}
        assert len(calls) == 1
        assert "expansion_id=5" in calls[0]


class TestParseStaticTimers:
    """The parser's job is to tolerate a shape we can't verify (see module
    docstring / raiderio.py notes) -- these cover the recognized shapes
    it's designed around, and confirm anything else degrades to {}
    instead of raising."""

    def test_seasons_nested_shape_resolves_par_ms(self):
        payload = {
            "seasons": [
                {
                    "slug": "season-1",
                    "dungeons": [
                        {"challenge_mode_id": 587, "name": "Murder Row",
                         "par_time_ms": 1800000},
                        # seconds-style field instead of *_ms -> converted
                        {"challenge_mode_id": 600, "name": "Altar of Fangs",
                         "time_limit": 1980},
                    ],
                },
            ]
        }
        assert parse_static_timers(payload) == {587: 1800000, 600: 1980000}

    def test_flat_top_level_list_shape_also_resolves(self):
        payload = {"mythic_plus_dungeons": [{"id": 612, "par_time_ms": 2100000}]}
        assert parse_static_timers(payload) == {612: 2100000}

    def test_alternate_id_and_time_field_names(self):
        payload = {"dungeons": [{"map_id": 5, "timer_1": 1500}]}
        assert parse_static_timers(payload) == {5: 1500000}

    def test_malformed_top_level_shapes_degrade_to_empty_mapping(self):
        assert parse_static_timers(None) == {}
        assert parse_static_timers(["not", "a", "dict"]) == {}
        assert parse_static_timers("just a string") == {}
        assert parse_static_timers({"unexpected": "shape entirely"}) == {}
        assert parse_static_timers({"seasons": "not a list"}) == {}
        assert parse_static_timers({"dungeons": "not a list either"}) == {}
        assert parse_static_timers({"dungeons": [1, 2, "not dicts"]}) == {}

    def test_entries_missing_usable_fields_are_skipped_not_fatal(self):
        payload = {"dungeons": [
            {"id": 1},                       # no time field at all
            {"par_time_ms": 100},            # no id field at all
            {"id": "not-an-int", "par_time_ms": 500},  # bad id
            {"id": 2, "par_time_ms": "not-a-number"},  # bad time
            {"id": 3, "par_time_ms": 500000},          # the one good entry
        ]}
        assert parse_static_timers(payload) == {3: 500000}


# --- bundled fallback (data/timers.json) ------------------------------------


class TestFallbackTimers:
    def test_bundled_default_file_loads_and_contains_murder_row(self):
        timers = load_fallback_timers()
        assert isinstance(timers, dict) and timers
        assert all(isinstance(k, int) and isinstance(v, int) and v > 0
                    for k, v in timers.items())
        # challenge_map_id 587 = Murder Row, matching tests/conftest.py's
        # synthetic dungeon (and the default `start()` cm= in LogBuilder)
        assert timers[587] > 0

    def test_explicit_path_overrides_bundled_default(self, tmp_path):
        path = tmp_path / "custom_timers.json"
        path.write_text(json.dumps({"timers": {"999": 123456}}), encoding="utf-8")
        assert load_fallback_timers(path) == {999: 123456}

    def test_missing_file_is_tolerated(self, tmp_path):
        assert load_fallback_timers(tmp_path / "nope.json") == {}

    def test_malformed_json_is_tolerated(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        assert load_fallback_timers(path) == {}

    def test_unexpected_shapes_are_tolerated(self, tmp_path):
        path = tmp_path / "weird.json"
        path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        assert load_fallback_timers(path) == {}
        path.write_text(json.dumps({"no_timers_key": True}), encoding="utf-8")
        assert load_fallback_timers(path) == {}
        path.write_text(json.dumps({"timers": {"bad-key": "bad-value"}}),
                        encoding="utf-8")
        assert load_fallback_timers(path) == {}


# --- resolve_timer_map: live-fetch-then-fallback orchestration -------------


class TestResolveTimerMap:
    def test_offline_fetcher_falls_back_to_bundled_file(self):
        """WP-C2 acceptance criterion: a fetcher that always returns None
        (simulating no internet) yields the bundled data/timers.json
        fallback rather than an empty mapping."""
        timers = resolve_timer_map(expansion_id=5, fetcher=lambda url: None)
        assert timers == load_fallback_timers()
        assert 587 in timers

    def test_no_expansion_id_skips_the_fetcher_and_uses_fallback(self):
        calls = []
        timers = resolve_timer_map(
            expansion_id=None, fetcher=lambda url: calls.append(url) or {},
        )
        assert calls == []
        assert timers == load_fallback_timers()

    def test_malformed_live_response_also_falls_back(self):
        timers = resolve_timer_map(
            expansion_id=5, fetcher=lambda url: {"unexpected": "shape"},
        )
        assert timers == load_fallback_timers()

    def test_successful_live_fetch_is_preferred_over_fallback(self):
        live = {"dungeons": [{"id": 587, "par_time_ms": 999000}]}
        timers = resolve_timer_map(expansion_id=5, fetcher=lambda url: live)
        assert timers == {587: 999000}  # not the bundled 587 value

    def test_custom_fallback_path_used_in_place_of_bundled_default(self, tmp_path):
        path = tmp_path / "custom.json"
        path.write_text(json.dumps({"timers": {"42": 100000}}), encoding="utf-8")
        timers = resolve_timer_map(expansion_id=None, fallback_path=path)
        assert timers == {42: 100000}


# --- margin math: analyze_run's `timer` report block -----------------------


class TestTimerReportBlock:
    PAR_MS = 1_000_000  # threshold_2=800_000, threshold_3=600_000

    def _report(self, run_segment, duration_ms):
        run_segment.duration_ms = duration_ms
        run_segment.completed = duration_ms is not None
        return analyze_run(run_segment, par_ms=self.PAR_MS)

    def test_comfortably_timed_plus_one_only(self, run_segment):
        timer = self._report(run_segment, 900_000)["timer"]
        assert timer["par_ms"] == 1_000_000
        assert timer["threshold_2_ms"] == 800_000
        assert timer["threshold_3_ms"] == 600_000
        assert timer["margin_ms"] == 100_000
        assert timer["threshold"] == 1

    def test_plus_two_result(self, run_segment):
        timer = self._report(run_segment, 750_000)["timer"]
        assert timer["margin_ms"] == 250_000
        assert timer["threshold"] == 2

    def test_plus_three_result(self, run_segment):
        timer = self._report(run_segment, 500_000)["timer"]
        assert timer["margin_ms"] == 500_000
        assert timer["threshold"] == 3

    def test_over_timer_result(self, run_segment):
        timer = self._report(run_segment, 1_100_000)["timer"]
        assert timer["margin_ms"] == -100_000
        assert timer["threshold"] == 0

    def test_threshold_boundaries_are_inclusive(self, run_segment):
        # "finishing at or under a threshold earns that many upgrades"
        assert self._report(run_segment, 800_000)["timer"]["threshold"] == 2
        assert self._report(run_segment, 600_000)["timer"]["threshold"] == 3
        assert self._report(run_segment, 1_000_000)["timer"]["threshold"] == 1

    def test_no_par_ms_means_no_timer_block(self, run_segment):
        report = analyze_run(run_segment)
        assert "timer" not in report

    def test_par_ms_without_a_finished_run_omits_verdict_not_thresholds(self, run_segment):
        run_segment.duration_ms = None
        run_segment.completed = False
        timer = analyze_run(run_segment, par_ms=self.PAR_MS)["timer"]
        assert timer["par_ms"] == self.PAR_MS
        assert timer["threshold_2_ms"] == 800_000
        assert timer["threshold_3_ms"] == 600_000
        assert "margin_ms" not in timer
        assert "threshold" not in timer

    def test_json_round_trip(self, run_segment):
        report = self._report(run_segment, 750_000)
        payload = json.loads(json.dumps(report))
        assert payload["timer"]["threshold"] == 2
        assert payload["timer"]["margin_ms"] == 250_000


# --- rendering ---------------------------------------------------------


class TestTimerRendering:
    def test_text_shows_beat_timer_and_threshold(self, run_segment):
        run_segment.duration_ms = 750_000
        run_segment.completed = True
        report = analyze_run(run_segment, par_ms=1_000_000)
        text = render_text(report)
        assert "beat timer by" in text
        assert "(+2)" in text

    def test_text_shows_over_timer(self, run_segment):
        run_segment.duration_ms = 1_100_000
        run_segment.completed = True
        report = analyze_run(run_segment, par_ms=1_000_000)
        text = render_text(report)
        assert "over timer by" in text

    def test_text_omits_timer_line_when_absent(self, run_segment):
        report = analyze_run(run_segment)
        text = render_text(report)
        assert "Timer:" not in text

    def test_html_shows_timer_info_and_threshold(self, run_segment):
        run_segment.duration_ms = 500_000
        run_segment.completed = True
        report = analyze_run(run_segment, par_ms=1_000_000)
        html = render_html(report)
        assert "beat timer by" in html
        assert '"threshold": 3' in html

    def test_html_shows_over_timer(self, run_segment):
        run_segment.duration_ms = 1_100_000
        run_segment.completed = True
        report = analyze_run(run_segment, par_ms=1_000_000)
        html = render_html(report)
        assert "over timer by" in html

    def test_html_omits_timer_block_when_absent(self, run_segment):
        report = analyze_run(run_segment)
        html = render_html(report)
        assert '"timer"' not in html


# --- CLI wiring --------------------------------------------------------


class TestTimerCLI:
    def test_no_timer_flags_means_no_block(self, log_file, capsys):
        assert main(["analyze", str(log_file), "--format", "json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert "timer" not in out

    def test_timer_data_flag_alone_adds_block_with_no_network(self, log_file, tmp_path, capsys):
        timer_file = tmp_path / "timers.json"
        timer_file.write_text(json.dumps({"timers": {"587": 500000}}), encoding="utf-8")
        assert main(["analyze", str(log_file), "--timer-data", str(timer_file),
                     "--format", "json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["timer"]["par_ms"] == 500000

    def test_raiderio_without_expansion_id_uses_bundled_fallback(
        self, log_file, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setenv("MYTHIC_ANALYZER_CACHE", str(tmp_path / "cache-home"))
        # character-profile lookups fail; irrelevant to this test
        monkeypatch.setattr("mythic_analyzer.raiderio._default_fetcher",
                            lambda url: None)
        assert main(["analyze", str(log_file), "--raiderio", "us",
                     "--format", "json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["timer"]["par_ms"] == load_fallback_timers()[587]

    def test_raiderio_with_expansion_id_attempts_live_fetch(
        self, log_file, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setenv("MYTHIC_ANALYZER_CACHE", str(tmp_path / "cache-home"))
        calls = []

        def fake_fetcher(url):
            calls.append(url)
            if "static-data" in url:
                return {"dungeons": [{"id": 587, "par_time_ms": 42000}]}
            return None  # character profile lookups: irrelevant here

        monkeypatch.setattr("mythic_analyzer.raiderio._default_fetcher", fake_fetcher)
        assert main(["analyze", str(log_file), "--raiderio", "us",
                     "--expansion-id", "5", "--format", "json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["timer"]["par_ms"] == 42000
        assert any("static-data" in c and "expansion_id=5" in c for c in calls)

    def test_expansion_id_without_raiderio_is_a_no_op(self, log_file, capsys):
        # --expansion-id only matters alongside --raiderio (an already
        # internet-enabled invocation) -- on its own it doesn't trigger
        # a live fetch, and without --timer-data either, no timer block.
        assert main(["analyze", str(log_file), "--expansion-id", "5",
                     "--format", "json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert "timer" not in out
