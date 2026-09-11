"""Medium audit findings in the desktop app, the recorder and the local
history store (2026-09-11).

Each of these is a silent failure: work skipped, lost or stalled with the
interface still reporting that everything is fine.
"""

from __future__ import annotations

import threading
import time

import pytest

from postmortem.history.store import (
    has_run,
    has_uploaded_run,
    ingest,
    mark_uploaded,
)
from postmortem.recorder import Recorder


def _log_with_runs(tmp_path, zones):
    from conftest import LogBuilder

    b = LogBuilder()
    t = 0.0
    for i, zone in enumerate(zones):
        b.start(t, zone=zone, instance=2830 + i, cm=580 + i, lvl=10)
        b.end(t + 100, success=1, lvl=10, ms=971306, instance=2830 + i)
        t += 200
    path = tmp_path / "WoWCombatLog.txt"
    path.write_text(b.text(), encoding="utf-8")
    return path


class TestCatchUpDedupesOnUploadNotOnHistory:
    """DESK-6. A key analyzed by hand is in local history, so catch-up
    treated it as dealt with -- and it could never reach the site."""

    def _report(self, zone, start_ts):
        return {
            "run": {"zone": zone, "start_ts": start_ts, "keystone_level": 10,
                    "completed": True, "timed": True, "wall_duration_s": 100.0},
            "dungeon": {"name": zone},
            "players": [],
            "deaths": [],
        }

    def test_a_run_in_history_is_not_yet_uploaded(self, tmp_path):
        db = tmp_path / "history.db"
        ingest(self._report("Murder Row", 1000.0), db)
        assert has_run(db, "Murder Row", 1000.0) is True
        assert has_uploaded_run(db, "Murder Row", 1000.0) is False

    def test_marking_it_uploaded_settles_it(self, tmp_path):
        db = tmp_path / "history.db"
        ingest(self._report("Murder Row", 1000.0), db)
        assert mark_uploaded(db, "Murder Row", 1000.0) is True
        assert has_uploaded_run(db, "Murder Row", 1000.0) is True

    def test_a_missing_database_means_nothing_is_uploaded(self, tmp_path):
        assert has_uploaded_run(tmp_path / "nope.db", "Murder Row", 1.0) is False

    def test_the_desktop_helper_treats_an_unreadable_history_as_not_done(self, tmp_path):
        """Skipping a key on a database error loses it forever; replaying
        one is cheap and de-duplicated at ingest."""
        from postmortem.desktop.api import _already_uploaded

        broken = tmp_path / "broken.db"
        broken.write_text("this is not a database", encoding="utf-8")
        assert _already_uploaded(broken, "Murder Row", 1.0) is False


class TestALockedHistoryIsNotSilentlyUnprocessed:
    """STORE-8. has_run() caught every exception and returned False, so a
    transient lock read as "never processed" -- an instruction to
    re-analyze and re-upload finished work."""

    def test_a_missing_database_is_still_false(self, tmp_path):
        assert has_run(tmp_path / "nope.db", "Murder Row", 1.0) is False

    def test_an_unreadable_database_raises_instead_of_lying(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_text("definitely not sqlite", encoding="utf-8")
        with pytest.raises(Exception):
            has_run(bad, "Murder Row", 1.0)


class TestCatchUpIsCapped:
    """DESK-9. A first watch on a months-old log found every finished key
    unprocessed and analyzed and uploaded all of them."""

    def test_only_the_newest_keys_are_replayed(self, tmp_path):
        log = _log_with_runs(tmp_path, ["Zone A", "Zone B", "Zone C", "Zone D"])
        replayed, skipped = [], []
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", poll_interval=0.02,
            echo=lambda s: None,
            already_processed=lambda zone, ts: False,
            max_catch_up_runs=2,
            on_catch_up_skipped=skipped.append,
            on_run_complete=lambda run: replayed.append(run.zone),
        )
        t = threading.Thread(target=rec.watch, daemon=True)
        t.start()
        time.sleep(0.4)
        rec.request_stop()
        t.join(timeout=5)

        assert replayed == ["Zone C", "Zone D"], "the cap kept the wrong keys"
        assert skipped == [2]

    def test_no_cap_replays_everything(self, tmp_path):
        log = _log_with_runs(tmp_path, ["Zone A", "Zone B", "Zone C"])
        replayed = []
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", poll_interval=0.02,
            echo=lambda s: None,
            already_processed=lambda zone, ts: False,
            on_run_complete=lambda run: replayed.append(run.zone),
        )
        t = threading.Thread(target=rec.watch, daemon=True)
        t.start()
        time.sleep(0.5)
        rec.request_stop()
        t.join(timeout=5)
        assert replayed == ["Zone A", "Zone B", "Zone C"]

    def test_the_initial_scan_reports_progress(self, tmp_path, monkeypatch):
        """The scan walks the whole file before tailing begins -- minutes
        on a multi-gigabyte log, with the interface already saying it was
        watching."""
        import postmortem.recorder as recorder_module

        monkeypatch.setattr(recorder_module, "_SCAN_PROGRESS_EVERY", 64)
        log = _log_with_runs(tmp_path, ["Zone A", "Zone B"])
        seen = []
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", poll_interval=0.02,
            echo=lambda s: None,
            on_scan_progress=lambda done, total: seen.append((done, total)),
        )
        t = threading.Thread(target=rec.watch, daemon=True)
        t.start()
        time.sleep(0.3)
        rec.request_stop()
        t.join(timeout=5)

        assert seen, "the whole-file scan reported nothing"
        done, total = seen[-1]
        assert 0 < done <= total == log.stat().st_size


class TestAVanishedLogIsReported:
    """DESK-7. Both filesystem checks in the idle path swallow their
    errors, so a deleted log or a disconnected drive looked exactly like
    "no keys yet" -- forever, with the interface still saying watching."""

    def test_the_watch_says_when_the_file_is_gone(self, tmp_path):
        log = _log_with_runs(tmp_path, ["Zone A"])
        gone = []
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", poll_interval=0.02,
            rotation_check_s=0.0, follow_rotation=False,
            echo=lambda s: None,
            on_log_unavailable=lambda path: gone.append(path),
        )
        t = threading.Thread(target=rec.watch, daemon=True)
        t.start()
        time.sleep(0.2)
        log.unlink()
        time.sleep(0.3)
        rec.request_stop()
        t.join(timeout=5)

        assert gone and gone[0] == log

    def test_it_is_reported_once_per_disappearance(self, tmp_path):
        log = _log_with_runs(tmp_path, ["Zone A"])
        gone = []
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", poll_interval=0.02,
            rotation_check_s=0.0, follow_rotation=False,
            echo=lambda s: None,
            on_log_unavailable=lambda path: gone.append(path),
        )
        t = threading.Thread(target=rec.watch, daemon=True)
        t.start()
        time.sleep(0.2)
        log.unlink()
        time.sleep(0.4)
        rec.request_stop()
        t.join(timeout=5)
        assert len(gone) == 1, f"reported {len(gone)} times for one disappearance"

    def test_a_present_file_is_never_reported(self, tmp_path):
        log = _log_with_runs(tmp_path, ["Zone A"])
        gone = []
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", poll_interval=0.02,
            rotation_check_s=0.0, follow_rotation=False,
            echo=lambda s: None,
            on_log_unavailable=lambda path: gone.append(path),
        )
        t = threading.Thread(target=rec.watch, daemon=True)
        t.start()
        time.sleep(0.3)
        rec.request_stop()
        t.join(timeout=5)
        assert gone == []


class TestArchiveExtractionIsBounded:
    """DESK-18. Link targets were never checked and the archive's full
    permission bits were restored, set-user-id included -- demonstrated
    against the real function. The bundle check only asked whether the
    executable path existed, which a link to any local binary satisfies."""

    @staticmethod
    def _zip_with(tmp_path, entries):
        """entries: (name, data, external_attr_mode)."""
        import zipfile

        path = tmp_path / "update.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data, mode in entries:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                zf.writestr(info, data)
        return path

    def test_a_symlink_out_of_the_tree_is_refused(self, tmp_path):
        import stat as _stat

        from postmortem.desktop.updater import _safe_extract

        zip_path = self._zip_with(tmp_path, [
            ("Postmortem.app/Contents/MacOS/Postmortem", "../../../../../bin/sh",
             _stat.S_IFLNK | 0o777),
        ])
        with pytest.raises(ValueError, match="escaping"):
            _safe_extract(zip_path, tmp_path / "out")

    def test_an_absolute_symlink_is_refused(self, tmp_path):
        import stat as _stat

        from postmortem.desktop.updater import _safe_extract

        zip_path = self._zip_with(tmp_path, [
            ("link", "/bin/sh", _stat.S_IFLNK | 0o777),
        ])
        with pytest.raises(ValueError, match="absolute symlink"):
            _safe_extract(zip_path, tmp_path / "out")

    def test_a_symlink_inside_the_tree_still_works(self, tmp_path):
        """Real bundles are symlink-heavy internally -- that is why this
        module does not use extractall()."""
        import stat as _stat

        from postmortem.desktop.updater import _safe_extract

        zip_path = self._zip_with(tmp_path, [
            ("Resources/postmortem", "real", 0o644),
            ("Frameworks/postmortem", "../Resources/postmortem", _stat.S_IFLNK | 0o777),
        ])
        out = tmp_path / "out"
        _safe_extract(zip_path, out)
        link = out / "Frameworks" / "postmortem"
        assert link.is_symlink()
        assert link.read_text() == "real"

    def test_setuid_and_group_write_are_masked_off(self, tmp_path):
        import stat as _stat

        from postmortem.desktop.updater import _safe_extract

        zip_path = self._zip_with(tmp_path, [
            ("Postmortem", "binary", _stat.S_IFREG | 0o6777),
        ])
        out = tmp_path / "out"
        _safe_extract(zip_path, out)
        mode = (out / "Postmortem").stat().st_mode
        assert not mode & _stat.S_ISUID
        assert not mode & _stat.S_ISGID
        assert not mode & _stat.S_IWGRP and not mode & _stat.S_IWOTH
        # the executable bit is still honoured -- the reason for all this
        assert mode & _stat.S_IXUSR

    def test_a_linked_executable_does_not_pass_bundle_validation(self, tmp_path):
        from postmortem.desktop.updater import _validate_macos_bundle

        exe = tmp_path / "Postmortem.app" / "Contents" / "MacOS" / "Postmortem"
        exe.parent.mkdir(parents=True)
        real = tmp_path / "somewhere-else"
        real.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.symlink_to(real)
        with pytest.raises(ValueError, match="incomplete"):
            _validate_macos_bundle(tmp_path)
