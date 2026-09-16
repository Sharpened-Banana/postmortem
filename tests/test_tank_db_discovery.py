"""Finding the addon's SavedVariables file without asking the user.

The tank spellbook capture is what stops the report hedging about
untalented spells (see analysis/tank_death.py). Making the user paste a
path inside WTF/Account/<ACCOUNT>/SavedVariables/ to get that would leave
almost everyone on the worse report forever, for a reason they have no way
to guess at -- the same trap resolve_avoidable_data_path documents, where
a blank field silently meant "feature missing".

So the desktop app derives it from the WoW paths it already holds. These
cover that derivation and, just as importantly, its failure modes: this
runs against a live game client's files, so half-written and vanishing
files are ordinary conditions, not errors worth failing a run's report
over.
"""

from __future__ import annotations

import pytest

from postmortem.desktop import config as dconfig


def _flavor(tmp_path, name="_retail_"):
    """A minimal WoW flavor folder: Logs/, Interface/AddOns/, WTF/."""
    root = tmp_path / "World of Warcraft" / name
    (root / "Logs").mkdir(parents=True)
    (root / "Interface" / "AddOns" / "MythicDungeonTools").mkdir(parents=True)
    return root


def _savedvars(root, account="ACCOUNT#1", body="PostmortemTankDB = {}\n"):
    path = root / "WTF" / "Account" / account / "SavedVariables" / "Postmortem.lua"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestDiscovery:
    def test_found_from_the_logs_folder(self, tmp_path):
        # What Watch Live stores is a Logs folder, which sits one level
        # inside the flavor folder.
        root = _flavor(tmp_path)
        expected = _savedvars(root)

        found = dconfig.resolve_tank_db_path({"wow_log_path": str(root / "Logs")})
        assert found == expected

    def test_found_from_a_log_file_path(self, tmp_path):
        # The pickers also accept a file, not just its folder.
        root = _flavor(tmp_path)
        expected = _savedvars(root)
        log = root / "Logs" / "WoWCombatLog-2026-09-16.txt"
        log.write_text("", encoding="utf-8")

        assert dconfig.resolve_tank_db_path({"wow_log_path": str(log)}) == expected

    def test_found_from_the_mdt_addon_folder(self, tmp_path):
        # The other path the app knows: <flavor>/Interface/AddOns/<Addon>.
        root = _flavor(tmp_path)
        expected = _savedvars(root)
        addon = root / "Interface" / "AddOns" / "MythicDungeonTools"

        assert dconfig.resolve_tank_db_path({"wow_addon_path": str(addon)}) == expected

    def test_newest_account_wins(self, tmp_path):
        # An install can hold several account folders; the one being played
        # is the one being written to, same reasoning resolve_watch_log_path
        # uses to pick among rotated combat logs.
        import os

        root = _flavor(tmp_path)
        old = _savedvars(root, account="OLD#1")
        new = _savedvars(root, account="NEW#2")
        os.utime(old, (1_600_000_000, 1_600_000_000))
        os.utime(new, (1_700_000_000, 1_700_000_000))

        found = dconfig.resolve_tank_db_path({"wow_log_path": str(root / "Logs")})
        assert found == new

    def test_explicit_setting_wins(self, tmp_path):
        root = _flavor(tmp_path)
        _savedvars(root)
        elsewhere = tmp_path / "somewhere-else.lua"
        elsewhere.write_text("PostmortemTankDB = {}\n", encoding="utf-8")

        found = dconfig.resolve_tank_db_path({
            "wow_log_path": str(root / "Logs"),
            "tank_db_path": str(elsewhere),
        })
        assert found == elsewhere

    def test_explicit_setting_pointing_nowhere_is_not_silently_replaced(self, tmp_path):
        # If the user named a file, quietly analysing a different one would
        # be worse than finding nothing.
        root = _flavor(tmp_path)
        _savedvars(root)

        found = dconfig.resolve_tank_db_path({
            "wow_log_path": str(root / "Logs"),
            "tank_db_path": str(tmp_path / "gone.lua"),
        })
        assert found is None


class TestNoDiscovery:
    @pytest.mark.parametrize("settings", [
        {},
        {"wow_log_path": None, "wow_addon_path": None},
        {"wow_log_path": "/definitely/not/here/Logs"},
        {"wow_addon_path": "/nope"},
        # A path shallow enough that walking up three levels would raise.
        {"wow_addon_path": "/"},
    ])
    def test_returns_none_rather_than_raising(self, settings):
        assert dconfig.resolve_tank_db_path(settings) is None

    def test_wow_installed_but_addon_never_run(self, tmp_path):
        # The SavedVariables file only appears after the addon has saved
        # once. Until then there is simply nothing to find.
        root = _flavor(tmp_path)
        assert dconfig.resolve_tank_db_path({"wow_log_path": str(root / "Logs")}) is None


class TestQuietLoader:
    """The desktop loader must degrade, never raise.

    A SavedVariables file is written by a client that may be running right
    now, so a truncated or half-written read is ordinary. Losing the
    spellbook resolution costs one section of the report; raising would
    cost the user the whole report for the run they just finished.
    """

    def test_unparseable_file_degrades_to_none(self, tmp_path, monkeypatch):
        from postmortem.desktop import api

        broken = tmp_path / "Postmortem.lua"
        broken.write_text("PostmortemTankDB = { this is not lua", encoding="utf-8")
        monkeypatch.setattr(dconfig, "resolve_tank_db_path", lambda s: broken)
        monkeypatch.setattr(dconfig, "load_settings", dict)

        assert api._load_tank_knowledge_quietly() is None

    def test_file_without_the_table_degrades_to_none(self, tmp_path, monkeypatch):
        # Several addons share one SavedVariables file; ours may not have
        # written to it yet.
        from postmortem.desktop import api

        other = tmp_path / "Postmortem.lua"
        other.write_text('SomeOtherAddonDB = { ["global"] = {} }\n', encoding="utf-8")
        monkeypatch.setattr(dconfig, "resolve_tank_db_path", lambda s: other)
        monkeypatch.setattr(dconfig, "load_settings", dict)

        assert api._load_tank_knowledge_quietly() is None

    def test_nothing_found_degrades_to_none(self, monkeypatch):
        from postmortem.desktop import api

        monkeypatch.setattr(dconfig, "resolve_tank_db_path", lambda s: None)
        monkeypatch.setattr(dconfig, "load_settings", dict)

        assert api._load_tank_knowledge_quietly() is None

    def test_a_real_capture_is_loaded(self, tmp_path, monkeypatch):
        from postmortem.desktop import api

        path = tmp_path / "Postmortem.lua"
        path.write_text(
            'PostmortemTankDB = {\n'
            '\t["global"] = {\n'
            '\t\t["deaths"] = {\n'
            '\t\t\t{\n'
            '\t\t\t\t["ts"] = 1788120140,\n'
            '\t\t\t\t["specID"] = 66,\n'
            '\t\t\t\t["known"] = { 31850 },\n'
            '\t\t\t\t["notKnown"] = { 642 },\n'
            '\t\t\t},\n'
            '\t\t},\n'
            '\t},\n'
            '}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(dconfig, "resolve_tank_db_path", lambda s: path)
        monkeypatch.setattr(dconfig, "load_settings", dict)

        knowledge = api._load_tank_knowledge_quietly()
        assert knowledge is not None
        assert knowledge.verdict(66, 31850) is True
        assert knowledge.verdict(66, 642) is False
