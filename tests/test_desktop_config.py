"""Desktop app settings persistence (postmortem.desktop.config)."""

from __future__ import annotations

import time

import pytest

from postmortem.desktop import config


@pytest.fixture()
def isolated_config_dir(tmp_path, monkeypatch):
    """A config dir under tmp_path, with config.config_dir() patched to
    return it -- never touches the real user's home directory."""
    fake_dir = tmp_path / "postmortem-config"
    monkeypatch.setattr(config, "config_dir", lambda: fake_dir)
    return fake_dir


# Per-OS config_dir() resolution is now covered by tests/test_appdirs.py --
# config_dir()/APP_DIR_NAME moved to postmortem.appdirs (this module
# just re-exports them), so testing the resolution logic itself belongs
# there, not here.


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
            "site_url": "https://postmortem.fly.dev",
            "wow_log_path": "/wow/Logs/WoWCombatLog.txt",
            "watch_auto_start": True,
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


class TestResolveWatchLogPath:
    """Some WoW installs never write a stable "WoWCombatLog.txt" -- every
    session's log gets a timestamp appended instead (confirmed real
    2026-09-01). resolve_watch_log_path() must find the log actually
    being written to right now, not assume the plain name."""

    def test_falls_back_to_plain_name_when_nothing_logged_yet(self, tmp_path):
        assert config.resolve_watch_log_path(tmp_path) == tmp_path / "WoWCombatLog.txt"

    def test_falls_back_when_folder_does_not_exist(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        assert config.resolve_watch_log_path(missing) == missing / "WoWCombatLog.txt"

    def test_picks_the_most_recently_modified_timestamped_log(self, tmp_path):
        older = tmp_path / "WoWCombatLog-083126_032155.txt"
        newer = tmp_path / "WoWCombatLog-090126_203647.txt"
        older.write_text("old session", encoding="utf-8")
        time.sleep(0.01)
        newer.write_text("current session", encoding="utf-8")
        assert config.resolve_watch_log_path(tmp_path) == newer

    def test_prefers_an_actively_growing_plain_named_log(self, tmp_path):
        # An install that DOES use the stable plain name: an archived
        # previous session sits alongside it, but the plain file is the
        # one being actively written to (newest mtime) and must win.
        archived = tmp_path / "WoWCombatLog-083126_032155.txt"
        archived.write_text("archived session", encoding="utf-8")
        time.sleep(0.01)
        active = tmp_path / "WoWCombatLog.txt"
        active.write_text("this session so far", encoding="utf-8")
        assert config.resolve_watch_log_path(tmp_path) == active
