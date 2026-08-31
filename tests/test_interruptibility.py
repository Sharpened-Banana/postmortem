"""InterruptibilityData.load(), and the extract-interrupts extraction
pipeline (real, non-mocked LuaLiteralParser/_find_assignment usage)."""

from __future__ import annotations

import json
import textwrap

import pytest

from postmortem.analysis.interruptibility import InterruptibilityData
from postmortem.cli import main
from postmortem.mdt.extract import LuaLiteralParser, _find_assignment


class TestInterruptibilityDataLoad:
    """Mirrors TestAvoidableDataLoad in tests/test_analysis.py."""

    def test_loads_valid_file(self, tmp_path):
        path = tmp_path / "interrupts.json"
        path.write_text(json.dumps({
            "spells": {
                "196607": {"name": "Eye Beam", "interruptible": True},
                "204331": {"name": "Runic Spike", "interruptible": False},
            },
        }), encoding="utf-8")
        data = InterruptibilityData.load(path)
        assert data.spells[196607] == {"name": "Eye Beam", "interruptible": True}
        assert data.spells[204331] == {"name": "Runic Spike", "interruptible": False}

    def test_get_returns_flag_for_known_spell(self, tmp_path):
        path = tmp_path / "interrupts.json"
        path.write_text(json.dumps({
            "spells": {
                "196607": {"name": "Eye Beam", "interruptible": True},
                "204331": {"name": "Runic Spike", "interruptible": False},
            },
        }), encoding="utf-8")
        data = InterruptibilityData.load(path)
        assert data.get(196607) is True
        assert data.get(204331) is False

    def test_get_returns_none_for_unseen_spell(self, tmp_path):
        path = tmp_path / "interrupts.json"
        path.write_text(json.dumps({"spells": {}}), encoding="utf-8")
        data = InterruptibilityData.load(path)
        assert data.get(999999) is None

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            InterruptibilityData.load(tmp_path / "does-not-exist.json")

    def test_malformed_json_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            InterruptibilityData.load(path)

    def test_missing_spells_key_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"other": {}}), encoding="utf-8")
        with pytest.raises(KeyError):
            InterruptibilityData.load(path)


class TestSpellDBExtraction:
    """Confirms the real extract-interrupts pipeline (_find_assignment +
    LuaLiteralParser, same as mdt/extract.py's extract_dungeon_file)
    correctly locates PostmortemSpellDB specifically -- not
    PostmortemDB, which is also present in the same file -- and
    parses its nested [spellId] = {...} entries, including confirming
    integer-keyed table entries come back as Python int keys."""

    LUA = textwrap.dedent("""
    PostmortemDB = {global = {}}
    PostmortemSpellDB = {global = {
        [12345] = {name = "Test Spell", interruptible = true, lastSeenTs = 123},
        [67890] = {name = "Other Spell", interruptible = false, lastSeenTs = 456},
    }}
    """)

    def test_locates_spell_db_not_regular_db(self):
        pos = _find_assignment(self.LUA, r"PostmortemSpellDB\s*=\s*")
        assert pos is not None
        # the located position should be past the PostmortemDB line
        assert self.LUA.index("PostmortemDB =") < pos
        # and the text right at pos should be the SpellDB table, not
        # PostmortemDB's empty one
        assert self.LUA[pos:].lstrip().startswith("{global = {")
        assert "[12345]" in self.LUA[pos:pos + 200]

    def test_parses_nested_table_with_int_keys(self):
        pos = _find_assignment(self.LUA, r"PostmortemSpellDB\s*=\s*")
        parser = LuaLiteralParser(self.LUA)
        raw = parser.parse_value_at(pos)
        assert isinstance(raw, dict)
        global_table = raw["global"]
        assert isinstance(global_table, dict)
        assert set(global_table.keys()) == {12345, 67890}
        assert all(isinstance(k, int) for k in global_table.keys())
        assert global_table[12345] == {
            "name": "Test Spell", "interruptible": True, "lastSeenTs": 123,
        }
        assert global_table[67890]["interruptible"] is False


class TestExtractInterruptsCLI:
    def _write_savedvariables(self, tmp_path, lua_text):
        path = tmp_path / "Postmortem.lua"
        path.write_text(lua_text, encoding="utf-8")
        return path

    def test_extracts_json_shape(self, tmp_path, capsys):
        lua = textwrap.dedent("""
        PostmortemDB = {global = {someOtherStuff = true}}
        PostmortemSpellDB = {global = {
            [196607] = {name = "Eye Beam", interruptible = true, lastSeenTs = 111},
            [204331] = {name = "Runic Spike", interruptible = false, lastSeenTs = 222},
        }}
        """)
        sv_path = self._write_savedvariables(tmp_path, lua)
        out_path = tmp_path / "interrupt_data.json"

        assert main(["extract-interrupts", str(sv_path), "-o", str(out_path)]) == 0

        out = capsys.readouterr().out
        assert "extracted 2 spells" in out
        assert "1 known interruptible" in out
        assert "1 known uninterruptible" in out

        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload == {
            "spells": {
                "196607": {"name": "Eye Beam", "interruptible": True},
                "204331": {"name": "Runic Spike", "interruptible": False},
            }
        }

        # and the output round-trips through InterruptibilityData.load()
        data = InterruptibilityData.load(out_path)
        assert data.get(196607) is True
        assert data.get(204331) is False
        assert data.get(999) is None

    def test_default_output_filename(self, tmp_path, capsys, monkeypatch):
        lua = "PostmortemSpellDB = {global = {}}"
        sv_path = self._write_savedvariables(tmp_path, lua)
        monkeypatch.chdir(tmp_path)

        assert main(["extract-interrupts", str(sv_path)]) == 0
        assert (tmp_path / "interrupt_data.json").exists()

    def test_missing_file_is_clear_systemexit(self, tmp_path):
        missing = tmp_path / "nope.lua"
        with pytest.raises(SystemExit) as exc:
            main(["extract-interrupts", str(missing)])
        assert "could not read" in str(exc.value)

    def test_missing_assignment_is_clear_systemexit(self, tmp_path):
        sv_path = self._write_savedvariables(tmp_path, "PostmortemDB = {global = {}}")
        with pytest.raises(SystemExit) as exc:
            main(["extract-interrupts", str(sv_path)])
        assert "PostmortemSpellDB" in str(exc.value)
