"""Tests for the desktop API bridge (mythic_analyzer.desktop.api.DesktopAPI).

Covers every method except the three native-dialog pickers
(pick_log_file/pick_route_file/pick_folder/pick_dungeon_data_file/
pick_avoidable_data_file), which need a live pywebview window and can't
be meaningfully unit tested (see api.py's module docstring).
"""

from __future__ import annotations

import json
import textwrap

import pytest

from mythic_analyzer.desktop import config as config_module
from mythic_analyzer.desktop.api import DesktopAPI
from mythic_analyzer.history.store import ingest as history_ingest


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
        import mythic_analyzer.desktop.api as api_module
        from mythic_analyzer.combatlog.segmenter import segment_runs as real_segment_runs

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

        monkeypatch.setattr("mythic_analyzer.upload.upload_report", fake_upload_report)
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

        monkeypatch.setattr("mythic_analyzer.upload.upload_report", fake_upload_report)
        api.save_settings({"site_url": "https://saved.example"})

        result = api.upload_report({"run": {}})
        assert result["ok"] is True
        assert seen["url"] == "https://saved.example"

    def test_upstream_failure_is_returned_as_is(self, api, monkeypatch):
        monkeypatch.setattr(
            "mythic_analyzer.upload.upload_report",
            lambda report, url, **kwargs: {"error": "already submitted by another uploader"},
        )
        result = api.upload_report({"run": {}}, "https://example.com")
        assert result == {"error": "already submitted by another uploader"}


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
