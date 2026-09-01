"""History index, Raider.io enrichment, recorder hooks."""

import json
import re
import shutil
import subprocess

import pytest

from conftest import DPS1, LogBuilder, build_run_log

from postmortem.cli import main
from postmortem.raiderio import (
    enrich_report,
    match_run,
    parse_recent_runs,
    realm_slug,
)
from postmortem.recorder import Recorder
from postmortem.report.index import build_index, collect_reports, render_index

NODE = shutil.which("node")


def _row(**over):
    """A synthetic collect_reports()-shaped row, for chart tests that don't
    need a real report file on disk."""
    base = {
        "file": "run.json", "html": "run.html", "zone": "Operation: Mechagon",
        "level": 10, "start_ts": 1_700_000_000, "date": "2023-11-14 12:00",
        "completed": True, "timed": True, "duration_ms": 1_500_000,
        "wall_s": 1500.0, "deaths": 0, "death_cost_s": 0.0,
        "forces_pct": 100.0, "adherence_pct": 80.0,
        "kick_efficiency_pct": 60.0, "affixes": [],
    }
    base.update(over)
    return base


def _extract_inline_script(html):
    """Pull the vanilla-JS <script> block (the one that does the actual
    rendering) out of a rendered index page, for the Node-runtime tests."""
    m = re.search(r"<script>\n(.*)\n</script>\n</body>", html, re.S)
    assert m, "inline render <script> block not found in rendered index"
    return m.group(1)


def _run_chart_js(tmp_path, rows, filter_zone=None):
    """Execute the real render() against a stubbed document/RUNS, optionally
    re-rendering once more with the dungeon filter set, and return the final
    #app innerHTML. Requires node; callers should be skipped without it."""
    html = render_index(rows)
    script = _extract_inline_script(html)
    runs_literal = json.dumps(json.dumps(rows))
    filter_stmt = f"dungeon = {json.dumps(filter_zone)};\n" if filter_zone is not None else ""
    harness = f"""
class El {{
  constructor() {{ this.innerHTML = ""; this.textContent = ""; }}
}}
const __els = {{ "runs-data": new El(), "app": new El() }};
__els["runs-data"].textContent = {runs_literal};
global.document = {{ getElementById: id => __els[id] }};

{script}

{filter_stmt}render();
console.log(document.getElementById("app").innerHTML);
"""
    js_file = tmp_path / "harness.js"
    js_file.write_text(harness, encoding="utf-8")
    result = subprocess.run([NODE, str(js_file)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestHistoryIndex:
    def _make_reports(self, tmp_path, log_file, route_string, dungeon_data_file):
        out_dir = tmp_path / "reports"
        assert main([
            "analyze", str(log_file),
            "--route", route_string,
            "--dungeon-data", str(dungeon_data_file),
            "--format", "json,html",
            "--out", str(out_dir),
        ]) == 0
        return out_dir

    def test_collect_and_build(self, tmp_path, log_file, route_string,
                               dungeon_data_file, capsys):
        out_dir = self._make_reports(tmp_path, log_file, route_string,
                                     dungeon_data_file)
        rows = collect_reports(out_dir)
        assert len(rows) == 1
        row = rows[0]
        assert row["zone"] == "Murder Row"
        assert row["level"] == 10
        assert row["timed"] is True
        assert row["deaths"] == 1
        assert row["adherence_pct"] == 66.7
        assert row["kick_efficiency_pct"] == 42.9
        assert row["html"] is not None and row["html"].endswith(".html")

        out = build_index(out_dir)
        assert out.name == "index.html"
        html = out.read_text()
        assert "Mythic+ run history" in html
        assert "Murder Row" in html

    def test_cli_index(self, tmp_path, log_file, route_string,
                       dungeon_data_file, capsys):
        out_dir = self._make_reports(tmp_path, log_file, route_string,
                                     dungeon_data_file)
        capsys.readouterr()
        assert main(["index", str(out_dir)]) == 0
        assert "indexed 1 runs" in capsys.readouterr().out
        assert (out_dir / "index.html").exists()

    def test_ignores_foreign_json(self, tmp_path):
        (tmp_path / "other.json").write_text('{"hello": "world"}')
        assert collect_reports(tmp_path) == []

    def test_ignores_a_top_level_list_json_without_crashing(self, tmp_path):
        # Regression (2026-09-01 debug sweep): `record --analyze` writes a
        # <run>.chapters.json sidecar -- a top-level JSON *list* -- into
        # the same directory as the reports. collect_reports (and cli.py's
        # _scan_report_files) called report.get("run") without a type
        # check, so a list raised AttributeError (not caught by the
        # OSError/ValueError guard), crashing `index` and 500ing every
        # `serve` request on any real record-produced directory.
        (tmp_path / "run.chapters.json").write_text('[{"title": "Pull 1", "t": 0}]')
        (tmp_path / "scalar.json").write_text('42')
        assert collect_reports(tmp_path) == []

    def test_index_survives_a_chapters_sidecar_next_to_a_real_report(
        self, tmp_path, log_file, route_string, dungeon_data_file,
    ):
        out_dir = self._make_reports(tmp_path, log_file, route_string,
                                     dungeon_data_file)
        # mimic the recorder's sidecar landing beside the real report
        (out_dir / "somerun.chapters.json").write_text('[{"title": "Pull 1", "t": 0}]')
        rows = collect_reports(out_dir)
        assert len(rows) == 1  # the real report, sidecar cleanly skipped
        assert rows[0]["zone"] == "Murder Row"


class TestHistoryCharts:
    """render_index()'s trend-chart markup, at the template/string level
    (no JS runtime needed -- see TestHistoryChartsRuntime below for that)."""

    def test_charts_render_for_multiple_rows(self):
        rows = [
            _row(start_ts=1_700_000_000 + i * 3600, timed=(i % 2 == 0),
                 deaths=i, adherence_pct=50 + i, kick_efficiency_pct=40 + i,
                 file=f"r{i}.json")
            for i in range(6)
        ]
        html = render_index(rows)
        assert "chartsSection" in html
        assert "sparklineChart" in html
        assert "<h2>Trends</h2>" in html
        assert 'class="charts"' in html
        assert "chart-svg" in html

    def test_charts_handle_missing_adherence_and_kicks(self):
        # Runs analyzed without --route / interrupt tracking have null
        # adherence_pct/kick_efficiency_pct -- the most likely real-world
        # failure mode. This must not raise, and the nulls must survive
        # into the embedded JSON (not get coerced to 0) so the chart code
        # can treat them as gaps.
        rows = [
            _row(start_ts=1_700_000_000, adherence_pct=None, kick_efficiency_pct=None),
            _row(start_ts=1_700_003_600, adherence_pct=70.0, kick_efficiency_pct=None),
            _row(start_ts=1_700_007_200, adherence_pct=None, kick_efficiency_pct=55.5),
        ]
        html = render_index(rows)
        assert "sparklineChart" in html
        assert '"adherence_pct": null' in html
        assert '"kick_efficiency_pct": null' in html

    def test_charts_render_for_small_row_counts(self):
        for n in (0, 1, 2, 3):
            rows = [_row(start_ts=1_700_000_000 + i, file=f"r{i}.json") for i in range(n)]
            html = render_index(rows)  # must not raise for a sparse/new history
            assert "<html" in html.lower()
            assert "sparklineChart" in html

    def test_charts_read_the_dungeon_filtered_row_set(self):
        html = render_index([_row()])
        # Charts are built from the same `filtered` set the table sorts
        # from, not straight off the unfiltered RUNS global, so they react
        # to the dungeon <select>'s onchange -> render() cycle.
        assert "const filtered = RUNS.filter(r => !dungeon || r.zone === dungeon);" in html
        assert "const chartRows = filtered.slice().sort((a, b) => (a.start_ts ?? 0) - (b.start_ts ?? 0));" in html
        assert "chartsSection(chartRows)" in html

    def test_chart_ordering_independent_of_table_sort_state(self):
        html = render_index([_row()])
        m = re.search(r"function chartsSection\(sorted\) \{.*?\n\}\n\nfunction render", html, re.S)
        assert m, "chartsSection() not found"
        body = m.group(0)
        # chart x-axis ordering must not depend on the clickable table's
        # sortKey/sortDir state.
        assert "sortKey" not in body
        assert "sortDir" not in body


@pytest.mark.skipif(NODE is None, reason="node not available for JS-runtime chart tests")
class TestHistoryChartsRuntime:
    """Executes the actual render() function (extracted from the rendered
    page) against a stubbed document/RUNS, the way a browser would, for
    stronger confidence than the string-level checks above give alone."""

    def test_svg_present_and_filter_changes_output(self, tmp_path):
        rows = (
            [_row(zone="A", start_ts=1_700_000_000 + i * 100, deaths=i,
                  adherence_pct=50 + i, kick_efficiency_pct=30 + i, file=f"a{i}.json")
             for i in range(4)]
            + [_row(zone="B", start_ts=1_700_001_000 + i * 100, deaths=i,
                    adherence_pct=None, kick_efficiency_pct=None, file=f"b{i}.json")
               for i in range(3)]
        )
        unfiltered = _run_chart_js(tmp_path, rows)
        assert "<svg" in unfiltered
        assert unfiltered.count("<svg") == 4  # one per series
        # combined dataset has adherence/kick data (from zone A) -> no gap-out
        assert "not enough data" not in unfiltered

        filtered_b = _run_chart_js(tmp_path, rows, filter_zone="B")
        assert "<svg" in filtered_b
        # zone B alone has zero adherence/kick data -> those two charts fall
        # back to "not enough data" instead of crashing or plotting zeros
        assert filtered_b.count("not enough data") == 2
        assert filtered_b != unfiltered

    def test_missing_values_do_not_crash_runtime(self, tmp_path):
        rows = [
            _row(start_ts=1_700_000_000, adherence_pct=None, kick_efficiency_pct=None),
            _row(start_ts=1_700_003_600, adherence_pct=70.0, kick_efficiency_pct=None),
            _row(start_ts=1_700_007_200, adherence_pct=None, kick_efficiency_pct=55.5),
        ]
        out = _run_chart_js(tmp_path, rows)
        assert out.count("<svg") == 4

    def test_small_row_counts_runtime(self, tmp_path):
        for n in (1, 2, 3):
            rows = [_row(start_ts=1_700_000_000 + i, file=f"r{i}.json") for i in range(n)]
            out = _run_chart_js(tmp_path, rows)
            assert out.count("<svg") == 4


class TestRaiderIO:
    def test_realm_slug(self):
        assert realm_slug("TarrenMill") == "tarren-mill"
        assert realm_slug("Area52") == "area-52"
        assert realm_slug("Kil'jaeden") == "kiljaeden"
        assert realm_slug("Draenor") == "draenor"

    def test_enrich_report(self):
        report = {"players": [
            {"name": "Zappyboi-Area52", "guid": "Player-1"},
            {"name": "Thicktank-TarrenMill", "guid": "Player-2"},
            {"name": "Pets & Guardians", "guid": "_pets"},
        ]}
        calls = []

        def fake_fetcher(url):
            calls.append(url)
            if "zappyboi" in url.lower():
                return {
                    "name": "Zappyboi",
                    "profile_url": "https://raider.io/characters/us/area-52/Zappyboi",
                    "class": "Mage",
                    "active_spec_name": "Fire",
                    "mythic_plus_scores_by_season": [{"scores": {"all": 3021.4}}],
                    "mythic_plus_best_runs": [
                        {"dungeon": "Murder Row", "mythic_level": 17,
                         "clear_time_ms": 1500000, "url": "https://raider.io/x"},
                    ],
                }
            return None  # second player: lookup fails

        n = enrich_report(report, "us", fetcher=fake_fetcher)
        assert n == 1
        assert len(calls) == 2  # the pets bucket has no realm, no call
        assert "region=us" in calls[0] and "realm=area-52" in calls[0]
        rio = report["players"][0]["raiderio"]
        assert rio["score"] == 3021.4
        assert rio["season_best"]["level"] == 17
        assert report["players"][1]["raiderio"] == {"error": "lookup failed"}
        assert report["raiderio"]["enriched_players"] == 1


class TestRunMatching:
    """WP-C3: matching a locally-analyzed run against a player's
    Raider.io "recent runs" (sourced from the mythic_plus_recent_runs
    field on the same character-profile fetch enrich_report already
    makes -- see raiderio.py's module notes on why)."""

    def _candidate(self, **over):
        base = {
            "challenge_map_id": 587,
            "keystone_level": 10,
            "completed_ts": 1_700_000_000.0,
            "score": 150.0,
            "url": "https://raider.io/runs/1",
            "dungeon": "Murder Row",
        }
        base.update(over)
        return base

    # -- match_run ------------------------------------------------------

    def test_match_run_exact_match(self):
        cand = self._candidate()
        result = match_run(
            [cand],
            challenge_map_id=587,
            keystone_level=10,
            completion_ts=1_700_000_030.0,  # 30s off, well inside window
        )
        assert result is cand
        assert result["score"] == 150.0
        assert result["url"] == "https://raider.io/runs/1"

    def test_match_run_no_match_wrong_map_id(self):
        cand = self._candidate(challenge_map_id=600)
        assert match_run(
            [cand], challenge_map_id=587, keystone_level=10,
            completion_ts=1_700_000_000.0,
        ) is None

    def test_match_run_no_match_wrong_level(self):
        cand = self._candidate(keystone_level=11)
        assert match_run(
            [cand], challenge_map_id=587, keystone_level=10,
            completion_ts=1_700_000_000.0,
        ) is None

    def test_match_run_no_match_just_outside_window(self):
        cand = self._candidate(completed_ts=1_700_000_000.0)
        # 601s off (> 600s default window): no match
        assert match_run(
            [cand], challenge_map_id=587, keystone_level=10,
            completion_ts=1_700_000_000.0 + 601,
        ) is None

    def test_match_run_at_window_boundary_matches(self):
        cand = self._candidate(completed_ts=1_700_000_000.0)
        # exactly 600s off (== default window): still matches
        result = match_run(
            [cand], challenge_map_id=587, keystone_level=10,
            completion_ts=1_700_000_000.0 + 600,
        )
        assert result is cand

    def test_match_run_picks_closest_of_multiple_candidates(self):
        far = self._candidate(completed_ts=1_700_000_000.0 - 400, url="far")
        near = self._candidate(completed_ts=1_700_000_000.0 - 50, url="near")
        other_run = self._candidate(completed_ts=1_700_000_000.0 + 590, url="other")
        result = match_run(
            [far, other_run, near],
            challenge_map_id=587, keystone_level=10,
            completion_ts=1_700_000_000.0,
        )
        assert result["url"] == "near"

    def test_match_run_no_local_completion_time_returns_none(self):
        # An incomplete/abandoned local run has no completion time to
        # match against at all -- skip cleanly, no crash.
        cand = self._candidate()
        assert match_run(
            [cand], challenge_map_id=587, keystone_level=10,
            completion_ts=None,
        ) is None

    def test_match_run_empty_candidates(self):
        assert match_run(
            [], challenge_map_id=587, keystone_level=10,
            completion_ts=1_700_000_000.0,
        ) is None

    # -- parse_recent_runs ------------------------------------------------

    def test_parse_recent_runs_extracts_plausible_fields(self):
        profile = {
            "mythic_plus_recent_runs": [
                {
                    "dungeon": "Murder Row",
                    "mythic_level": 12,
                    "challenge_mode_id": 587,
                    "completed_at": "2023-11-14T22:13:20Z",
                    "score": 175.5,
                    "url": "https://raider.io/runs/2",
                },
            ]
        }
        out = parse_recent_runs(profile)
        assert len(out) == 1
        entry = out[0]
        assert entry["challenge_map_id"] == 587
        assert entry["keystone_level"] == 12
        assert entry["score"] == 175.5
        assert entry["url"] == "https://raider.io/runs/2"
        assert entry["dungeon"] == "Murder Row"
        # 2023-11-14T22:13:20Z as epoch seconds
        assert entry["completed_ts"] == pytest.approx(1_700_000_000.0, abs=1)

    def test_parse_recent_runs_numeric_and_ms_timestamps(self):
        profile = {
            "mythic_plus_recent_runs": [
                {"map_id": 600, "level": 8, "completed_at": 1_700_000_000},
                {"map_id": 612, "level": 9, "clear_time": 1_700_000_000_000},  # ms
            ]
        }
        out = parse_recent_runs(profile)
        assert len(out) == 2
        assert out[0]["completed_ts"] == 1_700_000_000.0
        assert out[1]["completed_ts"] == 1_700_000_000.0

    def test_parse_recent_runs_malformed_payload_yields_nothing(self):
        assert parse_recent_runs(None) == []
        assert parse_recent_runs({}) == []
        assert parse_recent_runs({"mythic_plus_recent_runs": "not a list"}) == []
        assert parse_recent_runs({"mythic_plus_recent_runs": [1, 2, "x"]}) == []
        # missing required fields (no map id / level / timestamp)
        assert parse_recent_runs({
            "mythic_plus_recent_runs": [{"score": 100}]
        }) == []
        # unparseable timestamp
        assert parse_recent_runs({
            "mythic_plus_recent_runs": [
                {"map_id": 587, "level": 10, "completed_at": "not-a-date"}
            ]
        }) == []
        # wrong types don't raise
        assert parse_recent_runs({
            "mythic_plus_recent_runs": [
                {"map_id": "nope", "level": 10, "completed_at": 1_700_000_000}
            ]
        }) == []

    # -- enrich_report integration ----------------------------------------

    def _profile(self, recent_runs):
        return {
            "name": "Zappyboi",
            "profile_url": "https://raider.io/characters/us/area-52/Zappyboi",
            "class": "Mage",
            "active_spec_name": "Fire",
            "mythic_plus_scores_by_season": [{"scores": {"all": 3021.4}}],
            "mythic_plus_best_runs": [],
            "mythic_plus_recent_runs": recent_runs,
        }

    def test_enrich_report_attaches_raiderio_run_on_match(self):
        report = {
            "run": {"challenge_map_id": 587, "keystone_level": 10,
                     "end_ts": 1_700_000_000.0},
            "players": [{"name": "Zappyboi-Area52", "guid": "Player-1"}],
        }
        recent = [{
            "challenge_mode_id": 587, "mythic_level": 10,
            "completed_at": 1_700_000_030.0,
            "score": 210.0, "url": "https://raider.io/runs/match",
        }]
        fetcher = lambda url: self._profile(recent)

        enrich_report(report, "us", fetcher=fetcher)
        run_match = report["players"][0].get("raiderio_run")
        assert run_match == {
            "score": 210.0,
            "url": "https://raider.io/runs/match",
            "level": 10,
            "dungeon": None,
        }
        # unrelated fields from _summarize are unaffected
        assert report["players"][0]["raiderio"]["score"] == 3021.4

    def test_enrich_report_no_match_no_attachment(self):
        report = {
            "run": {"challenge_map_id": 587, "keystone_level": 10,
                     "end_ts": 1_700_000_000.0},
            "players": [{"name": "Zappyboi-Area52", "guid": "Player-1"}],
        }
        recent = [
            {"challenge_mode_id": 600, "mythic_level": 10,  # wrong map
             "completed_at": 1_700_000_000.0, "score": 1, "url": "a"},
            {"challenge_mode_id": 587, "mythic_level": 10,  # right key, too far off
             "completed_at": 1_700_000_000.0 + 900, "score": 2, "url": "b"},
        ]
        fetcher = lambda url: self._profile(recent)

        enrich_report(report, "us", fetcher=fetcher)
        assert "raiderio_run" not in report["players"][0]
        assert "raiderio" in report["players"][0]  # enrichment itself still worked

    def test_enrich_report_incomplete_local_run_skips_matching(self):
        # No end_ts (abandoned/incomplete run): matching is skipped
        # entirely, even though character enrichment succeeds.
        report = {
            "run": {"challenge_map_id": 587, "keystone_level": 10,
                     "end_ts": None},
            "players": [{"name": "Zappyboi-Area52", "guid": "Player-1"}],
        }
        recent = [{"challenge_mode_id": 587, "mythic_level": 10,
                   "completed_at": 1_700_000_000.0, "score": 1, "url": "a"}]
        fetcher = lambda url: self._profile(recent)

        n = enrich_report(report, "us", fetcher=fetcher)
        assert n == 1  # profile lookup itself still succeeded
        assert "raiderio_run" not in report["players"][0]

    def test_enrich_report_no_run_key_skips_matching(self):
        # No "run" block on the report at all (e.g. a caller that never
        # threaded one through): matching is skipped, no crash.
        report = {"players": [{"name": "Zappyboi-Area52", "guid": "Player-1"}]}
        fetcher = lambda url: self._profile(
            [{"challenge_mode_id": 587, "mythic_level": 10,
              "completed_at": 1_700_000_000.0, "score": 1, "url": "a"}]
        )
        enrich_report(report, "us", fetcher=fetcher)
        assert "raiderio_run" not in report["players"][0]

    def test_enrich_report_malformed_recent_runs_payload_no_crash(self):
        report = {
            "run": {"challenge_map_id": 587, "keystone_level": 10,
                     "end_ts": 1_700_000_000.0},
            "players": [{"name": "Zappyboi-Area52", "guid": "Player-1"}],
        }

        def fetcher(url):
            profile = self._profile("not a list")  # wrong type entirely
            return profile

        n = enrich_report(report, "us", fetcher=fetcher)
        assert n == 1
        assert "raiderio_run" not in report["players"][0]

    def test_enrich_report_lookup_failure_skips_matching(self):
        # Total lookup failure (profile None) is still marked via the
        # existing {"error": "lookup failed"} path; run-matching simply
        # never gets attempted since there's no profile to pull
        # candidates from.
        report = {
            "run": {"challenge_map_id": 587, "keystone_level": 10,
                     "end_ts": 1_700_000_000.0},
            "players": [{"name": "Zappyboi-Area52", "guid": "Player-1"}],
        }
        n = enrich_report(report, "us", fetcher=lambda url: None)
        assert n == 0
        assert report["players"][0]["raiderio"] == {"error": "lookup failed"}
        assert "raiderio_run" not in report["players"][0]


class TestRaiderIOCliCache:
    """WP-C1: the CLI wraps the real fetcher in an on-disk cache by
    default, and --raiderio-no-cache bypasses it. These monkeypatch
    postmortem.raiderio._default_fetcher (cmd_analyze imports it
    fresh from the module each call) so no real network traffic happens,
    and point MYTHIC_ANALYZER_CACHE at a tmp_path so the real
    ~/.cache/postmortem is never touched."""

    def _counting_fetcher(self, calls):
        def fake(url):
            calls.append(url)
            return {"name": "Cached", "class": "Mage", "active_spec_name": "Fire"}
        return fake

    def test_default_second_analyze_hits_the_cache(self, log_file, tmp_path,
                                                     monkeypatch):
        monkeypatch.setenv("MYTHIC_ANALYZER_CACHE", str(tmp_path / "cache-home"))
        calls = []
        monkeypatch.setattr("postmortem.raiderio._default_fetcher",
                             self._counting_fetcher(calls))

        assert main(["analyze", str(log_file), "--raiderio", "us",
                     "--out", str(tmp_path / "out1")]) == 0
        first = len(calls)
        assert first > 0

        assert main(["analyze", str(log_file), "--raiderio", "us",
                     "--out", str(tmp_path / "out2")]) == 0
        assert len(calls) == first  # second run served entirely from cache

    def test_no_cache_flag_refetches_every_time(self, log_file, tmp_path,
                                                  monkeypatch):
        monkeypatch.setenv("MYTHIC_ANALYZER_CACHE", str(tmp_path / "cache-home"))
        calls = []
        monkeypatch.setattr("postmortem.raiderio._default_fetcher",
                             self._counting_fetcher(calls))

        assert main(["analyze", str(log_file), "--raiderio", "us",
                     "--raiderio-no-cache", "--out", str(tmp_path / "out1")]) == 0
        first = len(calls)
        assert first > 0

        assert main(["analyze", str(log_file), "--raiderio", "us",
                     "--raiderio-no-cache", "--out", str(tmp_path / "out2")]) == 0
        assert len(calls) == first * 2  # bypassed the cache both times


class TestRecorderWaitsForLogFile:
    """Real UX gap (2026-08-31): WoW only creates the combat log once
    logging is actually enabled -- with this project's own addon, that's
    automatically at the start of the session's first key (see
    CombatLogging.lua). Starting Watch Live *before* that (a completely
    normal thing to do -- open the app, click Start, then go play) used
    to crash immediately with FileNotFoundError instead of just waiting."""

    def test_watch_waits_for_a_not_yet_existing_file_then_picks_it_up(self, tmp_path):
        import threading
        import time

        log = tmp_path / "WoWCombatLog.txt"
        assert not log.exists()
        waited = threading.Event()
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", from_start=True,
            poll_interval=0.05, echo=lambda s: None, on_waiting_for_log=waited.set,
        )

        def create_log_after_a_moment():
            waited.wait(timeout=5.0)
            time.sleep(0.05)
            log.write_text(build_run_log().text(), encoding="utf-8")

        threading.Thread(target=create_log_after_a_moment, daemon=True).start()
        runs = rec.watch(stop_after_runs=1)

        assert waited.is_set()
        assert len(runs) == 1
        assert runs[0].zone == "Murder Row"

    def test_stopping_while_waiting_for_the_file_returns_cleanly(self, tmp_path):
        import threading
        import time

        log = tmp_path / "WoWCombatLog.txt"
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs",
            poll_interval=0.05, echo=lambda s: None,
        )

        def stop_after_a_moment():
            time.sleep(0.1)
            rec.request_stop()

        threading.Thread(target=stop_after_a_moment, daemon=True).start()
        runs = rec.watch()  # must return, not hang/raise, and never see the file
        assert runs == []
        assert not log.exists()


class TestRecorderResumesInProgressRun:
    """Real report (2026-09-01): a key that finished completely normally
    was never picked up by Watch Live -- no error, nothing in the
    activity list, silence. Root cause: the key had already started
    (CHALLENGE_MODE_START already written) before Watch Live was turned
    on, so watch()'s usual "skip to end-of-file, only tail what's new"
    behavior for an already-existing file skipped straight past that
    START. When the run's own CHALLENGE_MODE_END arrived later, _feed()
    had no open run to close (self._current was still None), so the END
    matched nothing and was silently dropped."""

    def test_a_key_already_in_progress_when_watch_starts_is_still_caught(self, tmp_path):
        b = LogBuilder()
        b.start(0)  # the key already began before watch() opens the file
        b.player_damage(5, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(b.text(), encoding="utf-8")

        rec = Recorder(log_path=log, out_dir=tmp_path / "runs", echo=lambda s: None)

        import threading
        import time

        def finish_the_key_after_a_moment():
            time.sleep(0.2)
            end_only = LogBuilder()
            end_only.end(60)
            with open(log, "a", encoding="utf-8") as f:
                f.write(end_only.text())

        threading.Thread(target=finish_the_key_after_a_moment, daemon=True).start()
        runs = rec.watch(stop_after_runs=1)

        assert len(runs) == 1
        assert runs[0].completed

    def test_a_run_already_finished_before_watch_starts_is_not_replayed(self, tmp_path):
        # The normal case this fix must not disturb: watch() starting
        # against a log whose last run already has its own END should
        # still only tail what's new from here, not re-report history.
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(build_run_log().text(), encoding="utf-8")

        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs",
            poll_interval=0.05, echo=lambda s: None,
        )

        import threading
        import time

        def stop_after_a_moment():
            time.sleep(0.3)
            rec.request_stop()

        threading.Thread(target=stop_after_a_moment, daemon=True).start()
        runs = rec.watch()
        assert runs == []


class TestRecorderHooks:
    def test_hooks_fire_with_env(self, tmp_path):
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(build_run_log().text(), encoding="utf-8")
        start_marker = tmp_path / "started.txt"
        end_marker = tmp_path / "ended.txt"
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", from_start=True,
            echo=lambda s: None,
            on_start_cmd=f'echo "$MA_ZONE +$MA_LEVEL" > "{start_marker}"',
            on_end_cmd=f'echo "$MA_ZONE" > "{end_marker}"',
        )
        runs = rec.watch(stop_after_runs=1)
        assert len(runs) == 1
        # hooks are fire-and-forget; give the shells a moment
        import time
        for _ in range(50):
            if start_marker.exists() and end_marker.exists():
                break
            time.sleep(0.05)
        assert start_marker.read_text().strip() == "Murder Row +10"
        assert end_marker.read_text().strip() == "Murder Row"
