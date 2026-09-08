"""extract-avoidable: the addon's PostmortemAvoidableDB SavedVariables
capture -> the avoidable_spells.json shape AvoidableData.load() reads,
plus AvoidableData.load_bundled()'s never-raises contract."""

import json
import textwrap

import pytest

from postmortem.analysis import avoidable as avoidable_mod
from postmortem.analysis.avoidable import AvoidableData
from postmortem.cli import main


def _write_savedvariables(tmp_path, body: str):
    path = tmp_path / "Postmortem.lua"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _dungeon_data(tmp_path):
    """A minimal dungeon-data file bridging challenge-map 588 -> MDT
    dungeon_idx 164 (the shape DungeonDataStore.load reads)."""
    path = tmp_path / "dungeon_data.json"
    path.write_text(json.dumps({
        "dungeons": {
            "164": {"dungeon_idx": 164, "name": "Altar of Fangs", "map_id": 588,
                    "zone_ids": [], "total_count": {}, "enemies": []},
        }
    }), encoding="utf-8")
    return path


SV = """
PostmortemDB = { ["global"] = { ["combatLoggingEnabled"] = true } }
PostmortemSpellDB = { ["global"] = {} }
PostmortemAvoidableDB = {
    ["global"] = {
        [1216538] = { ["name"] = "Fel Detonation", ["maps"] = { [588] = true },
                      ["keys"] = 2, ["total"] = 1234567, ["lastSeenTs"] = 1757300000 },
        [999999] = { ["name"] = "spell:999999", ["maps"] = { [588] = true, [77777] = true },
                     ["keys"] = 1, ["total"] = 10, ["lastSeenTs"] = 1757300000 },
        ["junk"] = "ignored",
    },
}
"""


class TestExtractAvoidable:
    def test_extracts_shape_and_groups_by_dungeon(self, tmp_path, capsys):
        sv = _write_savedvariables(tmp_path, SV)
        out = tmp_path / "avoidable_spells.json"

        rc = main(["extract-avoidable", str(sv), "-o", str(out), "--no-bundle",
                   "--dungeon-data", str(_dungeon_data(tmp_path))])
        assert rc == 0

        captured = capsys.readouterr()
        assert "extracted 2 captured spells" in captured.out
        assert "grouped under 1 dungeon" in captured.out
        # map 77777 isn't in the dungeon data: warned about, not dropped
        assert "77777" in captured.err

        payload = json.loads(out.read_text(encoding="utf-8"))
        assert [s["id"] for s in payload["spells"]] == [999999, 1216538]
        by_id = {s["id"]: s for s in payload["spells"]}
        assert by_id[1216538]["name"] == "Fel Detonation"
        assert "seen in 2 keys" in by_id[1216538]["note"]
        assert "seen in 1 key)" in by_id[999999]["note"]
        assert payload["dungeons"] == {"164": [999999, 1216538]}

        # and it round-trips through the loader the analyzer uses
        data = AvoidableData.load(out)
        assert set(data.spells) == {999999, 1216538}
        assert data.dungeons == {164: {999999, 1216538}}

    def test_merges_into_existing_output_without_clobbering_hand_edits(self, tmp_path):
        sv = _write_savedvariables(tmp_path, SV)
        out = tmp_path / "avoidable_spells.json"
        out.write_text(json.dumps({
            "spells": [
                # hand-maintained entry the capture never saw: must survive
                {"id": 424242, "name": "Hand Added", "note": "from a friend"},
                # captured spell with a hand-written note and a real name
                # for what the capture only knows as a placeholder
                {"id": 999999, "name": "Real Name", "note": "my own note"},
            ],
            "dungeons": {"45": [424242]},
        }), encoding="utf-8")

        assert main(["extract-avoidable", str(sv), "-o", str(out), "--no-bundle",
                     "--dungeon-data", str(_dungeon_data(tmp_path))]) == 0

        payload = json.loads(out.read_text(encoding="utf-8"))
        by_id = {s["id"]: s for s in payload["spells"]}
        assert set(by_id) == {424242, 999999, 1216538}
        assert by_id[424242] == {"id": 424242, "name": "Hand Added", "note": "from a friend"}
        assert by_id[999999]["name"] == "Real Name"
        assert by_id[999999]["note"] == "my own note"
        assert payload["dungeons"] == {"45": [424242], "164": [999999, 1216538]}

    def test_no_merge_rebuilds_from_capture_alone(self, tmp_path):
        sv = _write_savedvariables(tmp_path, SV)
        out = tmp_path / "avoidable_spells.json"
        out.write_text(json.dumps({"spells": [{"id": 424242, "name": "Old"}]}),
                       encoding="utf-8")

        assert main(["extract-avoidable", str(sv), "-o", str(out), "--no-bundle",
                     "--no-merge", "--dungeon-data", str(_dungeon_data(tmp_path))]) == 0

        payload = json.loads(out.read_text(encoding="utf-8"))
        assert {s["id"] for s in payload["spells"]} == {999999, 1216538}

    def test_rerun_refreshes_key_count_note(self, tmp_path):
        sv = _write_savedvariables(tmp_path, SV)
        out = tmp_path / "avoidable_spells.json"
        args = ["extract-avoidable", str(sv), "-o", str(out), "--no-bundle",
                "--dungeon-data", str(_dungeon_data(tmp_path))]
        assert main(args) == 0
        sv.write_text(sv.read_text(encoding="utf-8").replace('["keys"] = 2', '["keys"] = 7'),
                      encoding="utf-8")
        assert main(args) == 0
        by_id = {s["id"]: s for s in json.loads(out.read_text(encoding="utf-8"))["spells"]}
        assert "seen in 7 keys" in by_id[1216538]["note"]

    def test_missing_table_is_a_clear_error(self, tmp_path):
        sv = _write_savedvariables(tmp_path, "PostmortemDB = { ['global'] = {} }\n")
        with pytest.raises(SystemExit) as exc:
            main(["extract-avoidable", str(sv), "-o", str(tmp_path / "x.json"), "--no-bundle"])
        assert "PostmortemAvoidableDB" in str(exc.value)

    def test_bundles_by_default(self, tmp_path, monkeypatch):
        from postmortem import bundled
        bundled_path = tmp_path / "pkg" / "avoidable_spells.json"
        monkeypatch.setattr(bundled, "bundled_avoidable_data_path", lambda: bundled_path)
        # the CLI imports the helper lazily from the module, so the patch above is what it sees
        sv = _write_savedvariables(tmp_path, SV)
        out = tmp_path / "avoidable_spells.json"
        assert main(["extract-avoidable", str(sv), "-o", str(out),
                     "--dungeon-data", str(_dungeon_data(tmp_path))]) == 0
        assert bundled_path.is_file()
        assert json.loads(bundled_path.read_text()) == json.loads(out.read_text())


class TestLoadBundled:
    def test_none_when_missing(self, tmp_path, monkeypatch):
        from postmortem import bundled
        monkeypatch.setattr(bundled, "bundled_avoidable_data_path",
                            lambda: tmp_path / "nope.json")
        assert AvoidableData.load_bundled() is None

    def test_none_when_empty_or_broken(self, tmp_path, monkeypatch):
        from postmortem import bundled
        path = tmp_path / "avoidable_spells.json"
        monkeypatch.setattr(bundled, "bundled_avoidable_data_path", lambda: path)
        path.write_text(json.dumps({"spells": []}), encoding="utf-8")
        assert AvoidableData.load_bundled() is None
        path.write_text("{not json", encoding="utf-8")
        assert AvoidableData.load_bundled() is None

    def test_loads_when_present(self, tmp_path, monkeypatch):
        from postmortem import bundled
        path = tmp_path / "avoidable_spells.json"
        monkeypatch.setattr(bundled, "bundled_avoidable_data_path", lambda: path)
        path.write_text(json.dumps({"spells": [{"id": 5, "name": "Five"}]}), encoding="utf-8")
        data = AvoidableData.load_bundled()
        assert data is not None and data.spells[5]["name"] == "Five"
        assert avoidable_mod is not None  # module import sanity
