"""History index, Raider.io enrichment, recorder hooks."""

import json

from conftest import build_run_log

from mythic_analyzer.cli import main
from mythic_analyzer.raiderio import enrich_report, realm_slug
from mythic_analyzer.recorder import Recorder
from mythic_analyzer.report.index import build_index, collect_reports


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
