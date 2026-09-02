"""Tests for the desktop API bridge (postmortem.desktop.api.DesktopAPI).

Covers every method except the three native-dialog pickers
(pick_log_file/pick_route_file/pick_folder/pick_dungeon_data_file/
pick_avoidable_data_file), which need a live pywebview window and can't
be meaningfully unit tested (see api.py's module docstring).
"""

from __future__ import annotations

import json
import textwrap
import threading
from pathlib import Path

import pytest

from postmortem.desktop import api as api_module
from postmortem.desktop import config as config_module
from postmortem.desktop import updater as updater_module
from postmortem.desktop.api import DesktopAPI
from postmortem.history.store import ingest as history_ingest


@pytest.fixture()
def api() -> DesktopAPI:
    return DesktopAPI()


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    """File-wide, not per-class: a growing number of DesktopAPI methods
    resolve local app-data paths through desktop.config.config_dir() --
    settings, watch-mode output, and (since analyze() started
    auto-saving reports locally -- see api.py's _save_report_locally)
    every analyze() call too. A real incident (2026-09-01): only
    TestUploadReport/TestWatchMode/TestSettings had their own copy of
    this fixture, so adding analyze()'s local auto-save made
    TestAnalyzeSuccess's tests silently write real report files and a
    real history.db into the developer's actual
    ~/Library/Application Support/postmortem the moment this suite ran
    -- caught and cleaned up by hand, not by any test. One file-wide
    autouse fixture instead of a fixture every test class has to
    remember to add for itself makes that structurally impossible going
    forward, for this method or any future one.
    """
    fake_dir = tmp_path / "postmortem-config"
    monkeypatch.setattr(config_module, "config_dir", lambda: fake_dir)
    return fake_dir


# -- list_runs ----------------------------------------------------------


class TestListRuns:
    def test_single_run_log(self, api, log_file):
        result = api.list_runs(str(log_file))
        assert result["ok"] is True
        assert len(result["runs"]) == 1
        run = result["runs"][0]
        assert run["index"] == 1
        assert run["zone"] == "Murder Row"
        assert run["keystone_level"] == 10
        assert run["timed"] is True
        assert run["completed"] is True

    def test_three_run_log_counts_and_fields(self, api, three_run_log_file):
        result = api.list_runs(str(three_run_log_file))
        assert result["ok"] is True
        runs = result["runs"]
        assert len(runs) == 3
        assert [r["index"] for r in runs] == [1, 2, 3]
        assert [r["zone"] for r in runs] == ["Cave One", "Cave Two", "Cave Three"]
        assert [r["keystone_level"] for r in runs] == [5, 10, 15]
        assert runs[0]["completed"] is False  # abandoned, no CHALLENGE_MODE_END
        assert runs[1]["completed"] is True and runs[1]["timed"] is True
        assert runs[2]["completed"] is True and runs[2]["timed"] is False

    def test_missing_log_returns_error_not_exception(self, api, tmp_path):
        result = api.list_runs(str(tmp_path / "does-not-exist.txt"))
        assert result["ok"] is False
        assert "error" in result

    def test_does_not_retain_events_after_summarizing(
        self, api, three_run_log_file, monkeypatch,
    ):
        """Streaming/memory-conscious contract (WP-A0 pattern): list_runs
        must drop each RunSegment's events immediately after summarizing
        it, never holding more than one run's full event list at a time.
        Taps the real segment_runs() (same technique as
        TestPickRunStreaming in test_cli_and_tools.py) to inspect every
        segment after the fact.
        """
        import postmortem.desktop.api as api_module
        from postmortem.combatlog.segmenter import segment_runs as real_segment_runs

        captured = []

        def tapped(events):
            for seg in real_segment_runs(events):
                captured.append(seg)
                yield seg

        monkeypatch.setattr(api_module, "segment_runs", tapped)

        result = api.list_runs(str(three_run_log_file))
        assert result["ok"] is True
        assert len(captured) == 3
        # every segment's events were cleared right after being summarized
        assert all(seg.events == [] for seg in captured)


# -- analyze --------------------------------------------------------------


class TestAnalyzeSuccess:
    def test_full_pipeline_round_trips_and_renders_html(
        self, api, log_file, route_string, dungeon_data_file,
    ):
        result = api.analyze({
            "log_path": str(log_file),
            "route": route_string,
            "dungeon_data_path": str(dungeon_data_file),
        })
        assert result["ok"] is True
        report = result["report"]

        # must survive a JSON round trip with no loss
        round_tripped = json.loads(json.dumps(report))
        assert round_tripped == report

        assert report["run"]["zone"] == "Murder Row"
        assert report["run"]["keystone_level"] == 10
        assert "comparison" in report  # route + dungeon data were both wired in

        html = result["html"]
        assert "<html" in html
        assert "Murder Row" in html

    def test_without_route_or_dungeon_data_still_succeeds(self, api, log_file):
        result = api.analyze({"log_path": str(log_file)})
        assert result["ok"] is True
        assert result["report"]["run"]["zone"] == "Murder Row"
        assert "route" not in result["report"]

    def test_route_from_file_path(self, api, log_file, route_string, tmp_path):
        route_file = tmp_path / "route.txt"
        route_file.write_text(route_string, encoding="utf-8")
        result = api.analyze({"log_path": str(log_file), "route": str(route_file)})
        assert result["ok"] is True
        assert result["report"]["route"]["name"] == "Test MR Route"

    def test_numeric_run_selector_picks_correct_run(self, api, three_run_log_file):
        result = api.analyze({
            "log_path": str(three_run_log_file), "run_selector": "2",
        })
        assert result["ok"] is True
        assert result["report"]["run"]["zone"] == "Cave Two"

    def test_default_run_selector_is_last_and_skips_abandoned(
        self, api, three_run_log_file,
    ):
        result = api.analyze({"log_path": str(three_run_log_file)})
        assert result["ok"] is True
        assert result["report"]["run"]["zone"] == "Cave Three"

    def test_custom_pull_gap_and_death_penalty_are_honored(self, api, log_file):
        result = api.analyze({
            "log_path": str(log_file),
            "pull_gap_seconds": 999,  # absurdly large -> collapses to one pull
            "death_penalty_s": 30,
        })
        assert result["ok"] is True
        assert result["report"]["death_cost"]["per_death_s"] == 30


class TestAnalyzeAutoSavesLocally:
    """analyze() ("New Analysis") used to only ever hold its report in
    memory for as long as the report screen stayed open -- unlike Watch
    Live, which has always written its recorded runs to disk. Real gap
    (2026-09-01, user question: "are reports saved locally? if not can
    we add this?"): closing the app (or just navigating away) lost the
    report for good, with no way back short of re-running the analysis
    from the same log. _save_report_locally() fixes this the same way
    start_watch() already has zero-config output -- see
    config.resolve_output_dir/resolve_history_db_path.
    """

    def test_saves_json_and_html_and_ingests_into_history_with_zero_config(
        self, api, log_file, isolated_config_dir,
    ):
        result = api.analyze({"log_path": str(log_file)})
        assert result["ok"] is True
        saved = result["saved"]
        assert saved is not None

        json_path = Path(saved["json_path"])
        html_path = Path(saved["html_path"])
        assert json_path.exists() and json_path.parent == isolated_config_dir / "analyzed-runs"
        assert html_path.exists()
        assert json.loads(json_path.read_text())["run"]["zone"] == "Murder Row"

        assert isinstance(saved["run_id"], int)

        from postmortem.history.store import query_runs
        rows = query_runs(isolated_config_dir / "history.db")
        assert len(rows) == 1
        assert rows[0]["zone"] == "Murder Row"

    def test_honors_configured_output_dir_and_history_db(
        self, api, log_file, tmp_path,
    ):
        custom_out = tmp_path / "my-reports"
        custom_db = tmp_path / "my-history.db"
        api.save_settings({
            "default_output_dir": str(custom_out), "history_db_path": str(custom_db),
        })

        result = api.analyze({"log_path": str(log_file)})
        assert result["ok"] is True
        saved = result["saved"]
        assert Path(saved["json_path"]).parent == custom_out
        assert custom_db.exists()

    def test_a_second_analysis_of_the_same_run_updates_rather_than_duplicates(
        self, api, log_file,
    ):
        # Store.ingest() is already idempotent on (zone, start_ts) --
        # this just confirms analyze()'s own auto-save goes through that
        # same path rather than e.g. re-ingesting with a fresh identity
        # every time (re-analyzing the same log with a tweaked pull-gap
        # setting is a completely normal thing to do).
        api.analyze({"log_path": str(log_file)})
        api.analyze({"log_path": str(log_file)})

        from postmortem.history.store import query_runs
        rows = query_runs(config_module.config_dir() / "history.db")
        assert len(rows) == 1

    def test_a_save_failure_does_not_break_returning_the_report(
        self, api, log_file, monkeypatch,
    ):
        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr("postmortem.history.store.ingest", boom)
        result = api.analyze({"log_path": str(log_file)})
        assert result["ok"] is True
        assert result["saved"] is None
        assert result["report"]["run"]["zone"] == "Murder Row"  # the report itself is unaffected


class TestGetDefaultPaths:
    def test_zero_config_reports_the_fallback_locations(self, api, isolated_config_dir):
        result = api.get_default_paths()
        assert result["ok"] is True
        assert result["output_dir"] == str(isolated_config_dir / "analyzed-runs")
        assert result["history_db_path"] == str(isolated_config_dir / "history.db")

    def test_configured_settings_are_reflected(self, api, tmp_path):
        api.save_settings({
            "default_output_dir": str(tmp_path / "out"),
            "history_db_path": str(tmp_path / "runs.db"),
        })
        result = api.get_default_paths()
        assert result["output_dir"] == str(tmp_path / "out")
        assert result["history_db_path"] == str(tmp_path / "runs.db")


class TestAnalyzeFailureNeverRaises:
    def test_missing_log_path_key(self, api):
        result = api.analyze({})
        assert result == {"ok": False, "error": "error: log_path is required"}

    def test_empty_params(self, api):
        result = api.analyze(None)  # tolerate a falsy/None params dict too
        assert result["ok"] is False

    def test_nonexistent_log_path(self, api, tmp_path):
        result = api.analyze({"log_path": str(tmp_path / "nope.txt")})
        assert result["ok"] is False
        assert "error" in result

    def test_log_with_no_runs(self, api, tmp_path):
        empty = tmp_path / "empty.txt"
        empty.write_text("8/30/2026 20:00:00.000-4  SPELL_CAST_SUCCESS,a,b,c,d\n",
                          encoding="utf-8")
        result = api.analyze({"log_path": str(empty)})
        assert result["ok"] is False
        assert "no Mythic+ runs" in result["error"]

    def test_malformed_route_string(self, api, log_file):
        result = api.analyze({
            "log_path": str(log_file),
            "route": "!!! this is not a valid MDT export string !!!",
        })
        assert result["ok"] is False
        assert "error" in result

    def test_nonexistent_dungeon_data_path(self, api, log_file, route_string, tmp_path):
        result = api.analyze({
            "log_path": str(log_file),
            "route": route_string,
            "dungeon_data_path": str(tmp_path / "does-not-exist.json"),
        })
        assert result["ok"] is False
        assert "error" in result

    def test_nonexistent_avoidable_data_path(self, api, log_file, tmp_path):
        result = api.analyze({
            "log_path": str(log_file),
            "avoidable_data_path": str(tmp_path / "does-not-exist.json"),
        })
        assert result["ok"] is False
        assert "error" in result

    def test_run_selector_out_of_range(self, api, three_run_log_file):
        result = api.analyze({
            "log_path": str(three_run_log_file), "run_selector": "99",
        })
        assert result["ok"] is False
        assert "out of range" in result["error"]


# -- list_history ---------------------------------------------------------


class TestListHistory:
    def test_requires_db_path_or_directory(self, api):
        result = api.list_history()
        assert result["ok"] is False

    def test_from_db_path(self, api, log_file, route_string, dungeon_data_file, tmp_path):
        analyzed = api.analyze({
            "log_path": str(log_file),
            "route": route_string,
            "dungeon_data_path": str(dungeon_data_file),
        })
        assert analyzed["ok"] is True
        db_path = tmp_path / "runs.db"
        history_ingest(analyzed["report"], db_path)

        result = api.list_history(db_path=str(db_path))
        assert result["ok"] is True
        assert len(result["rows"]) == 1
        assert result["rows"][0]["zone"] == "Murder Row"
        assert "<html" in result["html"]

    def test_from_directory(self, api, log_file, route_string, dungeon_data_file, tmp_path):
        analyzed = api.analyze({
            "log_path": str(log_file),
            "route": route_string,
            "dungeon_data_path": str(dungeon_data_file),
        })
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        (reports_dir / "run.json").write_text(
            json.dumps(analyzed["report"]), encoding="utf-8",
        )

        result = api.list_history(directory=str(reports_dir))
        assert result["ok"] is True
        assert len(result["rows"]) == 1
        assert result["rows"][0]["zone"] == "Murder Row"
        assert "<html" in result["html"]

    def test_db_and_directory_rows_share_the_same_shape(
        self, api, log_file, route_string, dungeon_data_file, tmp_path,
    ):
        analyzed = api.analyze({
            "log_path": str(log_file),
            "route": route_string,
            "dungeon_data_path": str(dungeon_data_file),
        })
        report = analyzed["report"]

        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        (reports_dir / "run.json").write_text(json.dumps(report), encoding="utf-8")
        dir_result = api.list_history(directory=str(reports_dir))

        db_path = tmp_path / "runs.db"
        history_ingest(report, db_path)
        db_result = api.list_history(db_path=str(db_path))

        assert set(dir_result["rows"][0]) == set(db_result["rows"][0])

    def test_bad_db_path_does_not_raise(self, api, tmp_path):
        # a directory that can't hold a sqlite file (parent missing, no
        # permission to create it) -- exercise the "never raise" contract
        result = api.list_history(db_path=str(tmp_path / "nope" / "sub" / "runs.db"))
        # Store() creates parent dirs, so this actually succeeds with an
        # empty result; assert it at least doesn't raise and is well-formed.
        assert "ok" in result


# -- upload_report ------------------------------------------------------


class TestUploadReport:
    # config_dir isolation is now file-wide -- see the module-level
    # isolated_config_dir fixture above.

    def test_no_url_and_no_saved_setting_returns_error(self, api):
        result = api.upload_report({"run": {}})
        assert result == {"ok": False, "error": "no site URL configured"}

    def test_explicit_url_is_used_over_saved_setting(self, api, monkeypatch):
        seen = {}

        def fake_upload_report(report, url, **kwargs):
            seen["report"] = report
            seen["url"] = url
            return {"ok": True, "run_id": 1, "url": "/runs/1"}

        monkeypatch.setattr("postmortem.upload.upload_report", fake_upload_report)
        api.save_settings({"site_url": "https://saved.example"})

        result = api.upload_report({"run": {"zone": "x"}}, "https://explicit.example")
        assert result == {"ok": True, "run_id": 1, "url": "/runs/1"}
        assert seen["url"] == "https://explicit.example"
        assert seen["report"] == {"run": {"zone": "x"}}

    def test_falls_back_to_saved_site_url_setting(self, api, monkeypatch):
        seen = {}

        def fake_upload_report(report, url, **kwargs):
            seen["url"] = url
            return {"ok": True, "run_id": 2, "url": "/runs/2"}

        monkeypatch.setattr("postmortem.upload.upload_report", fake_upload_report)
        api.save_settings({"site_url": "https://saved.example"})

        result = api.upload_report({"run": {}})
        assert result["ok"] is True
        assert seen["url"] == "https://saved.example"

    def test_upstream_failure_is_returned_as_is(self, api, monkeypatch):
        monkeypatch.setattr(
            "postmortem.upload.upload_report",
            lambda report, url, **kwargs: {"error": "already submitted by another uploader"},
        )
        result = api.upload_report({"run": {}}, "https://example.com")
        assert result == {"error": "already submitted by another uploader"}


# -- live watch mode ---------------------------------------------------------


class TestWatchMode:
    """start_watch()/stop_watch(): watches a growing combat log on a
    background thread and auto-analyzes + auto-uploads each completed
    run. _emit_watch_event is monkeypatched to capture events instead of
    calling the real webview.windows[0].evaluate_js(...) -- there's no
    live pywebview window under test, matching the caveat already noted
    on the pick_* dialog methods. config_dir isolation is file-wide --
    see the module-level isolated_config_dir fixture above.
    """

    @pytest.fixture()
    def events(self, api, monkeypatch):
        captured = []
        monkeypatch.setattr(api, "_emit_watch_event", captured.append)
        return captured

    def _wait_for(self, events, event_type, timeout=5.0):
        import time as _time

        deadline = _time.time() + timeout
        while _time.time() < deadline:
            for e in events:
                if e["type"] == event_type:
                    return e
            _time.sleep(0.05)
        raise AssertionError(f"no {event_type!r} event within {timeout}s; got {events}")

    def test_missing_log_path_is_an_error(self, api, events):
        result = api.start_watch({"site_url": "https://example.test"})
        assert result == {"ok": False, "error": "log_path is required"}

    def test_missing_site_url_is_an_error(self, api, events, tmp_path):
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")
        result = api.start_watch({"log_path": str(log)})
        assert result == {
            "ok": False,
            "error": "no site URL configured -- set one in Settings first",
        }

    def test_stop_watch_with_nothing_running_is_a_noop_ok(self, api, events):
        assert api.stop_watch() == {"ok": True}

    def test_starting_watch_before_the_log_file_exists_waits_and_recovers(
        self, api, events, tmp_path,
    ):
        # Real UX gap (2026-08-31): WoW only creates the combat log once
        # logging actually turns on -- with this addon, that's automatic
        # at the start of the session's first key. Clicking Start before
        # that (completely normal -- open the app, click Start, then go
        # play) used to crash the watch thread immediately.
        from conftest import build_run_log

        log = tmp_path / "WoWCombatLog.txt"
        assert not log.exists()

        result = api.start_watch({
            "log_path": str(log), "site_url": "https://example.test",
            "out_dir": str(tmp_path / "watch-runs"),
        })
        assert result == {"ok": True}

        waiting_event = self._wait_for(events, "waiting_for_log")
        assert waiting_event["log_path"] == str(log)
        assert api._watch_thread.is_alive()  # still watching, not crashed

        log.write_text(build_run_log().text(), encoding="utf-8")
        run_event = self._wait_for(events, "run_complete")
        assert run_event["zone"] == "Murder Row"

        api.stop_watch()

    def test_full_cycle_analyzes_and_uploads_each_completed_run(
        self, api, events, tmp_path, monkeypatch,
    ):
        from conftest import build_run_log

        uploaded = []

        def fake_upload_report(report, url, **kwargs):
            uploaded.append((report["run"]["zone"], url))
            return {"ok": True, "run_id": 1, "url": "/runs/1"}

        monkeypatch.setattr("postmortem.upload.upload_report", fake_upload_report)

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")

        result = api.start_watch({
            "log_path": str(log),
            "site_url": "https://example.test",
            "out_dir": str(tmp_path / "watch-runs"),
        })
        assert result == {"ok": True}
        assert api._watch_thread is not None and api._watch_thread.is_alive()

        # Simulate WoW appending to the log after watching has already
        # started (the realistic case -- start_watch()'s Recorder
        # defaults to from_start=False, so only lines written from here
        # on are seen). The small sleep is just letting the background
        # thread actually reach its open()+seek-to-end before we append
        # -- start_watch() returns as soon as the thread is *scheduled*,
        # not once it's running, so writing immediately races the
        # thread's own startup (a real key can't start within
        # microseconds of clicking "start watching", so this has no
        # real-world equivalent -- purely a test-timing concern).
        import time as _time
        _time.sleep(0.2)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(build_run_log().text())

        uploaded_event = self._wait_for(events, "uploaded")
        assert uploaded_event["url"] == "https://example.test/runs/1"
        assert uploaded == [("Murder Row", "https://example.test")]

        event_types = [e["type"] for e in events]
        # run_started lands the moment the key's CHALLENGE_MODE_START is
        # seen -- the UI's only sign of life for the whole run until then
        assert event_types == [
            "watching", "run_started", "run_complete", "analyzed", "uploaded",
        ]

    def test_watched_runs_land_in_the_same_local_history_as_new_analysis(
        self, api, events, tmp_path, monkeypatch, isolated_config_dir,
    ):
        # A Watch Live run should show up on the same History screen a
        # "New Analysis" run does -- one unified local history, not two
        # separate silos (see TestAnalyzeAutoSavesLocally above).
        from conftest import build_run_log

        monkeypatch.setattr(
            "postmortem.upload.upload_report",
            lambda report, url, **kwargs: {"ok": True, "run_id": 1, "url": "/runs/1"},
        )

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")
        api.start_watch({
            "log_path": str(log), "site_url": "https://example.test",
            "out_dir": str(tmp_path / "watch-runs"),
        })

        import time as _time
        _time.sleep(0.2)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(build_run_log().text())
        self._wait_for(events, "uploaded")

        from postmortem.history.store import query_runs
        rows = query_runs(isolated_config_dir / "history.db")
        assert len(rows) == 1
        assert rows[0]["zone"] == "Murder Row"
        # by type, not position: run_started now precedes run_complete
        by_type = {e["type"]: e for e in events}
        assert by_type["run_started"]["zone"] == "Murder Row"
        assert by_type["run_complete"]["zone"] == "Murder Row"
        assert by_type["analyzed"]["timed"] is True

        # The recorded slice's own JSON/HTML/chapters actually landed on
        # disk (same as record --analyze) -- _write_recorded_reports is
        # reused, not reimplemented. (.chapters.json is a separate sidecar
        # -- see chapters.py -- so it's excluded from this count.)
        written = [
            p for p in (tmp_path / "watch-runs").glob("*.json")
            if not p.name.endswith(".chapters.json")
        ]
        assert len(written) == 1

        stop_result = api.stop_watch()
        assert stop_result == {"ok": True}
        assert api._watch_thread is None
        assert self._wait_for(events, "stopped")

    def test_writes_the_addon_results_file_when_the_addon_is_installed(
        self, api, events, tmp_path, monkeypatch,
    ):
        # The in-game writeback: with the log in a real WoW layout that
        # has the addon installed, a completed watched run drops
        # PostmortemResults.lua into the addon folder and emits
        # results_written, so the player can /reload to see the stats.
        from conftest import build_run_log

        monkeypatch.setattr(
            "postmortem.upload.upload_report",
            lambda report, url, **kwargs: {"ok": True, "run_id": 1, "url": "/runs/1"},
        )

        flavor = tmp_path / "World of Warcraft" / "_retail_"
        (flavor / "Logs").mkdir(parents=True)
        addon_dir = flavor / "Interface" / "AddOns" / "Postmortem"
        addon_dir.mkdir(parents=True)
        log = flavor / "Logs" / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")

        api.start_watch({
            "log_path": str(log), "site_url": "https://example.test",
            "out_dir": str(tmp_path / "watch-runs"),
        })
        import time as _time
        _time.sleep(0.2)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(build_run_log().text())

        written_event = self._wait_for(events, "results_written")
        assert written_event["zone"] == "Murder Row"
        results = addon_dir / "PostmortemResults.lua"
        assert results.exists()
        assert "Murder Row" in results.read_text(encoding="utf-8")
        api.stop_watch()

    def test_no_addon_writeback_when_addon_not_installed(
        self, api, events, tmp_path, monkeypatch,
    ):
        # Same real WoW layout but no addon folder -> no results_written
        # event, no crash, upload still happens.
        from conftest import build_run_log

        monkeypatch.setattr(
            "postmortem.upload.upload_report",
            lambda report, url, **kwargs: {"ok": True, "run_id": 1, "url": "/runs/1"},
        )
        flavor = tmp_path / "World of Warcraft" / "_retail_"
        (flavor / "Logs").mkdir(parents=True)  # no Interface/AddOns/Postmortem
        log = flavor / "Logs" / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")

        api.start_watch({
            "log_path": str(log), "site_url": "https://example.test",
            "out_dir": str(tmp_path / "watch-runs"),
        })
        import time as _time
        _time.sleep(0.2)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(build_run_log().text())
        self._wait_for(events, "uploaded")  # upload still happens
        assert not any(e["type"] == "results_written" for e in events)
        api.stop_watch()

    def test_analysis_does_not_block_seeing_the_next_key_start(
        self, api, events, tmp_path, monkeypatch,
    ):
        # Real report (2026-09-02): analysis used to run *on the tailing
        # thread*, so for the whole duration of a big run's analysis (37
        # minutes on one real key) no log was read and the next key's
        # start went unseen. With analysis handed to a worker, the next
        # key's run_started must arrive while the previous run is still
        # being analyzed.
        import threading
        import time as _time
        from conftest import LogBuilder, build_run_log

        release = threading.Event()

        def slow_handle(run, *args, **kwargs):
            # the real handler emits run_complete first, then analyzes
            api._emit_watch_event({"type": "run_complete", "zone": run.zone,
                                   "level": run.keystone_level})
            release.wait(timeout=10)  # simulate a long analysis

        monkeypatch.setattr(api, "_handle_watched_run", slow_handle)

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")
        api.start_watch({
            "log_path": str(log), "site_url": "https://example.test",
            "out_dir": str(tmp_path / "watch-runs"),
        })
        _time.sleep(0.2)
        try:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(build_run_log().text())  # key 1: complete
            self._wait_for(events, "run_complete")
            # key 1 is now "being analyzed" (blocked on `release`). Start
            # key 2 -- its run_started must show up regardless.
            b = LogBuilder()
            b.start(1000, zone="Altar of Fangs", instance=2993, cm=588, lvl=7)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(b.text())
            started = [e for e in events if e["type"] == "run_started"]
            deadline = _time.time() + 5
            while len(started) < 2 and _time.time() < deadline:
                _time.sleep(0.05)
                started = [e for e in events if e["type"] == "run_started"]
            assert [e["zone"] for e in started] == ["Murder Row", "Altar of Fangs"]
        finally:
            release.set()
            api.stop_watch()

    def test_a_different_key_starting_reports_the_previous_as_abandoned(
        self, api, events, tmp_path, monkeypatch,
    ):
        import time as _time
        from conftest import LogBuilder

        monkeypatch.setattr(
            "postmortem.upload.upload_report",
            lambda report, url, **kwargs: {"ok": True, "run_id": 1, "url": "/runs/1"},
        )
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")
        api.start_watch({
            "log_path": str(log), "site_url": "https://example.test",
            "out_dir": str(tmp_path / "watch-runs"),
        })
        _time.sleep(0.2)
        b = LogBuilder()
        b.start(0, zone="Kings' Rest", instance=1762, cm=249, lvl=7)   # never ends
        b.start(100, zone="Altar of Fangs", instance=2993, cm=588, lvl=7)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(b.text())
        abandoned = self._wait_for(events, "run_abandoned")
        assert abandoned["zone"] == "Kings' Rest"
        started = [e["zone"] for e in events if e["type"] == "run_started"]
        assert started == ["Kings' Rest", "Altar of Fangs"]
        api.stop_watch()

    def test_second_start_watch_while_active_is_rejected(
        self, api, events, tmp_path,
    ):
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")
        first = api.start_watch({
            "log_path": str(log), "site_url": "https://example.test",
            "out_dir": str(tmp_path / "watch-runs"),
        })
        assert first == {"ok": True}
        try:
            second = api.start_watch({
                "log_path": str(log), "site_url": "https://example.test",
            })
            assert second == {"ok": False, "error": "already watching"}
        finally:
            api.stop_watch()

    def test_upload_failure_is_reported_as_an_event_not_a_crash(
        self, api, events, tmp_path, monkeypatch,
    ):
        from conftest import build_run_log

        monkeypatch.setattr(
            "postmortem.upload.upload_report",
            lambda report, url, **kwargs: {"ok": False, "error": "offline"},
        )

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")
        api.start_watch({
            "log_path": str(log), "site_url": "https://example.test",
            "out_dir": str(tmp_path / "watch-runs"),
        })
        import time as _time
        _time.sleep(0.2)  # see test_full_cycle_...'s comment on this
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(build_run_log().text())

        failed_event = self._wait_for(events, "upload_failed")
        assert failed_event["error"] == "offline"
        api.stop_watch()

    def test_bad_route_string_is_reported_not_raised(self, api, events, tmp_path):
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")
        result = api.start_watch({
            "log_path": str(log),
            "site_url": "https://example.test",
            "route": "not a valid mdt export string",
        })
        assert result["ok"] is False
        assert "error" in result


# -- settings ---------------------------------------------------------------


class TestGetVersion:
    def test_reports_the_stamped_version(self, api, monkeypatch):
        import postmortem.desktop._version as version_module
        monkeypatch.setattr(version_module, "VERSION", "alpha-desktop-11")
        assert api.get_version() == {"ok": True, "version": "alpha-desktop-11"}

    def test_a_source_checkout_reports_dev(self, api):
        # _version.py's checked-in placeholder -- see that module's own
        # docstring. Not monkeypatched here on purpose: this confirms
        # the real, currently-checked-in value, which matters because
        # it's exactly what tells check_for_update() "this isn't a
        # release build, don't offer an update".
        import postmortem.desktop._version as version_module
        assert version_module.VERSION == "dev"
        assert api.get_version() == {"ok": True, "version": "dev"}


class TestSettings:
    # config_dir isolation is now file-wide -- see the module-level
    # isolated_config_dir fixture above.

    def test_get_settings_defaults(self, api):
        assert api.get_settings() == config_module.DEFAULT_SETTINGS

    def test_save_then_get_round_trips(self, api):
        ack = api.save_settings({
            "raiderio_region": "eu",
            "wow_addon_path": "/addons/MDT",
        })
        assert ack == {"ok": True}
        settings = api.get_settings()
        assert settings["raiderio_region"] == "eu"
        assert settings["wow_addon_path"] == "/addons/MDT"

    def test_save_settings_with_none_does_not_raise(self, api):
        ack = api.save_settings(None)
        assert ack == {"ok": True}


# -- extract_dungeon_data -----------------------------------------------


_DUNGEON_LUA = textwrap.dedent("""
local _, MDT = ...
local addonName = MDT.AddonName
local L = MDT.L
local dungeonIndex = 160
MDT.dungeonList[dungeonIndex] = "Murder Row"
MDT.mapInfo[dungeonIndex] = { mapID = 587, englishName = "Murder Row" }
MDT.dungeonTotalCount[dungeonIndex] = { normal = 100 }
MDT.dungeonEnemies[dungeonIndex] = {
  [1] = { ["name"] = "Felwyrm", ["id"] = 236085, ["count"] = 4 },
  [2] = { ["name"] = "Duskblade", ["id"] = 236086, ["count"] = 6 },
}
""")


class TestExtractDungeonData:
    def test_success(self, api, tmp_path):
        addon_dir = tmp_path / "MythicDungeonTools"
        addon_dir.mkdir()
        (addon_dir / "MurderRow.lua").write_text(_DUNGEON_LUA, encoding="utf-8")
        output_path = tmp_path / "mdt_data.json"

        result = api.extract_dungeon_data(str(addon_dir), str(output_path))
        assert result["ok"] is True
        assert result["dungeon_count"] == 1
        assert result["dungeons"] == [
            {"dungeon_idx": 160, "name": "Murder Row", "enemy_count": 2},
        ]
        assert output_path.exists()
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert "160" in payload["dungeons"]

    def test_missing_addon_path_returns_error(self, api, tmp_path):
        result = api.extract_dungeon_data(
            str(tmp_path / "does-not-exist"), str(tmp_path / "out.json"),
        )
        assert result["ok"] is False
        assert "error" in result

    def test_addon_path_is_a_file_not_a_directory(self, api, tmp_path):
        not_a_dir = tmp_path / "file.txt"
        not_a_dir.write_text("nope", encoding="utf-8")
        result = api.extract_dungeon_data(str(not_a_dir), str(tmp_path / "out.json"))
        assert result["ok"] is False
        assert "error" in result


# -- auto-update ----------------------------------------------------------


class TestAutoUpdate:
    """check_for_update()/start_update(): see updater.py for the actual
    check/download/apply logic (covered in tests/test_desktop_updater.py)
    -- these tests are about the bridge layer's own contract: never
    raising, refusing to self-update outside a packaged build, refusing
    an untrusted URL, and pushing progress via _emit_update_event
    (monkeypatched here the same way TestWatchMode patches
    _emit_watch_event -- there's no live pywebview window under test).
    """

    @pytest.fixture()
    def events(self, api, monkeypatch):
        captured = []
        monkeypatch.setattr(api, "_emit_update_event", captured.append)
        return captured

    @pytest.fixture(autouse=True)
    def fast_and_safe_exit(self, monkeypatch):
        # start_update()'s success path calls time.sleep(1.5) then
        # os._exit(0) for real -- both patched so a passing test doesn't
        # take 1.5s and, far more importantly, doesn't kill the pytest
        # process itself.
        monkeypatch.setattr(api_module.time, "sleep", lambda s: None)
        exits = []
        monkeypatch.setattr(api_module.os, "_exit", exits.append)
        return exits

    def _wait_for(self, events, event_type, timeout=5.0):
        import time as _time

        deadline = _time.time() + timeout
        while _time.time() < deadline:
            for e in events:
                if e["type"] == event_type:
                    return e
            _time.sleep(0.02)
        raise AssertionError(f"no {event_type!r} event within {timeout}s; got {events}")

    def test_check_for_update_reports_an_available_update(self, api, monkeypatch):
        monkeypatch.setattr(
            updater_module, "check_for_update",
            lambda: {"tag": "alpha-desktop-9", "download_url": "https://x", "notes": ""},
        )
        result = api.check_for_update()
        assert result == {
            "ok": True,
            "update": {"tag": "alpha-desktop-9", "download_url": "https://x", "notes": ""},
        }

    def test_check_for_update_reports_none_when_up_to_date(self, api, monkeypatch):
        monkeypatch.setattr(updater_module, "check_for_update", lambda: None)
        assert api.check_for_update() == {"ok": True, "update": None}

    def test_check_for_update_never_raises(self, api, monkeypatch):
        def boom():
            raise RuntimeError("network exploded")

        monkeypatch.setattr(updater_module, "check_for_update", boom)
        result = api.check_for_update()
        assert result["ok"] is False
        assert "network exploded" in result["error"]

    def test_start_update_refuses_outside_a_packaged_build(self, api, events, monkeypatch):
        monkeypatch.setattr(api_module.sys, "frozen", False, raising=False)
        result = api.start_update("https://github.com/x/y/releases/download/t/a.zip")
        assert result == {"ok": False, "error": "auto-update only works in a packaged build"}

    def test_start_update_refuses_an_untrusted_url(self, api, events, monkeypatch):
        monkeypatch.setattr(api_module.sys, "frozen", True, raising=False)
        result = api.start_update("https://evil.example.com/a.zip")
        assert result == {"ok": False, "error": "refusing to download from an untrusted source"}

    def test_second_start_update_while_one_is_running_is_rejected(
        self, api, events, monkeypatch,
    ):
        monkeypatch.setattr(api_module.sys, "frozen", True, raising=False)
        started = threading.Event()
        finish = threading.Event()

        def slow_perform_update(url, work_dir, on_progress=None):
            started.set()
            finish.wait(timeout=5.0)
            raise RuntimeError("stop here -- this test only cares about the second call")

        monkeypatch.setattr(updater_module, "perform_update", slow_perform_update)

        first = api.start_update("https://github.com/x/y/releases/download/t/a.zip")
        assert first == {"ok": True}
        assert started.wait(timeout=5.0)

        second = api.start_update("https://github.com/x/y/releases/download/t/a.zip")
        assert second == {"ok": False, "error": "an update is already in progress"}

        finish.set()

    def test_full_success_path_downloads_applies_and_exits(
        self, api, events, monkeypatch, tmp_path,
    ):
        monkeypatch.setattr(api_module.sys, "frozen", True, raising=False)

        new_install = tmp_path / "extracted" / "Postmortem.app"
        applied = []

        def fake_perform_update(url, work_dir, on_progress=None):
            if on_progress:
                on_progress({"written": 50, "total": 100})
            return new_install

        monkeypatch.setattr(updater_module, "perform_update", fake_perform_update)
        monkeypatch.setattr(
            updater_module, "apply_update_and_relaunch",
            lambda path, **kw: applied.append(path),
        )

        result = api.start_update("https://github.com/x/y/releases/download/t/a.zip")
        assert result == {"ok": True}

        self._wait_for(events, "relaunching")
        assert applied == [new_install]
        assert any(e["type"] == "downloading" and e["written"] == 50 for e in events)
        assert any(e["type"] == "applying" for e in events)

    def test_failure_during_download_reports_a_failed_event_not_a_crash(
        self, api, events, monkeypatch,
    ):
        monkeypatch.setattr(api_module.sys, "frozen", True, raising=False)

        def boom(url, work_dir, on_progress=None):
            raise ValueError("disk full")

        monkeypatch.setattr(updater_module, "perform_update", boom)

        result = api.start_update("https://github.com/x/y/releases/download/t/a.zip")
        assert result == {"ok": True}  # the thread started fine; it fails asynchronously

        failed = self._wait_for(events, "failed")
        assert "disk full" in failed["error"]
