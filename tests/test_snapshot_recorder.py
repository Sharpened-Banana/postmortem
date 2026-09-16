"""The recorder's half of the snapshot keybind (docs/SNAPSHOT.md §3):
clusters of COMBAT_LOG_VERSION lines inside 1.5 s are markers whose
header count is the presser's role; a lone header, or the 2 s-apart pair
every log opens with, is not."""

from __future__ import annotations

from conftest import LogBuilder, PLAYERS

from postmortem.recorder import Recorder, marker_role

HEADER = "COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,1,BUILD_VERSION,12.1.0,PROJECT_ID,1"


def _feed_all(rec: Recorder, text: str) -> None:
    rec.out_dir.mkdir(parents=True, exist_ok=True)   # watch() does this; _feed alone does not
    for line in text.splitlines(keepends=True):
        rec._feed(line)


def _log_with_headers(header_times: list[float]) -> LogBuilder:
    b = LogBuilder()
    b.raw(-2.0, HEADER)          # login pair, 2 s apart, before the key
    b.raw(0.0, HEADER)
    b.start(1.0)
    for p in PLAYERS:
        b.combatant(1.5, p)
    for t in header_times:
        b.raw(t, HEADER)
    b.unit_died(90.0, "Creature-0-0-0-0-1-0", "Mob", 0xa48)  # a later line ends any cluster
    b.end(100.0)
    return b


class TestMarkerRole:
    def test_counts_map_to_roles(self):
        assert marker_role(1) is None
        assert marker_role(2) == "healer"
        assert marker_role(3) == "tank"
        assert marker_role(4) == "general"
        assert marker_role(7) == "general"


class TestRecorderMarkers:
    def _run(self, tmp_path, header_times):
        seen = []
        rec = Recorder(log_path=tmp_path / "log.txt", out_dir=tmp_path / "out",
                       echo=lambda _m: None,
                       on_snapshot_marker=lambda run, ts, role: seen.append((run.zone, ts, role)))
        _feed_all(rec, _log_with_headers(header_times).text())
        return seen

    def test_healer_and_tank_markers_are_told_apart(self, tmp_path):
        seen = self._run(tmp_path, [30.0, 30.25, 60.0, 60.25, 60.5])
        assert [(z, role) for z, _ts, role in seen] == [("Murder Row", "healer"),
                                                       ("Murder Row", "tank")]
        # the marker's time is the FIRST header of its cluster
        assert abs((seen[1][1] - seen[0][1]) - 30.0) < 0.01

    def test_lone_headers_and_the_login_pair_are_not_markers(self, tmp_path):
        assert self._run(tmp_path, [40.0]) == []
        assert self._run(tmp_path, [40.0, 42.0]) == []   # 2 s apart: two ordinary toggles

    def test_a_cluster_still_open_at_run_end_is_reported(self, tmp_path):
        seen = []
        rec = Recorder(log_path=tmp_path / "log.txt", out_dir=tmp_path / "out",
                       echo=lambda _m: None,
                       on_snapshot_marker=lambda run, ts, role: seen.append(role))
        b = LogBuilder()
        b.start(0.0)
        b.raw(50.0, HEADER)
        b.raw(50.3, HEADER)
        b.end(50.6)          # the END line itself is inside the 1.5 s window
        _feed_all(rec, b.text())
        assert seen == ["healer"]

    def test_a_raising_hook_never_stops_recording(self, tmp_path):
        def boom(run, ts, role):
            raise RuntimeError("ui gone")
        rec = Recorder(log_path=tmp_path / "log.txt", out_dir=tmp_path / "out",
                       echo=lambda _m: None, on_snapshot_marker=boom)
        completed = []
        rec.on_run_complete = lambda run: completed.append(run.zone)
        _feed_all(rec, _log_with_headers([30.0, 30.2]).text())
        assert completed == ["Murder Row"]
