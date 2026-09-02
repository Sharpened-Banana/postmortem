"""End-to-end CLI tests, Lua extractor, and the live recorder."""

import json
import textwrap
from pathlib import Path

from conftest import LogBuilder, build_run_log

from postmortem.cli import _pick_run, main
from postmortem.combatlog.parser import parse_file
from postmortem.combatlog.segmenter import segment_runs
from postmortem.mdt.extract import extract_dungeon_file
from postmortem.recorder import Recorder


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


class TestRecorderRunBoundaries:
    """Run-boundary handling in the *live* recorder.

    segment_runs() (the offline path) has always split runs correctly;
    Recorder._feed() reimplements boundary detection for live tailing and
    used to handle neither of these cases -- confirmed against a real
    watch-runs slice (2026-09-02): a 132MB file named for a Kings' Rest
    key held that key's CHALLENGE_MODE_START *and* a later Altar of
    Fangs key's, still growing, with neither run ever uploading.
    """

    @staticmethod
    def _filler(b, t):
        b.raw(t, 'SPELL_DAMAGE,Player-1-0001,"Tank-Realm",0x511,0x0,'
                 'Creature-0-1-1-1-2-1,"Mob",0xa48,0x0,133,"Fireball",0x4,'
                 'Creature-0-1-1-1-2-1,0000000000000000,100,100,0,0,0,0,0,0,'
                 '0,0,0,0,0.0,0.0,0,0.0,0,500,500,0,0,nil,nil,nil')

    def _record(self, tmp_path, builder, stop_after_runs=None):
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(builder.text(), encoding="utf-8")
        completed = []
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", from_start=True,
            on_run_complete=completed.append, echo=lambda s: None,
        )
        return rec.watch(stop_after_runs=stop_after_runs), completed

    def test_a_different_key_closes_the_previous_unfinished_run(self, tmp_path):
        b = LogBuilder()
        b.start(0, zone="Kings' Rest", instance=1762, cm=249, lvl=7)
        self._filler(b, 5)
        # A second, genuinely different key -- the first never wrote an END
        # (abandoned). It must not swallow this key's events.
        b.start(100, zone="Altar of Fangs", instance=2993, cm=588, lvl=7)
        self._filler(b, 105)
        b.end(200, success=1, lvl=7, ms=200000, instance=2993)

        runs, completed = self._record(tmp_path, b, stop_after_runs=1)

        # Only the *completed* second key is handed to on_run_complete --
        # an abandoned run has no end point to analyze.
        assert [r.zone for r in completed] == ["Altar of Fangs"]
        finished = completed[0]
        assert finished.completed
        assert finished.keystone_level == 7

        # Each key got its own slice, and the second key's slice starts at
        # its own CHALLENGE_MODE_START -- not partway through the first's.
        second = finished.path.read_text(encoding="utf-8")
        assert second.count("CHALLENGE_MODE_START") == 1
        assert "Altar of Fangs" in second
        assert "Kings' Rest" not in second

        slices = sorted((tmp_path / "runs").glob("*.txt"))
        assert len(slices) == 2
        first = next(p for p in slices if p != finished.path).read_text(encoding="utf-8")
        assert "Kings' Rest" in first
        assert "Altar of Fangs" not in first

    def test_same_key_start_is_a_reload_and_keeps_one_run(self, tmp_path):
        # A mid-key /reload re-logs CHALLENGE_MODE_START for the SAME key.
        # That's one run, not two -- matching segment_runs()'s same_key path.
        b = LogBuilder()
        b.start(0, zone="Murder Row", instance=2830, cm=587, lvl=10)
        self._filler(b, 5)
        b.start(50, zone="Murder Row", instance=2830, cm=587, lvl=10)
        self._filler(b, 55)
        b.end(200, success=1, lvl=10, ms=200000, instance=2830)

        runs, completed = self._record(tmp_path, b, stop_after_runs=1)

        assert len(completed) == 1
        run = completed[0]
        assert run.zone == "Murder Row"
        assert run.completed
        assert len(list((tmp_path / "runs").glob("*.txt"))) == 1
        # Both starts live in the one slice, so the run stays continuous.
        assert run.path.read_text(encoding="utf-8").count("CHALLENGE_MODE_START") == 2

    def test_phantom_end_does_not_report_a_completed_run(self, tmp_path):
        # WoW's all-zeroed phantom end (totalTimeMs == 0) means abandoned,
        # not finished -- closing it as completed would auto-analyze and
        # auto-upload a run that never actually ended. segment_runs()
        # already keys on exactly this; the live recorder didn't.
        #
        # Fed line-by-line rather than through watch(): nothing here ever
        # completes a run, and watch() without stop_after_runs tails
        # forever by design.
        b = LogBuilder()
        b.start(0, zone="Murder Row", instance=2830, cm=587, lvl=10)
        self._filler(b, 5)
        b.end(50, success=0, lvl=0, ms=0, instance=2830)

        out_dir = tmp_path / "runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        completed = []
        rec = Recorder(
            log_path=tmp_path / "WoWCombatLog.txt", out_dir=out_dir,
            on_run_complete=completed.append, echo=lambda s: None,
        )
        returned = [rec._feed(ln) for ln in b.text().splitlines(keepends=True)]

        assert completed == []                 # nothing auto-analyzed/uploaded
        assert all(r is None for r in returned)
        assert rec._current is None            # the run was closed, not left open

    def test_two_same_dungeon_keys_in_one_second_get_separate_slices(self, tmp_path):
        # Slice filenames are only unique to the second. Two keys in the
        # same dungeon at the same level can be processed within one second
        # whenever the reader is catching up on buffered log (a resumed
        # watch, or the backlog after a long analysis) -- the second used
        # to silently overwrite the first's slice. A real log (2026-09-02)
        # holds exactly this: two separate Altar of Fangs +7 keys.
        b = LogBuilder()
        b.start(0, zone="Altar of Fangs", instance=2993, cm=588, lvl=7)
        self._filler(b, 5)
        b.end(100, success=1, lvl=7, ms=100000, instance=2993)
        b.start(200, zone="Altar of Fangs", instance=2993, cm=588, lvl=7)
        self._filler(b, 205)
        b.end(300, success=1, lvl=7, ms=100000, instance=2993)

        runs, completed = self._record(tmp_path, b, stop_after_runs=2)

        assert len(completed) == 2
        first, second = completed
        assert first.path != second.path
        assert first.path.exists() and second.path.exists()
        # Each slice holds exactly its own key, not one clobbering the other.
        for run in (first, second):
            assert run.path.read_text(encoding="utf-8").count(
                "CHALLENGE_MODE_START") == 1

    def test_a_real_end_after_a_phantom_still_completes_the_next_run(self, tmp_path):
        b = LogBuilder()
        b.start(0, zone="Murder Row", instance=2830, cm=587, lvl=10)
        b.end(50, success=0, lvl=0, ms=0, instance=2830)  # phantom: abandoned
        b.start(60, zone="Murder Row", instance=2830, cm=587, lvl=10)
        self._filler(b, 65)
        b.end(300, success=1, lvl=10, ms=240000, instance=2830)

        runs, completed = self._record(tmp_path, b, stop_after_runs=1)

        assert len(completed) == 1
        assert completed[0].completed
        assert completed[0].zone == "Murder Row"


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
        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.parser import parse_file
        from postmortem.combatlog.segmenter import segment_runs
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

        from postmortem.cli import _write_recorded_reports
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

    def test_write_recorded_reports_enrich_hook_runs_before_rendering(self, tmp_path):
        """The desktop app embeds map art through this hook (mapart.py);
        it must see the finished report before HTML is rendered, and a
        raising hook must never cost the run its reports."""
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(build_run_log().text(), encoding="utf-8")
        rec = Recorder(log_path=log, out_dir=tmp_path / "runs", from_start=True,
                       echo=lambda s: None)
        (run,) = rec.watch(stop_after_runs=1)

        from postmortem.cli import _write_recorded_reports
        seen = {}

        def enrich(report):
            seen["zone"] = report["run"]["zone"]
            report["_enriched"] = True

        report = _write_recorded_reports(run, route=None, store=None, enrich=enrich)
        assert seen == {"zone": "Murder Row"}
        assert report["_enriched"] is True
        # the written JSON carries what the hook added (it ran before writes)
        written = json.loads(Path(f"{run.path.with_suffix('')}.json").read_text(encoding="utf-8"))
        assert written["_enriched"] is True

        def boom(report):
            raise RuntimeError("no art today")

        report2 = _write_recorded_reports(run, route=None, store=None, enrich=boom)
        assert report2 is not None and report2["run"]["zone"] == "Murder Row"

    def test_write_recorded_reports_returns_the_analyzed_report(self, tmp_path):
        """_write_recorded_reports' return value (added for the desktop
        app's watch mode, and for `record --upload`'s CLI parity with
        `analyze --upload`) is the same report dict its own JSON file
        holds -- callers shouldn't need to re-read that file to act on
        the result of an auto-analyzed run."""
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(build_run_log().text(), encoding="utf-8")
        out_dir = tmp_path / "runs"
        rec = Recorder(log_path=log, out_dir=out_dir, from_start=True, echo=lambda s: None)
        (run,) = rec.watch(stop_after_runs=1)

        from postmortem.cli import _write_recorded_reports
        report = _write_recorded_reports(run, route=None, store=None)

        assert report is not None
        assert report["run"]["zone"] == "Murder Row"
        base = run.path.with_suffix("")
        on_disk = json.loads(Path(f"{base}.json").read_text(encoding="utf-8"))
        assert report == on_disk

    def test_write_recorded_reports_threads_avoidable_data(self, tmp_path):
        """Regression (2026-09-01 debug sweep): Watch Live loaded and
        validated the avoidable-damage data file but never passed it into
        analysis -- _write_recorded_reports had no `avoidable` param, so a
        watched run silently produced no avoidable-damage breakdown even
        with the file set. build_run_log()'s Dark Bolt (1216538) hits the
        healer, so tagging it must now surface an avoidable_damage section
        -- and its absence without the param proves the threading is what
        matters."""
        from postmortem.analysis.avoidable import AvoidableData
        from postmortem.cli import _write_recorded_reports

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(build_run_log().text(), encoding="utf-8")
        rec = Recorder(log_path=log, out_dir=tmp_path / "runs",
                       from_start=True, echo=lambda s: None)
        (run,) = rec.watch(stop_after_runs=1)

        avoidable = AvoidableData(spells={1216538: {"name": "Dark Bolt", "note": None}})
        with_av = _write_recorded_reports(run, route=None, store=None,
                                          avoidable=avoidable)
        assert "avoidable_damage" in with_av

        # same run, no avoidable data -> no section (proves it's threaded,
        # not incidentally always present)
        without_av = _write_recorded_reports(run, route=None, store=None)
        assert "avoidable_damage" not in without_av

    def test_request_stop_ends_a_live_watch_loop(self, tmp_path):
        """request_stop() (added for the desktop app's watch mode, which
        runs watch() on a background thread it can't send a
        KeyboardInterrupt to) makes an in-progress watch() return, from
        another thread, within about one poll tick -- and, like the
        existing KeyboardInterrupt path, closes out any run that was
        still being recorded as incomplete rather than dropping it."""
        import threading
        import time

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text("", encoding="utf-8")
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs",
            poll_interval=0.05, echo=lambda s: None,
        )
        results = []
        t = threading.Thread(target=lambda: results.append(rec.watch()))
        t.start()
        time.sleep(0.2)  # let watch() actually open+seek before we act

        # A run that never gets a CHALLENGE_MODE_END -- request_stop()
        # should still close it out as incomplete, same as Ctrl-C would.
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(build_run_log().lines[0] + "\n")

        time.sleep(0.2)
        rec.request_stop()
        t.join(timeout=3)
        assert not t.is_alive()
        assert len(results) == 1
        (runs,) = results
        assert len(runs) == 1
        assert runs[0].completed is False
