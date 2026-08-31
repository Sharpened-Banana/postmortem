"""OS-appropriate per-user config directory resolution
(mythic_analyzer.appdirs) -- stdlib-only, shared by the CLI and the
(optional) desktop app.
"""

from __future__ import annotations

from pathlib import Path

import mythic_analyzer.appdirs as appdirs


class TestConfigDirResolution:
    """Directly exercise the per-OS resolution logic."""

    def test_windows_uses_appdata(self, monkeypatch):
        monkeypatch.setattr(appdirs.sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")
        assert appdirs.config_dir() == Path(
            r"C:\Users\someone\AppData\Roaming"
        ) / "mythic-analyzer"

    def test_windows_without_appdata_falls_back(self, monkeypatch):
        monkeypatch.setattr(appdirs.sys, "platform", "win32")
        monkeypatch.delenv("APPDATA", raising=False)
        assert appdirs.config_dir() == Path.home() / ".config" / "mythic-analyzer"

    def test_macos_uses_application_support(self, monkeypatch):
        monkeypatch.setattr(appdirs.sys, "platform", "darwin")
        expected = Path.home() / "Library" / "Application Support" / "mythic-analyzer"
        assert appdirs.config_dir() == expected

    def test_linux_uses_xdg_config_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(appdirs.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert appdirs.config_dir() == tmp_path / "xdg" / "mythic-analyzer"

    def test_linux_without_xdg_falls_back_to_dot_config(self, monkeypatch):
        monkeypatch.setattr(appdirs.sys, "platform", "linux")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert appdirs.config_dir() == Path.home() / ".config" / "mythic-analyzer"
