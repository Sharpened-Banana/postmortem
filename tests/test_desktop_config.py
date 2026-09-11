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
        loaded = config.load_settings()
        assert {k: loaded[k] for k in settings} == settings
        # every other key keeps its default
        assert loaded == {**config.DEFAULT_SETTINGS, **settings}

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

    def test_a_save_keeps_keys_the_caller_did_not_send(self):
        """The settings screen sends only the fields it shows. Merging onto
        the defaults instead of onto the saved file meant one click of Save
        discarded the five data-file overrides, silently changing which
        data files the analysis used (2026-09-11)."""
        config.save_settings({
            "site_url": "https://postmortem-mplus.fly.dev",
            "interrupt_data_path": "/data/interrupts.json",
            "stealable_data_path": "/data/stealable.json",
        })
        config.save_settings({"raiderio_region": "eu"})
        loaded = config.load_settings()
        assert loaded["raiderio_region"] == "eu"
        assert loaded["site_url"] == "https://postmortem-mplus.fly.dev"
        assert loaded["interrupt_data_path"] == "/data/interrupts.json"
        assert loaded["stealable_data_path"] == "/data/stealable.json"

    def test_a_key_outside_the_defaults_also_survives_a_save(self):
        config.save_settings({"future_setting": 42})
        config.save_settings({"raiderio_region": "kr"})
        assert config.load_settings()["future_setting"] == 42

    def test_every_setting_the_resolvers_read_has_a_default(self):
        """A key read by a resolver but missing from DEFAULT_SETTINGS was
        invisible in the settings screen and, before the merge fix, the
        first casualty of a save."""
        for key in ("dungeon_data_path", "interrupt_data_path",
                    "learned_interrupts_path", "learned_spell_damage_path",
                    "stealable_data_path"):
            assert key in config.DEFAULT_SETTINGS

    def test_the_file_is_replaced_atomically(self, isolated_config_dir):
        """A crash mid-write left truncated JSON, which the loader read as
        a reason to reset everything to defaults (2026-09-11)."""
        import json as _json

        config.save_settings({"site_url": "https://example.invalid"})
        path = isolated_config_dir / "desktop_settings.json"
        inode_before = path.stat().st_ino
        config.save_settings({"raiderio_region": "us"})
        assert path.stat().st_ino != inode_before, "written in place, not renamed"
        assert _json.loads(path.read_text())["site_url"] == "https://example.invalid"
        assert not (isolated_config_dir / "desktop_settings.json.tmp").exists()


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

    def test_an_unreadable_file_is_kept_and_reported(self, isolated_config_dir):
        """Falling back to defaults in silence lost the site URL and left
        Watch Live refusing to start with nothing to explain it."""
        isolated_config_dir.mkdir(parents=True)
        path = isolated_config_dir / "desktop_settings.json"
        path.write_text('{"site_url": "https://exa', encoding="utf-8")
        assert config.load_settings() == config.DEFAULT_SETTINGS

        error = config.last_load_error()
        assert error is not None
        kept, message = error
        assert message
        # the bytes are kept, so the next save cannot destroy them
        assert config.Path(kept).read_text().startswith('{"site_url"')

        # ...and a good file afterwards clears the report
        config.save_settings({"raiderio_region": "us"})
        assert config.load_settings()["raiderio_region"] == "us"
        assert config.last_load_error() is None

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


class TestWatchLogFolder:
    """The Watch Live path may be the Logs folder itself (what the
    "Choose Logs folder…" pickers store since 2026-09-06) or a file in it."""

    def test_folder_is_returned_as_is(self, tmp_path):
        assert config.watch_log_folder(tmp_path) == tmp_path

    def test_file_yields_its_folder(self, tmp_path):
        assert config.watch_log_folder(tmp_path / "WoWCombatLog-x.txt") == tmp_path

    def test_missing_path_is_treated_as_a_file(self, tmp_path):
        assert config.watch_log_folder(tmp_path / "gone" / "WoWCombatLog.txt") == tmp_path / "gone"


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
