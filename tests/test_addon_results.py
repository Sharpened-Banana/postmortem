"""Tests for postmortem.addon_results -- the Lua data file written back
into the WoW addon folder so the addon can show crunched stats in-game.
"""

from __future__ import annotations

import subprocess

import pytest

from postmortem.addon_results import (
    RESULTS_FILENAME,
    addon_dir_from_log_path,
    build_results_payload,
    render_results_lua,
    to_lua_literal,
    write_addon_results,
)


class TestLuaSerializer:
    def test_scalars(self):
        assert to_lua_literal(None) == "nil"
        assert to_lua_literal(True) == "true"
        assert to_lua_literal(False) == "false"
        assert to_lua_literal(42) == "42"
        assert to_lua_literal(1.5) == "1.5"

    def test_bool_is_not_serialized_as_int(self):
        # bool is an int subclass in Python -- the serializer must check
        # bool before int, or True would come out as "1".
        assert to_lua_literal(True) == "true"
        assert to_lua_literal(False) == "false"

    def test_non_finite_floats_never_emit_an_invalid_literal(self):
        assert to_lua_literal(float("inf")) == "0"
        assert to_lua_literal(float("nan")) == "0"

    def test_string_escaping(self):
        assert to_lua_literal('hi "there"') == '"hi \\"there\\""'
        assert to_lua_literal("a\\b") == '"a\\\\b"'
        assert to_lua_literal("line\nbreak") == '"line\\nbreak"'

    def test_unicode_player_name_passes_through(self):
        # WoW reads the file as UTF-8; a non-ASCII character name is left
        # intact rather than escaped.
        assert to_lua_literal("Keléthas-Área52") == '"Keléthas-Área52"'

    def test_control_char_is_decimal_escaped(self):
        assert to_lua_literal("a\x07b") == '"a\\7b"'

    def test_empty_containers(self):
        assert to_lua_literal([]) == "{}"
        assert to_lua_literal({}) == "{}"

    def test_dict_uses_bracketed_string_keys(self):
        out = to_lua_literal({"zone": "MR"})
        assert '["zone"] = "MR"' in out


class TestGeneratedLuaIsValid:
    """The whole point is a file WoW's Lua actually loads -- so parse the
    generated file with a real Lua compiler. Skips cleanly where luac
    isn't installed (CI without a Lua toolchain) rather than failing."""

    def _luac(self):
        from shutil import which
        return which("luac")

    def test_render_results_lua_parses_with_luac(self, tmp_path):
        luac = self._luac()
        if not luac:
            pytest.skip("luac not available")
        report = {
            "run": {"zone": 'Zone "quoted" \\ name', "keystone_level": 10,
                    "completed": True, "timed": False, "duration_ms": 1800000,
                    "wall_duration_s": 1795.4},
            "forces": {"pct": 101.2, "killed": 300, "required": 296},
            "enemy_casts": {"kick_efficiency_pct": 42.9},
            "death_cost": {"deaths": 3, "total_s": 45.0},
            "players": [
                {"name": "Keléthas-Área52", "class": "Mage", "spec": "Fire",
                 "role": "dps", "deaths": 1, "interrupts": 4, "dps": 1234567.8,
                 "hps": 0, "damage_done": 999999999, "healing_done": 0,
                 "avoidable_damage_taken": 123456},
            ],
        }
        path = tmp_path / RESULTS_FILENAME
        path.write_text(render_results_lua(report), encoding="utf-8")
        result = subprocess.run([luac, "-p", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


class TestBuildResultsPayload:
    def test_headline_and_players(self):
        report = {
            "run": {"zone": "Murder Row", "keystone_level": 10,
                    "completed": True, "timed": True, "duration_ms": 1173432,
                    "wall_duration_s": 1170.0},
            "forces": {"pct": 100.0, "killed": 300, "required": 296},
            "enemy_casts": {"kick_efficiency_pct": 42.9},
            "death_cost": {"deaths": 1, "total_s": 15.0},
            "players": [{"name": "Tank-Realm", "deaths": 0, "interrupts": 2,
                         "dps": 100.0}],
        }
        payload = build_results_payload(report)
        assert payload["run"]["zone"] == "Murder Row"
        assert payload["run"]["timed"] is True
        assert payload["kick_efficiency_pct"] == 42.9
        assert payload["deaths"] == 1
        assert len(payload["players"]) == 1
        assert payload["players"][0]["interrupts"] == 2
        assert "generated_at" in payload

    def test_optional_sections_absent_when_not_computed(self):
        # No route pasted / no par time -> no adherence_pct / timer keys,
        # mirroring the report's own "absent when not computed" shape.
        report = {"run": {"zone": "X", "keystone_level": 2}, "players": []}
        payload = build_results_payload(report)
        assert "adherence_pct" not in payload
        assert "timer" not in payload

    def test_optional_sections_present_when_computed(self):
        report = {
            "run": {"zone": "X", "keystone_level": 10},
            "players": [],
            "comparison": {"adherence_pct": 66.7},
            "timer": {"par_ms": 1800000, "diff_ms": -120000, "timed": True},
        }
        payload = build_results_payload(report)
        assert payload["adherence_pct"] == 66.7
        assert payload["timer"]["diff_ms"] == -120000


class TestAddonDirDerivation:
    def test_derives_installed_addon_dir_from_log_path(self, tmp_path):
        flavor = tmp_path / "World of Warcraft" / "_retail_"
        (flavor / "Logs").mkdir(parents=True)
        addon = flavor / "Interface" / "AddOns" / "Postmortem"
        addon.mkdir(parents=True)
        log = flavor / "Logs" / "WoWCombatLog.txt"
        assert addon_dir_from_log_path(log) == addon

    def test_returns_none_when_addon_not_installed(self, tmp_path):
        flavor = tmp_path / "World of Warcraft" / "_retail_"
        (flavor / "Logs").mkdir(parents=True)
        log = flavor / "Logs" / "WoWCombatLog.txt"
        # no Interface/AddOns/Postmortem exists
        assert addon_dir_from_log_path(log) is None

    def test_returns_none_for_a_non_logs_layout(self, tmp_path):
        # a log path that isn't inside a "Logs" dir -> can't derive
        weird = tmp_path / "somewhere" / "mylog.txt"
        weird.parent.mkdir(parents=True)
        assert addon_dir_from_log_path(weird) is None


class TestWriteAddonResults:
    def test_writes_the_file_and_returns_its_path(self, tmp_path):
        report = {"run": {"zone": "MR", "keystone_level": 10}, "players": []}
        dest = write_addon_results(report, tmp_path)
        assert dest == tmp_path / RESULTS_FILENAME
        text = dest.read_text(encoding="utf-8")
        assert "PostmortemResults = {" in text
        assert '["zone"] = "MR"' in text

    def test_overwrites_a_previous_result(self, tmp_path):
        write_addon_results({"run": {"zone": "First"}, "players": []}, tmp_path)
        write_addon_results({"run": {"zone": "Second"}, "players": []}, tmp_path)
        text = (tmp_path / RESULTS_FILENAME).read_text(encoding="utf-8")
        assert "Second" in text and "First" not in text
