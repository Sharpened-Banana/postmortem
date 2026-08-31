"""Tests for the desktop API bridge (postmortem.desktop.api.DesktopAPI).

Covers every method except the three native-dialog pickers
(pick_log_file/pick_route_file/pick_folder/pick_dungeon_data_file/
pick_avoidable_data_file), which need a live pywebview window and can't
be meaningfully unit tested (see api.py's module docstring).
"""

from __future__ import annotations

import json
import textwrap

import pytest

from postmortem.desktop import config as config_module
from postmortem.desktop.api import DesktopAPI
from postmortem.history.store import ingest as history_ingest


@pytest.fixture()
def api() -> DesktopAPI:
    return DesktopAPI()


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
    @pytest.fixture(autouse=True)
    def isolated_config_dir(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / "config"
        monkeypatch.setattr(config_module, "config_dir", lambda: fake_dir)

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
    on the pick_* dialog methods.
    """

    @pytest.fixture(autouse=True)
    def isolated_config_dir(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / "config"
        monkeypatch.setattr(config_module, "config_dir", lambda: fake_dir)

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
        assert event_types == ["watching", "run_complete", "analyzed", "uploaded"]
        assert events[1]["zone"] == "Murder Row"
        assert events[2]["timed"] is True

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


class TestSettings:
    @pytest.fixture(autouse=True)
    def isolated_config_dir(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / "config"
        monkeypatch.setattr(config_module, "config_dir", lambda: fake_dir)

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
