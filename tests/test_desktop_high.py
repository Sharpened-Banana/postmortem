"""Three HIGH audit findings in the desktop app and CLI (2026-09-11).

Two of them are violations of standing contracts in this codebase: every
public method on the desktop API is called across a JS bridge and is
documented never to raise, and an upload failure must never change the
CLI's exit code.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from postmortem.desktop import config as desktop_config
from postmortem.recorder import Recorder
from postmortem.upload import site_base_url, start_device_link, upload_report


class TestASchemeLessSiteUrl:
    """DESK-2. urllib's Request() raises ValueError for a bare hostname,
    and it was built ABOVE the try block -- so a site URL pasted without
    https:// escaped three functions documented not to let it."""

    def test_a_bare_hostname_gets_a_scheme(self):
        assert site_base_url("postmortem-mplus.fly.dev") == "https://postmortem-mplus.fly.dev"

    def test_a_bare_hostname_with_a_page_path_still_normalises(self):
        assert site_base_url("postmortem-mplus.fly.dev/upload") == "https://postmortem-mplus.fly.dev"

    def test_an_explicit_scheme_is_left_alone(self):
        assert site_base_url("http://localhost:8000") == "http://localhost:8000"

    def test_upload_report_returns_a_result_instead_of_raising(self):
        result = upload_report({"run": {}}, "example.com", token="t", timeout=0.01)
        assert isinstance(result, dict) and "ok" in result

    def test_start_device_link_returns_a_result_instead_of_raising(self):
        result = start_device_link("example.com", token="t", timeout=0.01)
        assert isinstance(result, dict)

    def test_a_genuinely_unusable_url_still_does_not_raise(self):
        """Whatever someone types, the answer is a result dict."""
        for url in ("", "   ", "ht!tp://nonsense", "://", "http://"):
            assert isinstance(
                upload_report({"run": {}}, url, token="t", timeout=0.01), dict
            )


class TestAnUnstattableLogFile:
    """DESK-3. glob finds a file and stat can still fail on it a moment
    later -- WoW rotating its own logs is exactly that race. The sort key
    was unguarded, and the call sits before start_watch()'s try block."""

    def test_a_vanished_candidate_does_not_raise(self, tmp_path, monkeypatch):
        real = tmp_path / "WoWCombatLog.txt"
        real.write_text("x", encoding="utf-8")
        ghost = tmp_path / "WoWCombatLog-ghost.txt"
        ghost.write_text("x", encoding="utf-8")

        real_stat = os.stat

        def stat_that_loses_the_ghost(path, *a, **k):
            if str(path).endswith("WoWCombatLog-ghost.txt"):
                raise FileNotFoundError(str(path))
            return real_stat(path, *a, **k)

        monkeypatch.setattr(os, "stat", stat_that_loses_the_ghost)
        chosen = desktop_config.resolve_watch_log_path(tmp_path)
        assert chosen == real, "the unstattable candidate was not skipped"

    def test_a_folder_of_only_unstattable_files_falls_back(self, tmp_path, monkeypatch):
        (tmp_path / "WoWCombatLog-ghost.txt").write_text("x", encoding="utf-8")
        real_stat = os.stat

        def stat_that_loses_every_log(path, *a, **k):
            # Scoped to the log files: glob itself stats the directory, so
            # a blanket failure would break the thing under test rather
            # than the thing being simulated.
            if "WoWCombatLog" in str(path):
                raise FileNotFoundError(str(path))
            return real_stat(path, *a, **k)

        monkeypatch.setattr(os, "stat", stat_that_loses_every_log)
        assert desktop_config.resolve_watch_log_path(tmp_path).name == "WoWCombatLog.txt"


class TestStoppingDuringCatchUp:
    """DESK-4. The resume scan and the catch-up replay ignored the stop
    flag, so Stop reported "stopped" while runs kept being analyzed --
    and clearing the watch state made a second Stop a no-op while letting
    a second recorder start on the same log."""

    def test_catch_up_stops_when_asked(self, tmp_path):
        from conftest import build_three_run_log

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(build_three_run_log().text(), encoding="utf-8")

        replayed: list[str] = []
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", poll_interval=0.02,
            echo=lambda s: None,
            already_processed=lambda zone, ts: False,
        )

        def on_complete(run):
            replayed.append(run.zone)
            rec.request_stop()  # stop after the very first replayed key

        rec.on_run_complete = on_complete
        rec.watch()
        assert len(replayed) == 1, (
            f"catch-up replayed {len(replayed)} keys after being asked to stop"
        )

    def test_the_resume_scan_stops_when_asked(self, tmp_path):
        """The scan walks the whole file before tailing begins, which on a
        real multi-gigabyte log is minutes."""
        from conftest import build_three_run_log

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(build_three_run_log().text(), encoding="utf-8")
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", poll_interval=0.02,
            echo=lambda s: None,
        )
        rec.request_stop()
        started = time.monotonic()
        runs = rec.watch()
        assert time.monotonic() - started < 5.0
        assert runs == []
