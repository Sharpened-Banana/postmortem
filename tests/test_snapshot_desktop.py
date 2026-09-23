"""The desktop app's half of the snapshot keybind (docs/SNAPSHOT.md §3):
settings clamping, the marker -> scheduled build -> snapshot_ready flow,
and the bridge that opens a snapshot page."""

from __future__ import annotations

import json

import pytest

from postmortem.desktop import config as desktop_config
from postmortem.desktop.api import DesktopAPI, _mmss, _snapshot_seconds


@pytest.fixture()
def api() -> DesktopAPI:
    return DesktopAPI()


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_config, "config_dir", lambda: tmp_path / "cfg")


class TestHelpers:
    def test_snapshot_seconds_clamps_and_defaults(self):
        assert _snapshot_seconds(None, 120, 30, 600) == 120
        assert _snapshot_seconds("abc", 120, 30, 600) == 120
        assert _snapshot_seconds("45", 120, 30, 600) == 45
        assert _snapshot_seconds(5, 120, 30, 600) == 30
        assert _snapshot_seconds(9999, 120, 30, 600) == 600

    def test_mmss(self):
        assert _mmss(0) == "0:00"
        assert _mmss(754.4) == "12:34"
        assert _mmss("x") == "?"

    def test_settings_defaults_carry_the_window(self):
        s = desktop_config.load_settings()
        assert s["snapshot_before_s"] == 120 and s["snapshot_after_s"] == 60


class TestOpenSnapshot:
    def test_reads_only_our_own_snapshot_files(self, api, tmp_path):
        good = tmp_path / "20260915_KingsRest_10-snapshot-1.html"
        good.write_text("<title>snap</title>", encoding="utf-8")
        result = api.open_snapshot(str(good))
        assert result["ok"] is True and result["html"] == "<title>snap</title>"
        assert "snapshot" in result["label"]

        other = tmp_path / "settings.json"
        other.write_text("{}", encoding="utf-8")
        assert api.open_snapshot(str(other))["ok"] is False
        assert api.open_snapshot(str(tmp_path / "missing-snapshot-2.html"))["ok"] is False

    def test_never_raises_across_the_bridge(self, api):
        assert api.open_snapshot(None)["ok"] is False  # type: ignore[arg-type]


class TestWatchLiveBuild:
    def test_marker_in_a_recorded_slice_becomes_a_snapshot_page(self, api, tmp_path):
        """The whole desktop path minus the timer's wait: the recorder
        writes the run's slice and reports the marker; the scheduled
        build reads the slice back, builds the healer snapshot, writes
        <slice>-snapshot-1.html and emits snapshot_ready."""
        from conftest import build_run_log
        from postmortem.recorder import Recorder

        b = build_run_log()
        # a healer press 30 s into the key, 200 s before the run ends
        b.snapshot_marker(30.0, "healer")
        b.lines.sort(key=lambda ln: ln[:30])   # keep the synthetic log chronological
        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(b.text(), encoding="utf-8")

        events = []
        api._emit_watch_event = lambda ev: events.append(ev)
        seen = []
        rec = Recorder(log_path=log, out_dir=tmp_path / "out", echo=lambda _m: None,
                       on_snapshot_marker=lambda run, ts, role: seen.append((run, ts, role)))
        rec.out_dir.mkdir(parents=True, exist_ok=True)
        for line in log.read_text(encoding="utf-8").splitlines(keepends=True):
            rec._feed(line)
        assert len(seen) == 1 and seen[0][2] == "healer"
        run, marker_ts, role = seen[0]

        api._build_snapshot_for_marker(run, marker_ts, role, 20, 10, None, None,
                                       None, None, key=("k", 1.0))
        ready = [e for e in events if e["type"] == "snapshot_ready"]
        assert ready, events
        page = tmp_path / "out" / f"{run.path.stem}-snapshot-1.html"
        assert ready[0]["path"] == str(page) and ready[0]["role"] == "healer"
        html = page.read_text(encoding="utf-8")
        assert "healer" in html.lower()
        # the only script is Wowhead's external tooltip loader, no inline code
        assert html.count("<script") == 1 and 'id="wowhead-tooltips" src=' in html
        assert api.open_snapshot(str(page))["ok"] is True

    def test_a_broken_slice_reports_snapshot_failed(self, api, tmp_path):
        from postmortem.recorder import RecordedRun

        events = []
        api._emit_watch_event = lambda ev: events.append(ev)
        run = RecordedRun(path=tmp_path / "nope.txt", zone="X", keystone_level=1,
                          started_at=0.0)
        api._build_snapshot_for_marker(run, 1.0, "tank", 20, 10, None, None, None, None,
                                       key=("k", 1.0))
        assert events and events[-1]["type"] == "snapshot_failed"
