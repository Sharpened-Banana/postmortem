"""Snapshot markers, window builds, focus sections, renderers and the CLI
(docs/SNAPSHOT.md section 2)."""

import json
from pathlib import Path

import pytest
from conftest import DPS1, HEALER, TANK, LogBuilder, build_run_log

from postmortem.analysis.gamedata import ACTIVE_MITIGATION
from postmortem.analysis.snapshot import (
    MARKER_CLUSTER_S,
    MARKER_GAP_S,
    Marker,
    build_snapshot,
    find_markers,
)
from postmortem.cli import main
from postmortem.combatlog.events import Event
from postmortem.combatlog.parser import iter_events, parse_file
from postmortem.combatlog.segmenter import segment_runs
from postmortem.report.snapshot import render_snapshot_html, render_snapshot_text

REAL_LOG = Path(__file__).parent / "fixtures" / "real_logs" / "abandoned_key_instance_mismatch.txt"


def _header(ts: float) -> Event:
    return Event(ts, "COMBAT_LOG_VERSION", ["22", "ADVANCED_LOG_ENABLED", "1"])


def _log_with_markers() -> LogBuilder:
    """The standard synthetic run plus a healer marker right at the healer's
    death (t=66) and a tank marker on the boss (t=110.5) with one Shield of
    the Righteous window and cast, and a mana-bearing heal for the mana
    series. Lines are re-sorted so the log stays chronological the way a
    real one is."""
    b = build_run_log()
    b.snapshot_marker(66.0, "healer")
    b.snapshot_marker(110.5, "tank")
    b.aura(105, TANK, TANK, 53600, "Shield of the Righteous")
    b.aura_removed(109, TANK, TANK, 53600, "Shield of the Righteous")
    b.cast(105, TANK, 53600, "Shield of the Righteous")
    b.heal(104, HEALER, TANK, 8004, "Healing Surge", 30000, hp=900000)
    # the healer's own advanced block (power type 0 = mana) rides on their
    # casts; two casts a minute apart give the series a start and an end
    b.cast(62, HEALER, 8004, "Healing Surge", power_type=0, power=80000, max_power=100000)
    b.cast(112, HEALER, 8004, "Healing Surge", power_type=0, power=30000, max_power=100000)
    b.lines.sort(key=lambda line: line.split("  ", 1)[0])
    return b


@pytest.fixture()
def marked_run():
    (run,) = segment_runs(iter_events(_log_with_markers().lines))
    return run


@pytest.fixture()
def marked_log_file(tmp_path) -> Path:
    path = tmp_path / "WoWCombatLog.txt"
    path.write_text(_log_with_markers().text(), encoding="utf-8")
    return path


class TestFindMarkers:
    def test_cluster_size_encodes_the_role(self):
        events = [
            _header(100.0), _header(100.25),                    # healer
            _header(200.0), _header(200.25), _header(200.5),    # tank
            _header(300.0), _header(300.25), _header(300.5), _header(300.75),  # general
            _header(400.0), _header(400.2), _header(400.4), _header(400.6), _header(400.8),
        ]
        markers = find_markers(events)
        assert markers == [
            Marker(100.0, "healer", 2),
            Marker(200.0, "tank", 3),
            Marker(300.0, "general", 4),
            Marker(400.0, "general", 5),
        ]

    def test_a_single_header_is_never_a_marker(self):
        assert find_markers([_header(10.0), _header(50.0)]) == []

    def test_clustering_is_measured_from_the_first_header(self):
        # 1.0 s apart is the addon's own re-assert at key start (real key,
        # 2026-09-16) -- three of those are three lone headers, no marker
        events = [_header(0.0), _header(1.0), _header(2.0)]
        assert find_markers(events) == []
        # exactly at the gap boundary still belongs to the cluster
        events = [_header(0.0), _header(MARKER_GAP_S)]
        assert find_markers(events) == [Marker(0.0, "healer", 2)]
        # the gap is between NEIGHBOURS: four headers 0.5 s apart span 1.5 s
        # and are still one general marker
        events = [_header(0.0), _header(0.5), _header(1.0), _header(1.5)]
        assert find_markers(events) == [Marker(0.0, "general", 4)]

    def test_ts_is_the_first_headers_and_other_events_are_ignored(self):
        events = [
            Event(99.0, "SPELL_DAMAGE", []), _header(100.0),
            Event(100.1, "SPELL_DAMAGE", []), _header(100.3),
        ]
        (m,) = find_markers(events)
        assert m.ts == 100.0 and m.role == "healer"

    def test_real_log_login_pair_is_not_a_marker(self):
        """The real fixture carries four header lines; the only two close
        together (22:15:11.916 and 22:15:13.915) are WoW's own ~2 s apart
        pair, so nothing here may be mistaken for a keybind press."""
        runs = list(segment_runs(parse_file(REAL_LOG)))
        assert len(runs) == 2
        headers = [
            ev.ts for run in runs for ev in run.events if ev.name == "COMBAT_LOG_VERSION"
        ]
        assert len(headers) == 4
        gaps = [b - a for a, b in zip(headers, headers[1:])]
        assert min(gaps) > MARKER_GAP_S
        assert min(gaps) < 2.5  # the pair really is close -- the rule is what saves it
        for run in runs:
            assert find_markers(run.events) == []

    def test_synthetic_log_lines_round_trip_through_the_parser(self, marked_run):
        markers = find_markers(marked_run.events)
        assert [(m.role, m.count) for m in markers] == [("healer", 2), ("tank", 3)]
        assert markers[0].ts == marked_run.start_ts + 66.0


class TestWindow:
    def test_window_is_clipped_to_the_run(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 66.0)
        snap = report["snapshot"]
        assert snap["t_start"] == 0.0            # 66 - 120 < 0 -> run start
        assert snap["t_end"] == 126.0
        assert snap["window_s"] == 126.0
        assert snap["before_s"] == 120.0 and snap["after_s"] == 60.0
        late = build_snapshot(marked_run, marked_run.start_ts + 130.0, before_s=5, after_s=60)
        assert late["snapshot"]["t_start"] == 125.0
        assert late["snapshot"]["t_end"] == 140.0  # the run ends at 140

    def test_slice_limits_every_section(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 20.0, before_s=10, after_s=10)
        # pull 1 only: no deaths (the healer dies at 66), one pull
        assert report["deaths"] == []
        assert len(report["pulls"]) == 1
        assert all(10.0 <= p["t_start"] for p in report["pulls"])
        assert report["series"]["t0"] == 10.0
        assert report["series"]["n"] == 21
        assert report["snapshot"]["role"] == "general"  # no marker at t=20
        assert report["snapshot"]["focus_player"] is None
        assert report["snapshot"]["source"] == "marker"

    def test_output_dict_has_the_documented_keys(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 66.0)
        assert set(report) == {
            "snapshot", "run", "players", "pulls", "deaths", "close_calls",
            "dispels", "interrupts", "enemy_casts", "cc", "consumables",
            "series", "focus", "around_marker",
        }
        assert set(report["snapshot"]) >= {
            "marker_ts", "t_marker", "before_s", "after_s", "start_ts",
            "end_ts", "role", "focus_player", "source",
        }
        assert report["run"]["zone"] == "Murder Row"
        # every ts carries a run-relative t
        for key in ("deaths", "close_calls", "interrupts", "dispels", "consumables"):
            for entry in report[key]:
                assert entry["t"] == pytest.approx(entry["ts"] - marked_run.start_ts, abs=0.1)
        json.dumps(report)  # JSON-ready

    def test_role_auto_takes_the_marker_role_and_explicit_role_wins(self, marked_run):
        auto = build_snapshot(marked_run, marked_run.start_ts + 66.0)
        assert auto["snapshot"]["role"] == "healer"
        forced = build_snapshot(marked_run, marked_run.start_ts + 66.0, role="tank")
        assert forced["snapshot"]["role"] == "tank"
        assert forced["snapshot"]["focus_player"] == TANK[1]
        with pytest.raises(ValueError):
            build_snapshot(marked_run, marked_run.start_ts + 66.0, role="dps")

    def test_spec_survives_a_window_that_starts_after_combatant_info(self, marked_run):
        """COMBATANT_INFO is logged at t=0.5; a window starting at t=100
        must still know who the tank is."""
        report = build_snapshot(marked_run, marked_run.start_ts + 110.5, before_s=10, after_s=10)
        assert report["snapshot"]["role"] == "tank"
        assert report["snapshot"]["focus_player"] == TANK[1]
        assert {p["name"]: p["role"] for p in report["players"]}[TANK[1]] == "tank"


class TestHealerFocus:
    def test_focus_is_the_healer_with_hp_and_mana_series(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 66.0)
        assert report["snapshot"]["focus_player"] == HEALER[1]
        f = report["focus"]
        assert f["role"] == "healer"
        healer = report["series"]["players"][HEALER[1]]
        assert any(v is not None for v in healer["hp_pct"])
        assert healer["mana_pct"][62] == 80.0
        assert healer["mana_pct"][112] == 30.0
        assert f["mana"] == {"start": 80.0, "min": 30.0, "end": 30.0}
        # the healer's two synthetic heals on the tank
        assert f["healing_by_target"][0]["name"] == TANK[1]
        assert f["healing_by_target"][0]["total"] == 50000
        assert f["healing_by_spell"][0]["name"] == "Healing Surge"
        assert f["overheal_pct"] == pytest.approx(100.0 * 5000 / 55000, abs=0.1)
        assert f["gcd_use_pct"] is not None
        assert f["damage_taken_by_player"][0]["name"] == HEALER[1]  # she took the 400k
        assert HEALER[1] not in f["mana"]  # shape check: mana is start/min/end only

    def test_series_are_per_second_and_run_relative(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 66.0)
        series = report["series"]
        assert series["step_s"] == 1 and series["t0"] == 0.0
        tank = series["players"][TANK[1]]
        assert len(tank["hp_pct"]) == series["n"]
        assert tank["damage_taken"][11] == 30000        # Fel Bite at t=11
        assert tank["healing_received"][16] == 20000    # Healing Surge, 5k over
        assert series["players"][HEALER[1]]["healing_done"][16] == 20000
        assert series["players"][HEALER[1]]["damage_taken"][66] == 250000
        assert "mana_pct" not in tank                    # energy, not mana

    def test_no_healer_degrades_to_a_note(self):
        b = build_run_log()
        b.lines = [l for l in b.lines if "COMBATANT_INFO" not in l]
        (run,) = segment_runs(iter_events(b.lines))
        report = build_snapshot(run, run.start_ts + 66.0, role="healer")
        assert report["snapshot"]["focus_player"] is None
        assert "no healer" in report["focus"]["note"]
        assert "no healer" in render_snapshot_text(report)


class TestTankFocus:
    def test_mitigation_uptime_and_casts_from_aura_windows(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 110.5, before_s=10, after_s=10)
        f = report["focus"]
        assert f["role"] == "tank"
        (mit,) = f["active_mitigation"]
        assert mit["name"] == "Shield of the Righteous"
        assert mit["casts"] == 1
        assert mit["uptime_s"] == 4.0
        assert mit["uptime_pct"] == pytest.approx(100.0 * 4 / 20, abs=0.1)
        assert f["mitigation_note"] is None
        assert f["damage_taken"] == 90000            # Boss Smash at t=110
        assert f["dtps"]["peak"] == 90000
        assert f["biggest_hits"][0]["spell"] == "Boss Smash"
        assert f["biggest_hits"][0]["t"] == 110.0
        assert f["physical_vs_magic"]["magic"] == 90000  # school 0x4 in the builder

    def test_buff_already_up_at_window_start_counts_from_the_start(self):
        b = build_run_log()
        b.aura(100, TANK, TANK, 2565, "Shield Block")
        b.aura_removed(120, TANK, TANK, 2565, "Shield Block")
        b.lines.sort(key=lambda line: line.split("  ", 1)[0])
        (run,) = segment_runs(iter_events(b.lines))
        report = build_snapshot(run, run.start_ts + 115.0, before_s=5, after_s=5, role="tank")
        (mit,) = report["focus"]["active_mitigation"]
        assert mit["name"] == "Shield Block"
        assert mit["uptime_s"] == 10.0
        assert mit["uptime_pct"] == 100.0

    def test_no_mitigation_seen_degrades_to_a_note(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 20.0, before_s=10,
                                after_s=10, role="tank")
        assert report["focus"]["active_mitigation"] == []
        assert report["focus"]["mitigation_note"] == "no mitigation data"
        assert "no mitigation data" in render_snapshot_text(report)
        assert "no mitigation data" in render_snapshot_html(report)

    def test_table_covers_the_documented_abilities(self):
        names = {name for name, _specs in ACTIVE_MITIGATION.values()}
        assert names >= {
            "Shield Block", "Ignore Pain", "Ironfur", "Demon Spikes",
            "Shield of the Righteous", "Bone Shield", "Death Strike",
            "Shuffle", "Celestial Brew", "Obsidian Scales",
        }


class TestAroundMarker:
    def test_deaths_close_calls_and_largest_hits_within_ten_seconds(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 66.0)
        around = report["around_marker"]
        assert around["radius_s"] == 10.0
        (death,) = around["deaths"]
        assert death["player"] == HEALER[1] and death["t"] == 66.5
        hits = around["largest_hits"]
        assert hits[0]["amount"] == 250000 and hits[0]["player"] == HEALER[1]
        assert hits[0]["hp_pct"] == 0.0
        assert all(56.0 <= h["t"] <= 76.0 for h in hits)
        assert len(hits) <= 10
        # sorted largest first, and the t=11 Fel Bite (30k, far away) is absent
        assert [h["amount"] for h in hits] == sorted((h["amount"] for h in hits), reverse=True)
        assert not any(h["t"] == 11.0 for h in hits)
        # the 20% dip at t=64 is the lead-up to her real death, so it is
        # correctly not a survived close call (see stats._tag_close_calls)
        assert around["close_calls"] == []

    def test_empty_when_nothing_happens(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 50.0, before_s=5, after_s=5)
        assert report["around_marker"] == {
            "radius_s": 10.0, "deaths": [], "close_calls": [], "largest_hits": [],
        }
        assert "nothing landed" in render_snapshot_text(report)


class TestRenderers:
    def test_text_smoke(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 66.0)
        text = render_snapshot_text(report)
        assert "SNAPSHOT (healer) at 1:06" in text
        assert "Window: 0:00 – 2:06" in text
        assert "HEALER FOCUS" in text
        assert "Mana: 80% at start, 30% min, 30% at end" in text
        assert "DEATH  Bigheals-Area52" in text
        assert "PLAYERS (window)" in text
        tank = build_snapshot(marked_run, marked_run.start_ts + 110.5)
        text = render_snapshot_text(tank)
        assert "TANK FOCUS" in text
        assert "Shield of the Righteous" in text and "uptime" in text

    def test_html_is_self_contained_with_svg_charts_and_no_script(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 66.0)
        page = render_snapshot_html(report)
        assert page.startswith("<!DOCTYPE html>")
        assert "<script" not in page
        assert "http://" not in page and "https://" not in page
        assert "@import" not in page and "<link" not in page
        assert "--accent: #C9A227" in page  # brand tokens from report/html.py
        assert page.count("<svg") >= 3       # hp, damage taken, healing, mana
        assert "stroke-dasharray" in page    # the marker line
        assert "Healer focus — Bigheals-Area52" in page
        assert "Around the marker" in page
        assert "Players (window)" in page
        assert "Enemy casts" in page

    def test_html_escapes_log_derived_text(self, marked_run):
        report = build_snapshot(marked_run, marked_run.start_ts + 66.0)
        report["run"]["zone"] = '</title><script>alert(1)</script>'
        report["players"][0]["name"] = '<img src=x onerror=alert(1)>'
        report["around_marker"]["largest_hits"][0]["spell"] = "<b>Bolt</b>"
        page = render_snapshot_html(report)
        assert "<script>" not in page
        assert "<img" not in page
        assert "<b>Bolt</b>" not in page
        assert "&lt;b&gt;Bolt&lt;/b&gt;" in page

    def test_csp_hash_list_still_only_has_the_two_scripted_pages(self):
        from postmortem.report.csp import report_page_script_hashes
        assert len(report_page_script_hashes()) == 2


class TestCli:
    def test_at_renders_one_snapshot_to_stdout(self, marked_log_file, capsys):
        assert main(["snapshot", str(marked_log_file), "--at", "1:06", "--role", "healer"]) == 0
        out = capsys.readouterr().out
        assert "SNAPSHOT (healer) at 1:06" in out
        assert "Focus: Bigheals-Area52" in out

    def test_at_formats_and_output_file(self, marked_log_file, tmp_path, capsys):
        out = tmp_path / "snap.html"
        assert main(["snapshot", str(marked_log_file), "--at", "66", "--format", "html",
                     "-o", str(out)]) == 0
        page = out.read_text(encoding="utf-8")
        assert "<svg" in page and "manual" in page
        assert f"wrote {out}" in capsys.readouterr().err
        assert main(["snapshot", str(marked_log_file), "--at", "0:66", "--format", "json",
                     "--before", "10", "--after", "5"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["snapshot"]["source"] == "manual"
        assert report["snapshot"]["t_start"] == 56.0
        assert report["snapshot"]["t_end"] == 71.0
        # -o that is a directory gets the stem-based name
        d = tmp_path / "dir"
        d.mkdir()
        assert main(["snapshot", str(marked_log_file), "--at", "1:06", "-o", str(d)]) == 0
        assert (d / "WoWCombatLog-snapshot-1.txt").exists()

    def test_at_ts_and_out_of_range(self, marked_log_file, capsys):
        (run,) = segment_runs(parse_file(marked_log_file))
        assert main(["snapshot", str(marked_log_file), "--at-ts", str(run.start_ts + 66.0)]) == 0
        assert "SNAPSHOT (healer)" in capsys.readouterr().out
        with pytest.raises(SystemExit, match="outside this run"):
            main(["snapshot", str(marked_log_file), "--at", "99:00"])
        with pytest.raises(SystemExit, match="not both"):
            main(["snapshot", str(marked_log_file), "--at", "1:00", "--at-ts", "5"])

    def test_no_markers_found(self, log_file, tmp_path, capsys):
        assert main(["snapshot", str(log_file), "-o", str(tmp_path / "out")]) == 0
        assert "no markers found" in capsys.readouterr().err
        assert not list((tmp_path / "out").glob("*snapshot*"))

    def test_every_marker_becomes_a_file(self, marked_log_file, tmp_path, capsys):
        out = tmp_path / "out"
        assert main(["snapshot", str(marked_log_file), "-o", str(out), "--format", "html"]) == 0
        files = sorted(out.glob("*"))
        assert [f.name for f in files] == [
            "WoWCombatLog-snapshot-1.html", "WoWCombatLog-snapshot-2.html",
        ]
        first = files[0].read_text(encoding="utf-8")
        second = files[1].read_text(encoding="utf-8")
        assert "Snapshot (healer) at 1:06" in first
        assert "Snapshot (tank) at 1:50" in second
        err = capsys.readouterr().err
        assert all(f"wrote {f}" in err for f in files)

    def test_run_selector_limits_the_marker_sweep(self, tmp_path, capsys):
        from conftest import build_three_run_log
        b = build_three_run_log()
        b.snapshot_marker(55.5, "tank")    # in run 2
        b.snapshot_marker(105.5, "healer")  # in run 3
        b.lines.sort(key=lambda line: line.split("  ", 1)[0])
        log = tmp_path / "three.txt"
        log.write_text(b.text(), encoding="utf-8")
        out = tmp_path / "o"
        assert main(["snapshot", str(log), "--run", "3", "-o", str(out)]) == 0
        assert [f.name for f in out.glob("*")] == ["three-snapshot-1.txt"]
        assert "healer" in (out / "three-snapshot-1.txt").read_text(encoding="utf-8")
        out2 = tmp_path / "o2"
        assert main(["snapshot", str(log), "-o", str(out2)]) == 0
        assert sorted(f.name for f in out2.glob("*")) == [
            "three-snapshot-1.txt", "three-snapshot-2.txt",
        ]


class TestRecordedReports:
    def test_write_recorded_reports_adds_a_snapshot_per_marker(self, tmp_path, capsys):
        from postmortem.cli import _write_recorded_reports
        from postmortem.recorder import Recorder

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(_log_with_markers().text(), encoding="utf-8")
        rec = Recorder(log_path=log, out_dir=tmp_path / "runs", from_start=True,
                       echo=lambda s: None)
        (run,) = rec.watch(stop_after_runs=1)
        report = _write_recorded_reports(run, route=None, store=None)
        assert report is not None
        base = run.path.with_suffix("")
        assert Path(f"{base}.html").exists()
        snaps = sorted(base.parent.glob(f"{base.name}-snapshot-*.html"))
        assert [p.name for p in snaps] == [
            f"{base.name}-snapshot-1.html", f"{base.name}-snapshot-2.html",
        ]
        assert "Snapshot (healer)" in snaps[0].read_text(encoding="utf-8")
        err = capsys.readouterr().err
        assert all(f"wrote {p}" in err for p in snaps)

    def test_snapshot_failure_never_costs_the_run_its_reports(self, tmp_path, monkeypatch):
        from postmortem import cli as cli_module
        from postmortem.recorder import Recorder

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(_log_with_markers().text(), encoding="utf-8")
        rec = Recorder(log_path=log, out_dir=tmp_path / "runs", from_start=True,
                       echo=lambda s: None)
        (run,) = rec.watch(stop_after_runs=1)

        def boom(*args, **kwargs):
            raise RuntimeError("no snapshots today")

        monkeypatch.setattr(cli_module, "build_snapshot", boom)
        report = cli_module._write_recorded_reports(run, route=None, store=None)
        assert report is not None
        base = run.path.with_suffix("")
        assert Path(f"{base}.html").exists()
        assert not list(base.parent.glob(f"{base.name}-snapshot-*"))
