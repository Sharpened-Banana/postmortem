"""End-to-end CLI tests, Lua extractor, and the live recorder."""

import json
import textwrap
from pathlib import Path

from conftest import build_run_log

from mythic_analyzer.cli import _pick_run, main
from mythic_analyzer.combatlog.parser import parse_file
from mythic_analyzer.combatlog.segmenter import segment_runs
from mythic_analyzer.mdt.extract import extract_dungeon_file
from mythic_analyzer.recorder import Recorder


class TestExtractor:
    def test_dungeon_file(self, tmp_path):
        lua = textwrap.dedent("""
        local _, MDT = ...
        local addonName = MDT.AddonName
        local L = MDT.L
        local dungeonIndex = 160
        MDT.dungeonList[dungeonIndex] = L["MurderRow"]
        MDT.mapInfo[dungeonIndex] = {
          teleportId = 1286809,
          shortName = L["MurderRowShortName"],
          englishName = "Murder Row",
          mapID = 587
        };
        local zones = { 2433, 2435, 2434 }
        for _, zone in ipairs(zones) do
          MDT.zoneIdToDungeonIdx[zone] = dungeonIndex
        end
        MDT.dungeonTotalCount[dungeonIndex] = { normal = 655 }
        MDT.dungeonSubLevels[dungeonIndex] = { [1] = "Murder Row" }
        MDT.dungeonMaps[dungeonIndex] = {
          [1] = { customTextures = 'Interface\\\\AddOns\\\\'..addonName..'\\\\MurderRow' },
        }
        MDT.mapPOIs[dungeonIndex] = {
          [1] = {
            [1] = { type = "dungeonEntrance", x = 779.77, y = -509.6, sizeMult = 1.5 },
          },
        }
        MDT.dungeonEnemies[dungeonIndex] = {
          [1] = {
            ["name"] = "Felwyrm",
            ["id"] = 236085,
            ["count"] = 1,
            ["health"] = 1297302,
            ["creatureType"] = "Beast",
            ["level"] = 90,
            ["spells"] = { [1214966] = {}, [1216538] = { ["magic"] = true } },
            ["clones"] = {
              [1] = { ["x"] = 697.5, ["y"] = -478.6, ["g"] = 2, ["sublevel"] = 1 },
              [2] = { ["x"] = 705.0, ["y"] = -471.9, ["g"] = 2, ["sublevel"] = 1 },
            },
          },
          [2] = {
            ["name"] = "The Boss",
            ["id"] = 999999,
            ["count"] = 0,
            ["isBoss"] = true,
            ["texture"] = 'Interface\\\\AddOns\\\\'..addonName..'\\\\tex',
            ["clones"] = { [1] = { ["x"] = 1, ["y"] = 2, ["sublevel"] = 1 } },
          },
        }
        """)
        path = tmp_path / "MurderRow.lua"
        path.write_text(lua, encoding="utf-8")
        data = extract_dungeon_file(path)
        assert data["dungeon_idx"] == 160
        assert data["name"] == "Murder Row"
        assert data["map_id"] == 587
        assert data["short_name"] == "MurderRowShortName"
        assert sorted(data["zone_ids"]) == [2433, 2434, 2435]
        assert data["total_count"] == {"normal": 655}
        assert len(data["enemies"]) == 2
        felwyrm = data["enemies"][0]
        assert felwyrm["id"] == 236085
        assert felwyrm["creature_type"] == "Beast"
        assert len(felwyrm["clones"]) == 2
        assert data["enemies"][1]["is_boss"] is True
        assert data["sublevels"] == {"1": "Murder Row"}
        assert data["map_textures"]["1"].endswith("MurderRow")
        assert data["pois"] == {
            "1": [{"type": "dungeonEntrance", "x": 779.77, "y": -509.6, "size_mult": 1.5}]
        }

    def test_missing_optional_tables_dont_break_extraction(self, tmp_path):
        # dungeonSubLevels/dungeonMaps/mapPOIs missing entirely (shouldn't
        # normally happen, but tolerant parsing is the whole ethos here) --
        # the rest of the dungeon should still extract cleanly.
        lua = textwrap.dedent("""
        local dungeonIndex = 161
        MDT.dungeonList[dungeonIndex] = "No Extras"
        MDT.dungeonEnemies[dungeonIndex] = {
          [1] = { ["name"] = "Lonely Mob", ["id"] = 1, ["count"] = 1,
                  ["clones"] = { [1] = { ["x"] = 1, ["y"] = 2, ["sublevel"] = 1 } } },
        }
        """)
        path = tmp_path / "NoExtras.lua"
        path.write_text(lua, encoding="utf-8")
        data = extract_dungeon_file(path)
        assert data["dungeon_idx"] == 161
        assert len(data["enemies"]) == 1
        assert "sublevels" not in data
        assert "map_textures" not in data
        assert "pois" not in data

    def test_non_dungeon_file_skipped(self, tmp_path):
        path = tmp_path / "Other.lua"
        path.write_text("local x = 1", encoding="utf-8")
        assert extract_dungeon_file(path) is None


class TestCLI:
    def test_runs(self, log_file, capsys):
        assert main(["runs", str(log_file)]) == 0
        out = capsys.readouterr().out
        assert "Murder Row +10" in out and "timed" in out

    def test_import_route(self, route_string, dungeon_data_file, capsys):
        assert main(["import-route", route_string,
                     "--dungeon-data", str(dungeon_data_file)]) == 0
        out = capsys.readouterr().out
        assert "Test MR Route" in out
        assert "2x Felwyrm" in out

    def test_import_route_json(self, route_string, capsys):
        assert main(["import-route", route_string, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["pull_count"] == 4

    def test_analyze_all_formats(self, log_file, route_string, dungeon_data_file,
                                 tmp_path, capsys):
        out_dir = tmp_path / "reports"
        assert main([
            "analyze", str(log_file),
            "--route", route_string,
            "--dungeon-data", str(dungeon_data_file),
            "--format", "text,json,html",
            "--out", str(out_dir),
        ]) == 0
        files = sorted(p.name for p in out_dir.iterdir())
        assert len(files) == 3
        json_file = next(p for p in out_dir.iterdir() if p.suffix == ".json")
        report = json.loads(json_file.read_text())
        assert report["comparison"]["adherence_pct"] == 66.7
        txt = next(p for p in out_dir.iterdir() if p.suffix == ".txt").read_text()
        assert "ROUTE vs ACTUAL" in txt

    def test_analyze_route_from_file(self, log_file, route_string, tmp_path, capsys):
        route_file = tmp_path / "route.txt"
        route_file.write_text(route_string)
        assert main(["analyze", str(log_file), "--route", str(route_file)]) == 0
        out = capsys.readouterr().out
        assert "MYTHIC+ POST-MORTEM" in out

    def test_analyze_no_runs(self, tmp_path):
        empty = tmp_path / "empty.txt"
        empty.write_text("8/30/2026 20:00:00.000-4  SPELL_CAST_SUCCESS,a,b,c,d\n")
        try:
            main(["analyze", str(empty)])
        except SystemExit as exc:
            assert "no Mythic+ runs" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit")

    def test_runs_lists_all_three(self, three_run_log_file, capsys):
        assert main(["runs", str(three_run_log_file)]) == 0
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 3
        assert "Cave One +5" in lines[0] and "incomplete" in lines[0]
        assert "Cave Two +10" in lines[1] and "timed" in lines[1]
        assert "Cave Three +15" in lines[2] and "over timer" in lines[2]

    def test_analyze_run_2_picks_middle_run(self, three_run_log_file, capsys):
        assert main(["analyze", str(three_run_log_file), "--run", "2"]) == 0
        out = capsys.readouterr().out
        assert "Cave Two +10" in out
        assert "Cave One" not in out
        assert "Cave Three" not in out

    def test_analyze_run_last_skips_abandoned_run(self, three_run_log_file, capsys):
        assert main(["analyze", str(three_run_log_file), "--run", "last"]) == 0
        out = capsys.readouterr().out
        assert "Cave Three +15" in out

    def test_analyze_run_out_of_range(self, three_run_log_file):
        try:
            main(["analyze", str(three_run_log_file), "--run", "5"])
        except SystemExit as exc:
            assert "out of range (log has 3 runs)" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit")


class TestPickRunStreaming:
    """Direct tests of _pick_run's streaming behavior (WP-A0 memory fix).

    These drive segment_runs/_pick_run directly (rather than through the
    CLI) so they can inspect which RunSegments were actually produced and
    whether their event lists were dropped, which is the part that
    actually proves runs other than the picked one never need to be held
    in memory in full.
    """

    @staticmethod
    def _tap(gen):
        """Wrap a segment generator, recording every segment it yields
        (by identity) as it's consumed, so the test can inspect segments
        _pick_run passed over after the fact."""
        seen = []

        def wrapped():
            for seg in gen:
                seen.append(seg)
                yield seg

        return seen, wrapped()

    def test_numeric_run_stops_early_and_drops_earlier_events(self, three_run_log_file):
        seen, tapped = self._tap(segment_runs(parse_file(three_run_log_file)))
        picked = _pick_run(tapped, "2")

        assert picked.zone_name == "Cave Two"
        assert picked.keystone_level == 10
        assert picked.events  # the picked run's events are retained

        # run 3 should never even have been parsed/yielded: _pick_run must
        # stop driving the generator as soon as it has run 2.
        assert len(seen) == 2
        assert seen[0].zone_name == "Cave One"
        assert seen[1] is picked

        # the abandoned run 1, passed over on the way to run 2, must not
        # retain its events.
        assert seen[0].events == []

    def test_last_keeps_only_current_candidates_events(self, three_run_log_file):
        seen, tapped = self._tap(segment_runs(parse_file(three_run_log_file)))
        picked = _pick_run(tapped, "last")

        assert picked.zone_name == "Cave Three"
        assert picked.events  # the picked (last) run's events are retained

        # 'last' has to scan the whole log to know it's last.
        assert len(seen) == 3
        assert seen[2] is picked

        # every earlier candidate had its events dropped once superseded.
        assert seen[0].events == []
        assert seen[1].events == []

    def test_numeric_run_out_of_range_reports_total(self, three_run_log_file):
        seen, tapped = self._tap(segment_runs(parse_file(three_run_log_file)))
        try:
            _pick_run(tapped, "5")
        except SystemExit as exc:
            assert "out of range (log has 3 runs)" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit")
        # had to scan the whole log to know the true count, but none of
        # the segments' events should have been retained.
        assert len(seen) == 3
        assert all(s.events == [] for s in seen)


class TestAvoidableDataCLI:
    """--avoidable-data: a bad explicitly-passed path is a clear CLI
    error (SystemExit), never a crash and never a silent no-op."""

    def test_missing_file_is_clear_systemexit(self, log_file, tmp_path):
        missing = tmp_path / "nope.json"
        try:
            main(["analyze", str(log_file), "--avoidable-data", str(missing)])
        except SystemExit as exc:
            assert "avoidable-damage data" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit")

    def test_malformed_json_is_clear_systemexit(self, log_file, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        try:
            main(["analyze", str(log_file), "--avoidable-data", str(bad)])
        except SystemExit as exc:
            assert "avoidable-damage data" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit")

    def test_missing_spells_key_is_clear_systemexit(self, log_file, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"dungeons": {}}), encoding="utf-8")
        try:
            main(["analyze", str(log_file), "--avoidable-data", str(bad)])
        except SystemExit as exc:
            assert "avoidable-damage data" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit")

    def test_valid_file_end_to_end_adds_section(self, log_file, tmp_path, capsys):
        good = tmp_path / "avoidable.json"
        good.write_text(json.dumps({
            "spells": [{"id": 1216538, "name": "Dark Bolt"}],
        }), encoding="utf-8")
        assert main(["analyze", str(log_file), "--avoidable-data", str(good)]) == 0
        out = capsys.readouterr().out
        assert "AVOIDABLE DAMAGE" in out
        assert "Bigheals-Area52" in out


class TestRecorder:
    def test_records_run_slice(self, tmp_path):
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(build_run_log().text(), encoding="utf-8")
        out_dir = tmp_path / "runs"
        completed = []
        rec = Recorder(
            log_path=log, out_dir=out_dir, from_start=True,
            on_run_complete=completed.append, echo=lambda s: None,
        )
        runs = rec.watch(stop_after_runs=1)
        assert len(runs) == 1
        run = runs[0]
        assert run.completed
        assert run.zone == "Murder Row"
        assert run.keystone_level == 10
        assert run.player_deaths == 1
        content = run.path.read_text()
        assert content.startswith("8/30/2026")
        assert "CHALLENGE_MODE_START" in content
        assert content.rstrip().endswith("CHALLENGE_MODE_END,2830,1,10,600000")
        assert completed == [run]

        # the recorded slice is itself analyzable
        from mythic_analyzer.analysis.run_analyzer import analyze_run
        from mythic_analyzer.combatlog.parser import parse_file
        from mythic_analyzer.combatlog.segmenter import segment_runs
        (seg,) = list(segment_runs(parse_file(run.path)))
        report = analyze_run(seg)
        assert len(report["pulls"]) == 3

    def test_analyze_writes_chapters_and_vtt_alongside_json_html(self, tmp_path):
        """WP-D2: `--analyze`'s auto-analysis wiring (cli._write_recorded_reports,
        called from cmd_record's analyze_recorded closure) writes the chapters
        sidecars next to the existing JSON/HTML/text reports."""
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(build_run_log().text(), encoding="utf-8")
        out_dir = tmp_path / "runs"
        rec = Recorder(
            log_path=log, out_dir=out_dir, from_start=True, echo=lambda s: None,
        )
        (run,) = rec.watch(stop_after_runs=1)

        from mythic_analyzer.cli import _write_recorded_reports
        _write_recorded_reports(run, route=None, store=None)

        base = run.path.with_suffix("")
        json_path = Path(f"{base}.json")
        html_path = Path(f"{base}.html")
        chapters_path = Path(f"{base}.chapters.json")
        vtt_path = Path(f"{base}.vtt")
        assert json_path.exists()
        assert html_path.exists()
        assert chapters_path.exists()
        assert vtt_path.exists()

        # run.started_at is real wall-clock time.time(), while the report's
        # own start_ts comes from the fixture log's fixed synthetic date/
        # time -- the two can be far apart depending on when the test
        # happens to run, so only the offset *math* (including the
        # clamp-to-zero path) is exercised precisely in test_chapters.py;
        # here we just confirm the wiring produces valid, sorted output.
        chapters = json.loads(chapters_path.read_text(encoding="utf-8"))
        assert chapters
        assert chapters[0]["kind"] == "run_start"
        assert all(c["offset_s"] >= 0 for c in chapters)
        offsets = [c["offset_s"] for c in chapters]
        assert offsets == sorted(offsets)
        kinds = {c["kind"] for c in chapters}
        assert "pull" in kinds or "boss_pull" in kinds
        assert "death" in kinds  # this fixture's run has one death

        vtt_text = vtt_path.read_text(encoding="utf-8")
        assert vtt_text.startswith("WEBVTT\n\n")
        assert "-->" in vtt_text
