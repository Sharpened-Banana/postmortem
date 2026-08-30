"""End-to-end CLI tests, Lua extractor, and the live recorder."""

import json
import textwrap

from conftest import build_run_log

from mythic_analyzer.cli import main
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
