"""Desktop app settings persistence (mythic_analyzer.desktop.config)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mythic_analyzer.desktop import config


@pytest.fixture()
def isolated_config_dir(tmp_path, monkeypatch):
    """A config dir under tmp_path, with config.config_dir() patched to
    return it -- never touches the real user's home directory. Used
    (autouse, class-scoped) by every class below except
    TestConfigDirResolution, which exercises the real per-OS resolution
    logic and must NOT have it patched out."""
    fake_dir = tmp_path / "mythic-analyzer-config"
    monkeypatch.setattr(config, "config_dir", lambda: fake_dir)
    return fake_dir


class TestConfigDirResolution:
    """Directly exercise the per-OS resolution logic (unpatched -- this
    class deliberately does not use the isolated_config_dir fixture)."""

    def test_windows_uses_appdata(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")
        assert config.config_dir() == Path(
            r"C:\Users\someone\AppData\Roaming"
        ) / "mythic-analyzer"

    def test_windows_without_appdata_falls_back(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "win32")
        monkeypatch.delenv("APPDATA", raising=False)
        assert config.config_dir() == Path.home() / ".config" / "mythic-analyzer"

    def test_macos_uses_application_support(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "darwin")
        expected = Path.home() / "Library" / "Application Support" / "mythic-analyzer"
        assert config.config_dir() == expected

    def test_linux_uses_xdg_config_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert config.config_dir() == tmp_path / "xdg" / "mythic-analyzer"

    def test_linux_without_xdg_falls_back_to_dot_config(self, monkeypatch):
        monkeypatch.setattr(config.sys, "platform", "linux")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert config.config_dir() == Path.home() / ".config" / "mythic-analyzer"


class TestLoadSaveRoundTrip:
    @pytest.fixture(autouse=True)
    def _isolate(self, isolated_config_dir):
        pass

    def test_defaults_when_no_file_exists(self, isolated_config_dir):
        assert config.load_settings() == config.DEFAULT_SETTINGS

    def test_save_then_load_round_trips(self, isolated_config_dir):
        settings = {
            "wow_addon_path": "/addons/MythicDungeonTools",
            "raiderio_region": "us",
            "avoidable_data_path": None,
            "default_output_dir": "/reports",
            "history_db_path": "/reports/runs.db",
        }
        config.save_settings(settings)
        assert config.load_settings() == settings

    def test_partial_save_is_merged_onto_defaults(self, isolated_config_dir):
        config.save_settings({"raiderio_region": "eu"})
        loaded = config.load_settings()
        assert loaded["raiderio_region"] == "eu"
        assert loaded["wow_addon_path"] is None
        assert loaded["history_db_path"] is None

    def test_save_creates_config_directory(self, isolated_config_dir):
        assert not isolated_config_dir.exists()
        config.save_settings({"raiderio_region": "kr"})
        assert isolated_config_dir.exists()
        assert (isolated_config_dir / "desktop_settings.json").exists()

    def test_second_save_overwrites_first(self, isolated_config_dir):
        config.save_settings({"raiderio_region": "us"})
        config.save_settings({"raiderio_region": "tw"})
        assert config.load_settings()["raiderio_region"] == "tw"


class TestTolerantOfBadState:
    @pytest.fixture(autouse=True)
    def _isolate(self, isolated_config_dir):
        pass

    def test_missing_file_returns_defaults(self, isolated_config_dir):
        assert not isolated_config_dir.exists()
        assert config.load_settings() == config.DEFAULT_SETTINGS

    def test_corrupt_json_returns_defaults(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        (isolated_config_dir / "desktop_settings.json").write_text(
            "{not valid json", encoding="utf-8",
        )
        assert config.load_settings() == config.DEFAULT_SETTINGS

    def test_non_object_json_returns_defaults(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        (isolated_config_dir / "desktop_settings.json").write_text(
            "[1, 2, 3]", encoding="utf-8",
        )
        assert config.load_settings() == config.DEFAULT_SETTINGS

    def test_unreadable_path_returns_defaults(self, isolated_config_dir):
        # settings_path()'s parent doesn't exist at all -- open() raises
        # FileNotFoundError (an OSError), which must be swallowed too.
        assert config.load_settings() == config.DEFAULT_SETTINGS
