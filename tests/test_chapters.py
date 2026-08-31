"""Chapters sidecar (WP-D2): video-relative offset math from a report's
in-game log start (report["run"]["start_ts"]) to the video's own start
(RecordedRun.started_at), chapter construction from a report dict, and
the .chapters.json / .vtt sidecar writers.
"""

from __future__ import annotations

import json
import re

import pytest

from conftest import build_run_log
from postmortem.analysis.run_analyzer import analyze_run
from postmortem.chapters import (
    build_chapters,
    video_offset,
    write_chapter_files,
    write_chapters_json,
    write_vtt,
)
from postmortem.combatlog.parser import parse_file
from postmortem.combatlog.segmenter import segment_runs
from postmortem.mdt.dungeon_data import DungeonDataStore

_VTT_TS_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3}) --> (\d{2}):(\d{2}):(\d{2})\.(\d{3})$"
)


def _hand_built_report() -> dict:
    """A small, fully hand-constructed report -- isolates the offset math
    and chapter labeling from run_analyzer's own construction (that's
    covered separately below via a real analyze_run() call)."""
    return {
        "run": {"start_ts": 1_000_000.0},
        "pulls": [
            {
                "pull": 1, "start_ts": 1_000_010.0, "end_ts": 1_000_040.0,
                "t_start": 10.0, "t_end": 40.0, "boss": None,
                "npcs": [
                    {"npc_id": 1, "name": "Felwyrm", "n": 2, "killed": 2},
                    {"npc_id": 2, "name": "Row Hooligan", "n": 1, "killed": 1},
                ],
            },
            {
                "pull": 2, "start_ts": 1_000_100.0, "end_ts": 1_000_130.0,
                "t_start": 100.0, "t_end": 130.0, "boss": "Big Boss",
                "npcs": [{"npc_id": 3, "name": "Big Boss", "n": 1, "killed": 1}],
            },
        ],
        "deaths": [
            {"t": 66.5, "player": "Bigheals-Area52", "pull": 1,
             "killing_blow": {"spell": "Dark Bolt"}},
        ],
        "lust": [
            {"t": 101.5, "pull": 2, "spell": "Bloodlust", "source": "Bigheals-Area52"},
        ],
        "encounters": [
            {"name": "Big Boss", "t": 98.0, "duration_s": 32.0, "kill": True},
        ],
    }


class TestVideoOffset:
    def test_video_started_with_log(self):
        assert video_offset(1000.0, 5.0, 1000.0) == 5.0

    def test_video_started_after_log(self):
        # video's own start lags the in-game log start by 3s -> offsets
        # shift back by that same 3s
        assert video_offset(1000.0, 5.0, 1003.0) == 2.0

    def test_video_started_before_log(self):
        # video started 3s before the in-game log start -> offsets shift
        # forward
        assert video_offset(1000.0, 5.0, 997.0) == 8.0

    def test_clamped_to_zero_not_negative(self):
        # event's absolute time (1000+2=1002) is before the video itself
        # started (1010) -- must clamp to 0, never go negative
        assert video_offset(1000.0, 2.0, 1010.0) == 0.0

    def test_exactly_at_video_start(self):
        assert video_offset(1000.0, 0.0, 1000.0) == 0.0


class TestBuildChapters:
    def test_offsets_including_clamp_case(self):
        report = _hand_built_report()
        # video started 20s into the in-game run -> every offset shifts
        # back by 20, and anything at or before t=20 clamps to 0
        chapters = build_chapters(report, video_started_at=1_000_020.0)
        by_kind: dict[str, list] = {}
        for c in chapters:
            by_kind.setdefault(c.kind, []).append(c)

        assert by_kind["run_start"][0].offset_s == 0.0  # t=0, clamped
        assert by_kind["pull"][0].offset_s == 0.0        # t_start=10, clamped
        assert by_kind["boss_pull"][0].offset_s == pytest.approx(80.0)  # 100-20
        assert by_kind["death"][0].offset_s == pytest.approx(46.5)      # 66.5-20
        assert by_kind["lust"][0].offset_s == pytest.approx(81.5)       # 101.5-20

    def test_offsets_no_shift(self):
        report = _hand_built_report()
        chapters = build_chapters(report, video_started_at=1_000_000.0)
        by_kind = {c.kind: c for c in chapters if c.kind != "pull"}
        assert by_kind["run_start"].offset_s == 0.0
        assert by_kind["boss_pull"].offset_s == pytest.approx(100.0)
        assert by_kind["death"].offset_s == pytest.approx(66.5)
        assert by_kind["lust"].offset_s == pytest.approx(101.5)

    def test_trash_pull_pack_summary_label(self):
        chapters = build_chapters(_hand_built_report(), video_started_at=1_000_000.0)
        pull1 = next(c for c in chapters if c.kind == "pull")
        assert "2x Felwyrm" in pull1.label
        assert "1x Row Hooligan" in pull1.label
        assert pull1.pull == 1
        assert pull1.end_s == pytest.approx(40.0)

    def test_boss_pull_label_folds_in_matching_encounter_no_duplicate(self):
        chapters = build_chapters(_hand_built_report(), video_started_at=1_000_000.0)
        boss_chapters = [c for c in chapters if c.kind == "boss_pull"]
        assert len(boss_chapters) == 1
        boss = boss_chapters[0]
        assert "Big Boss" in boss.label
        assert "Kill" in boss.label
        assert "32" in boss.label
        # offset comes from the pull's own t_start (100), not the
        # encounter's t (98) -- pulls are always anchored to t_start
        assert boss.offset_s == pytest.approx(100.0)
        # no separate "encounter"-kind chapter duplicating this one
        assert {c.kind for c in chapters} <= {
            "run_start", "pull", "boss_pull", "death", "lust"
        }

    def test_boss_pull_without_matching_encounter(self):
        report = _hand_built_report()
        report["encounters"] = []
        chapters = build_chapters(report, video_started_at=1_000_000.0)
        boss = next(c for c in chapters if c.kind == "boss_pull")
        assert boss.label == "Boss: Big Boss"

    def test_death_label_includes_killing_blow_spell(self):
        chapters = build_chapters(_hand_built_report(), video_started_at=1_000_000.0)
        death = next(c for c in chapters if c.kind == "death")
        assert "Bigheals-Area52" in death.label
        assert "Dark Bolt" in death.label
        assert death.pull == 1

    def test_lust_label(self):
        chapters = build_chapters(_hand_built_report(), video_started_at=1_000_000.0)
        lust = next(c for c in chapters if c.kind == "lust")
        assert "Bloodlust" in lust.label
        assert "Bigheals-Area52" in lust.label

    def test_sorted_ascending(self):
        chapters = build_chapters(_hand_built_report(), video_started_at=1_000_000.0)
        offsets = [c.offset_s for c in chapters]
        assert offsets == sorted(offsets)

    def test_no_deaths_or_lust_still_produces_chapters(self):
        report = _hand_built_report()
        report["deaths"] = []
        report["lust"] = []
        chapters = build_chapters(report, video_started_at=1_000_000.0)
        kinds = {c.kind for c in chapters}
        assert kinds == {"run_start", "pull", "boss_pull"}

    def test_missing_optional_sections_dont_crash(self):
        report = {"run": {"start_ts": 1000.0}}
        chapters = build_chapters(report, video_started_at=1000.0)
        assert len(chapters) == 1
        assert chapters[0].kind == "run_start"

    def test_no_start_ts_returns_empty_not_crash(self):
        assert build_chapters({}, video_started_at=1000.0) == []
        assert build_chapters({"run": {}}, video_started_at=1000.0) == []


class TestSidecarFiles:
    def test_chapters_json_shape_and_order(self, tmp_path):
        chapters = build_chapters(_hand_built_report(), video_started_at=1_000_000.0)
        path = tmp_path / "run.chapters.json"
        write_chapters_json(chapters, path)

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, list) and payload
        offsets = [c["offset_s"] for c in payload]
        assert offsets == sorted(offsets)
        for entry in payload:
            assert set(entry) == {"offset_s", "end_s", "label", "kind", "pull"}
            assert isinstance(entry["offset_s"], (int, float))
            assert isinstance(entry["label"], str) and entry["label"]
            assert isinstance(entry["kind"], str)
            assert entry["offset_s"] >= 0

    def test_vtt_starts_with_webvtt_and_has_valid_cues(self, tmp_path):
        chapters = build_chapters(_hand_built_report(), video_started_at=1_000_000.0)
        path = tmp_path / "run.vtt"
        write_vtt(chapters, path)

        text = path.read_text(encoding="utf-8")
        assert text.startswith("WEBVTT\n\n")

        lines = text.splitlines()
        cue_lines = [l for l in lines if "-->" in l]
        assert len(cue_lines) == len(chapters)
        for line in cue_lines:
            m = _VTT_TS_RE.match(line)
            assert m, f"malformed cue timestamp line: {line!r}"
            start = (
                int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                + int(m.group(4)) / 1000
            )
            end = (
                int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7))
                + int(m.group(8)) / 1000
            )
            assert end > start  # never zero/negative duration

        # each cue timestamp line is immediately followed by a label line,
        # then a blank line, per the WebVTT cue block shape
        for i, line in enumerate(lines):
            if "-->" in line:
                assert lines[i + 1].strip() != ""
                assert lines[i + 2] == ""

    def test_no_deaths_or_lust_still_valid_sparser_output(self, tmp_path):
        report = {
            "run": {"start_ts": 1000.0},
            "pulls": [{
                "pull": 1, "t_start": 5.0, "t_end": 20.0, "boss": None, "npcs": [],
            }],
            "deaths": [], "lust": [], "encounters": [],
        }
        chapters = build_chapters(report, video_started_at=1000.0)
        assert len(chapters) == 2  # run_start + the one pull

        json_path = tmp_path / "x.chapters.json"
        vtt_path = tmp_path / "x.vtt"
        write_chapters_json(chapters, json_path)
        write_vtt(chapters, vtt_path)
        assert json.loads(json_path.read_text(encoding="utf-8"))
        assert vtt_path.read_text(encoding="utf-8").startswith("WEBVTT")

    def test_write_chapter_files_names_and_writes_both(self, tmp_path):
        report = _hand_built_report()
        base = tmp_path / "20260830-200000_MurderRow_10"
        json_path, vtt_path = write_chapter_files(report, 1_000_000.0, base)
        assert json_path == tmp_path / "20260830-200000_MurderRow_10.chapters.json"
        assert vtt_path == tmp_path / "20260830-200000_MurderRow_10.vtt"
        assert json_path.exists()
        assert vtt_path.exists()


class TestRealAnalyzedReport:
    """Cross-check against a real analyze_run() report (not hand-built),
    to confirm the exact field names this module reads (t_start/t_end,
    boss, npcs, killing_blow.spell, encounters[].t/kill/duration_s, ...)
    really match run_analyzer's current output shape.
    """

    def test_real_report_produces_expected_chapters(self, dungeon_data_file, tmp_path):
        log_path = tmp_path / "WoWCombatLog.txt"
        log_path.write_text(build_run_log().text(), encoding="utf-8")
        store = DungeonDataStore.load(str(dungeon_data_file))
        (segment,) = list(segment_runs(parse_file(log_path)))
        report = analyze_run(segment, store=store)

        # video started exactly when the in-game log started -> offsets
        # should equal the report's own relative "t"/"t_start" values
        video_started_at = report["run"]["start_ts"]
        chapters = build_chapters(report, video_started_at)

        kinds = [c.kind for c in chapters]
        assert kinds.count("run_start") == 1
        assert kinds.count("pull") == 2       # the two trash pulls
        assert kinds.count("boss_pull") == 1  # the boss pull
        assert kinds.count("death") == 1
        assert kinds.count("lust") == 1
        assert [c.offset_s for c in chapters] == sorted(c.offset_s for c in chapters)

        boss = next(c for c in chapters if c.kind == "boss_pull")
        assert boss.label == "Boss: Big Boss (Kill, 30s)"
        assert boss.offset_s == pytest.approx(report["pulls"][2]["t_start"])

        death = next(c for c in chapters if c.kind == "death")
        assert "Bigheals-Area52" in death.label
        assert "Dark Bolt" in death.label
        assert death.offset_s == pytest.approx(report["deaths"][0]["t"])

        lust = next(c for c in chapters if c.kind == "lust")
        assert lust.offset_s == pytest.approx(report["lust"][0]["t"])

        pull1 = next(c for c in chapters if c.kind == "pull" and c.pull == 1)
        assert "2x Felwyrm" in pull1.label
        assert "1x Duskblade" in pull1.label
