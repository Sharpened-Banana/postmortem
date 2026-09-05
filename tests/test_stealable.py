"""StealableData.load(): schema parsing, same shape as AvoidableData
(see test_analysis.py's TestAvoidableDataLoad, which this mirrors)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from postmortem.analysis.stealable import StealableData


class TestStealableDataLoad:
    def test_loads_example_schema(self, tmp_path):
        path = tmp_path / "stealable.json"
        path.write_text(json.dumps({
            "spells": [
                {"id": 123456, "name": "Empowering Shield", "note": "big shield"},
            ],
        }), encoding="utf-8")
        data = StealableData.load(path)
        assert data.spells[123456] == {"name": "Empowering Shield", "note": "big shield"}
        assert data.is_stealable(123456) is True
        assert data.is_stealable(999) is False

    def test_missing_name_falls_back_to_spell_id(self, tmp_path):
        path = tmp_path / "stealable.json"
        path.write_text(json.dumps({"spells": [{"id": 5}]}), encoding="utf-8")
        data = StealableData.load(path)
        assert data.spells[5]["name"] == "spell:5"
        assert data.spells[5]["note"] is None

    def test_loads_the_shipped_example_file(self):
        example = Path(__file__).resolve().parents[1] / "docs" / "stealable_spells.example.json"
        data = StealableData.load(example)
        assert len(data.spells) == 2
        assert data.is_stealable(999901) is True

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            StealableData.load(tmp_path / "does-not-exist.json")

    def test_malformed_json_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            StealableData.load(path)

    def test_missing_spells_key_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"not": "the right shape"}), encoding="utf-8")
        with pytest.raises(KeyError):
            StealableData.load(path)
