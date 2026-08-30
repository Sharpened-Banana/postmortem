"""Tests for the local dev server (WP-B3): starts on an ephemeral port in
a background thread, serves an always-fresh index.html, rebuilds only
when a report actually changed, shuts down cleanly with no leaked
thread/socket, and defaults to loopback-only binding.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request

import pytest

from mythic_analyzer.cli import build_parser, main
from mythic_analyzer.history import serve as serve_module
from mythic_analyzer.history.serve import make_server


def _report(zone: str, start_ts: float, level: int = 10) -> dict:
    return {
        "run": {
            "zone": zone,
            "keystone_level": level,
            "start_ts": start_ts,
            "completed": True,
            "timed": True,
            "duration_ms": 1_000_000,
        },
        "forces": {}, "comparison": {}, "enemy_casts": {}, "death_cost": {},
        "deaths": [],
    }


def _get(port: int, path: str = "/index.html") -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        assert resp.status == 200
        return resp.read().decode("utf-8")


def _bump_mtime_past(path, reference_path) -> None:
    """Force ``path``'s mtime strictly newer than ``reference_path``'s,
    regardless of filesystem mtime resolution -- avoids flakiness from two
    fast writes landing in the same tick."""
    newer = reference_path.stat().st_mtime + 5
    os.utime(path, (newer, newer))


@pytest.fixture()
def running_server(tmp_path):
    (tmp_path / "run1.json").write_text(
        json.dumps(_report("Murder Row", 1000)), encoding="utf-8"
    )
    server = make_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, tmp_path
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive(), "server thread did not stop cleanly"


class TestServeBasics:
    def test_serves_index_html(self, running_server):
        server, _tmp_path = running_server
        port = server.server_address[1]
        body = _get(port)
        assert "Murder Row" in body

    def test_serves_root_too(self, running_server):
        server, _tmp_path = running_server
        port = server.server_address[1]
        body = _get(port, "/")
        assert "Murder Row" in body


class TestRegeneratesOnChange:
    def test_new_report_after_first_serve_is_reflected(self, running_server):
        server, tmp_path = running_server
        port = server.server_address[1]

        first = _get(port)
        assert "Second Dungeon" not in first

        new_report = tmp_path / "run2.json"
        new_report.write_text(json.dumps(_report("Second Dungeon", 2000)),
                               encoding="utf-8")
        _bump_mtime_past(new_report, tmp_path / "index.html")

        second = _get(port)
        assert "Second Dungeon" in second
        assert "Murder Row" in second  # original run still present

    def test_no_rebuild_when_nothing_changed(self, running_server, monkeypatch):
        server, tmp_path = running_server
        port = server.server_address[1]

        calls = []
        real_build_index = serve_module.build_index

        def spy(directory, out_path=None):
            calls.append(out_path)
            return real_build_index(directory, out_path)

        monkeypatch.setattr(serve_module, "build_index", spy)

        _get(port)
        assert len(calls) == 1  # index.html didn't exist yet -> one rebuild

        _get(port)
        assert len(calls) == 1  # nothing changed since -> no rebuild

        new_report = tmp_path / "run3.json"
        new_report.write_text(json.dumps(_report("Third Dungeon", 3000)),
                               encoding="utf-8")
        _bump_mtime_past(new_report, tmp_path / "index.html")

        _get(port)
        assert len(calls) == 2  # a report changed -> rebuilt again


class TestBindDefault:
    def test_parser_default_bind_is_loopback(self):
        parser = build_parser()
        args = parser.parse_args(["serve", "somedir"])
        assert args.bind == "127.0.0.1"
        assert args.port == 8765

    def test_make_server_defaults_to_loopback(self, tmp_path):
        server = make_server(tmp_path, port=0)
        try:
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.server_close()


class TestCmdServe:
    def test_handles_keyboard_interrupt_cleanly(self, tmp_path, capsys, monkeypatch):
        closed = []

        class FakeServer:
            server_address = ("127.0.0.1", 12345)

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                closed.append(True)

        def fake_make_server(directory, *, port, bind):
            return FakeServer()

        monkeypatch.setattr(serve_module, "make_server", fake_make_server)
        assert main(["serve", str(tmp_path)]) == 0
        assert closed == [True]
        captured = capsys.readouterr()
        assert "stopped" in (captured.out + captured.err).lower()
