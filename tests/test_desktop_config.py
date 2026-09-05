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
            "default_routes": [],
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


class TestResolveAvoidableDataPath:
    """A file dropped into the app's own data folder is picked up with
    zero configuration; an explicit setting still wins; nothing there
    means no tagging, same as before."""

    def test_explicit_setting_wins(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        (isolated_config_dir / config.AVOIDABLE_FILENAME).write_text("{}", encoding="utf-8")
        got = config.resolve_avoidable_data_path({"avoidable_data_path": "/explicit/list.json"})
        assert got == config.Path("/explicit/list.json")

    def test_default_file_in_config_dir_is_picked_up(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        default = isolated_config_dir / config.AVOIDABLE_FILENAME
        default.write_text("{}", encoding="utf-8")
        assert config.resolve_avoidable_data_path({"avoidable_data_path": None}) == default

    def test_none_when_nothing_configured_or_present(self, isolated_config_dir):
        assert config.resolve_avoidable_data_path({"avoidable_data_path": None}) is None


class TestResolveStealableDataPath:
    """Same shape as TestResolveAvoidableDataPath -- deliberately no
    packaged-copy fallback (contrast dungeon/interrupt data), see that
    function's own docstring."""

    def test_explicit_setting_wins(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        (isolated_config_dir / config.STEALABLE_FILENAME).write_text("{}", encoding="utf-8")
        got = config.resolve_stealable_data_path({"stealable_data_path": "/explicit/list.json"})
        assert got == config.Path("/explicit/list.json")

    def test_default_file_in_config_dir_is_picked_up(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        default = isolated_config_dir / config.STEALABLE_FILENAME
        default.write_text("{}", encoding="utf-8")
        assert config.resolve_stealable_data_path({"stealable_data_path": None}) == default

    def test_none_when_nothing_configured_or_present(self, isolated_config_dir):
        assert config.resolve_stealable_data_path({"stealable_data_path": None}) is None


class TestResolveDungeonDataPath:
    """Explicit setting > a dungeon_data.json in the app's data folder >
    the copy packaged with postmortem > None."""

    def test_explicit_setting_wins(self, isolated_config_dir):
        got = config.resolve_dungeon_data_path({"dungeon_data_path": "/x/data.json"})
        assert got == config.Path("/x/data.json")

    def test_file_in_config_dir_beats_the_packaged_copy(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        local = isolated_config_dir / "dungeon_data.json"
        local.write_text("{}", encoding="utf-8")
        assert config.resolve_dungeon_data_path({}) == local

    def test_falls_back_to_the_packaged_copy(self, isolated_config_dir):
        # Nothing configured, nothing in the data folder: the copy shipped
        # inside the package (the one the public site also uses) is used.
        got = config.resolve_dungeon_data_path({})
        assert got is not None and got.name == "dungeon_data.json" and got.is_file()

    def test_none_when_nothing_is_available(self, isolated_config_dir, monkeypatch):
        monkeypatch.setattr(
            config, "bundled_dungeon_data_path",
            lambda: isolated_config_dir / "nope" / "dungeon_data.json",
        )
        assert config.resolve_dungeon_data_path({}) is None


class TestResolveInterruptDataPath:
    """Same resolution order as dungeon data (package-maintained, bundled
    by default), not avoidable data (user-supplied) -- see that function's
    own docstring for why."""

    def test_explicit_setting_wins(self, isolated_config_dir):
        got = config.resolve_interrupt_data_path({"interrupt_data_path": "/x/interrupts.json"})
        assert got == config.Path("/x/interrupts.json")

    def test_file_in_config_dir_beats_the_packaged_copy(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        local = isolated_config_dir / "interrupt_data.json"
        local.write_text("{}", encoding="utf-8")
        assert config.resolve_interrupt_data_path({}) == local

    def test_falls_back_to_the_packaged_copy(self, isolated_config_dir):
        got = config.resolve_interrupt_data_path({})
        assert got is not None and got.name == "interrupt_data.json" and got.is_file()

    def test_none_when_nothing_is_available(self, isolated_config_dir, monkeypatch):
        monkeypatch.setattr(
            config, "bundled_interrupt_data_path",
            lambda: isolated_config_dir / "nope" / "interrupt_data.json",
        )
        assert config.resolve_interrupt_data_path({}) is None


class TestResolveDefaultRoute:
    ROUTES = [
        {"dungeon_idx": 160, "dungeon_name": "Murder Row",
         "challenge_map_id": 587, "route": "MR-ROUTE"},
        {"dungeon_idx": 164, "dungeon_name": "Altar of Fangs",
         "challenge_map_id": None, "route": "AOF-ROUTE"},
    ]

    def _resolve(self, **kw):
        kw.setdefault("challenge_map_id", None)
        kw.setdefault("zone_name", None)
        return config.resolve_default_route({"default_routes": self.ROUTES}, **kw)

    def test_matches_by_challenge_map_id_first(self):
        assert self._resolve(challenge_map_id=587, zone_name="Wrong Name") == "MR-ROUTE"

    def test_matches_by_dungeon_idx(self):
        assert self._resolve(dungeon_idx=164) == "AOF-ROUTE"

    def test_matches_by_zone_name_case_insensitively_as_a_last_resort(self):
        # entry saved with no challenge-map id (no dungeon data at the time)
        assert self._resolve(zone_name="altar OF fangs") == "AOF-ROUTE"

    def test_none_when_nothing_matches(self):
        assert self._resolve(challenge_map_id=999, zone_name="Nowhere") is None

    def test_tolerates_garbage_entries(self):
        got = config.resolve_default_route(
            {"default_routes": ["not a dict", {"route": ""}, None]},
            challenge_map_id=587, zone_name="Murder Row",
        )
        assert got is None


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
