"""postmortem.console: printing a report never fails on a legacy console."""

from __future__ import annotations

import io
import sys

from postmortem import console


def _cp1252_stream():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


class TestSafePrint:
    def test_replaces_what_the_console_cannot_encode(self, monkeypatch):
        stream = _cp1252_stream()
        monkeypatch.setattr(sys, "stdout", stream)
        console.safe_print("Death cost: 3 deaths ≈ 45s")  # would raise with plain print()
        stream.flush()
        out = stream.buffer.getvalue().decode("cp1252")
        assert "Death cost: 3 deaths ? 45s" in out

    def test_plain_text_passes_through(self, monkeypatch):
        stream = _cp1252_stream()
        monkeypatch.setattr(sys, "stdout", stream)
        console.safe_print("hello")
        stream.flush()
        assert stream.buffer.getvalue() == b"hello\n"

    def test_survives_a_missing_stream(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", None)
        console.safe_print("≈")  # windowed frozen build: nothing to print to


class TestMakeStreamsSafe:
    def test_reconfigures_to_utf8_with_replacement(self, monkeypatch):
        stream = _cp1252_stream()
        monkeypatch.setattr(sys, "stdout", stream)
        monkeypatch.setattr(sys, "stderr", None)  # must not trip on a missing one
        console.make_streams_safe()
        print("≈", file=sys.stdout)
        sys.stdout.flush()
        assert stream.buffer.getvalue() == "≈\n".encode("utf-8")
