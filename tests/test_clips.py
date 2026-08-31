"""Per-pull/death video clip cutter (WP-D3): ffmpeg argv construction
(including the padding math and the -ss/-t choice over -ss/-to), the
chapters-sidecar-preferred / recompute-as-fallback resolution, a clean
ffmpeg-missing failure, and an end-to-end fake-ffmpeg-on-PATH smoke
test.

No test here needs a real ffmpeg installation or a real video file:
ffmpeg's argv is either inspected directly (build_ffmpeg_command) or
recorded by a small fake shell-script stand-in placed on PATH within
each test's own tmp_path.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path

import pytest

from mythic_analyzer.chapters import build_chapters
from mythic_analyzer.cli import main
from mythic_analyzer.clips import (
    ClipSpec,
    FfmpegNotFoundError,
    build_ffmpeg_command,
    clip_specs_for_chapters,
    cut_clips,
    load_chapters,
)


def _report(start_ts: float = 1_000_000.0) -> dict:
    """Small hand-built report: one trash pull, one boss pull, one death,
    one bloodlust -- enough to exercise every chapter kind clips.py cares
    about (pull/boss_pull/death) and the two it must skip (run_start/lust)."""
    return {
        "run": {"start_ts": start_ts, "zone": "Murder Row", "keystone_level": 10},
        "pulls": [
            {"pull": 1, "t_start": 10.0, "t_end": 40.0, "boss": None,
             "npcs": [{"npc_id": 1, "name": "Felwyrm", "n": 2, "killed": 2}]},
            {"pull": 2, "t_start": 100.0, "t_end": 130.0, "boss": "Big Boss",
             "npcs": [{"npc_id": 2, "name": "Big Boss", "n": 1, "killed": 1}]},
        ],
        "deaths": [
            {"t": 66.5, "player": "Bigheals-Area52", "pull": 1,
             "killing_blow": {"spell": "Dark Bolt"}},
        ],
        "lust": [{"t": 101.5, "pull": 2, "spell": "Bloodlust", "source": "X"}],
        "encounters": [],
    }


def _chapters_for(report: dict) -> list[dict]:
    """Chapters assuming the video starts exactly at run start (offset
    shift 0) -- i.e. what load_chapters' fallback path would compute."""
    return [c.to_dict() for c in
            build_chapters(report, video_started_at=report["run"]["start_ts"])]


class TestBuildFfmpegCommand:
    """Argument math -- pure, no subprocess involved."""

    def test_basic_shape_and_flag_order(self):
        cmd = build_ffmpeg_command(Path("in.mp4"), 5.0, 20.0, Path("out.mp4"))
        assert cmd[0] == "ffmpeg"
        ss_idx = cmd.index("-ss")
        i_idx = cmd.index("-i")
        assert ss_idx < i_idx  # input-side seek: -ss before -i
        assert cmd[ss_idx + 1] == "5.000"
        assert cmd[i_idx + 1] == "in.mp4"

        t_idx = cmd.index("-t")
        assert cmd[t_idx + 1] == "15.000"  # duration (20 - 5), not absolute end
        assert "-to" not in cmd  # the ambiguous flag is never used

        c_idx = cmd.index("-c")
        assert cmd[c_idx + 1] == "copy"
        assert cmd[-1] == "out.mp4"

    def test_duration_math(self):
        cmd = build_ffmpeg_command(Path("v.mp4"), 12.5, 30.25, Path("o.mp4"))
        assert cmd[cmd.index("-t") + 1] == "17.750"

    def test_zero_start(self):
        cmd = build_ffmpeg_command(Path("v.mp4"), 0.0, 6.0, Path("o.mp4"))
        assert cmd[cmd.index("-ss") + 1] == "0.000"
        assert cmd[cmd.index("-t") + 1] == "6.000"

    def test_never_produces_negative_duration(self):
        cmd = build_ffmpeg_command(Path("v.mp4"), 10.0, 5.0, Path("o.mp4"))
        assert cmd[cmd.index("-t") + 1] == "0.000"


class TestClipSpecsForChapters:
    """Which chapters become clips, and the exact start/end window math
    (including padding and the start-clamped-to-zero case)."""

    def test_pull_window_is_offset_to_end_padded(self):
        specs = clip_specs_for_chapters(_chapters_for(_report()), Path("/out"), pad=3.0)
        pull1 = next(s for s in specs if s.kind == "pull")
        assert pull1.start_s == pytest.approx(10.0 - 3.0)
        assert pull1.end_s == pytest.approx(40.0 + 3.0)

    def test_boss_pull_window_is_offset_to_end_padded(self):
        specs = clip_specs_for_chapters(_chapters_for(_report()), Path("/out"), pad=3.0)
        boss = next(s for s in specs if s.kind == "boss_pull")
        assert boss.start_s == pytest.approx(100.0 - 3.0)
        assert boss.end_s == pytest.approx(130.0 + 3.0)

    def test_death_window_is_point_plus_minus_pad(self):
        specs = clip_specs_for_chapters(_chapters_for(_report()), Path("/out"), pad=3.0)
        death = next(s for s in specs if s.kind == "death")
        assert death.start_s == pytest.approx(66.5 - 3.0)
        assert death.end_s == pytest.approx(66.5 + 3.0)

    def test_pull_start_clamped_to_zero_near_run_start(self):
        chapters = [
            {"offset_s": 1.0, "end_s": 20.0, "label": "Pull 1", "kind": "pull", "pull": 1},
        ]
        specs = clip_specs_for_chapters(chapters, Path("/out"), pad=3.0)
        assert specs[0].start_s == 0.0  # 1.0 - 3.0 would be negative
        assert specs[0].end_s == pytest.approx(23.0)  # end is never clamped

    def test_death_start_clamped_to_zero_near_run_start(self):
        chapters = [
            {"offset_s": 1.5, "end_s": None, "label": "Death: X", "kind": "death", "pull": 1},
        ]
        specs = clip_specs_for_chapters(chapters, Path("/out"), pad=3.0)
        assert specs[0].start_s == 0.0
        assert specs[0].end_s == pytest.approx(4.5)

    def test_run_start_and_lust_are_skipped(self):
        specs = clip_specs_for_chapters(_chapters_for(_report()), Path("/out"))
        kinds = {s.kind for s in specs}
        assert kinds == {"pull", "boss_pull", "death"}

    def test_default_pad_is_three_seconds(self):
        chapters = [
            {"offset_s": 10.0, "end_s": None, "label": "Death: X", "kind": "death", "pull": 1},
        ]
        specs = clip_specs_for_chapters(chapters, Path("/out"))
        assert specs[0].start_s == pytest.approx(7.0)
        assert specs[0].end_s == pytest.approx(13.0)

    def test_numbered_chronologically_within_kind(self):
        specs = clip_specs_for_chapters(_chapters_for(_report()), Path("/out"), pad=3.0)
        pull_names = [s.out_path.name for s in specs if s.kind in ("pull", "boss_pull")]
        assert pull_names[0].startswith("pull01_")
        assert pull_names[1].startswith("pull02_")
        death_names = [s.out_path.name for s in specs if s.kind == "death"]
        assert death_names[0].startswith("death01_")
        assert all(s.out_path.parent == Path("/out") for s in specs)

    def test_chronological_order_across_kinds(self):
        # pull(10) < death(66.5) < boss_pull(100) -- specs list itself
        # (not just numbering) should follow chapter offset order.
        specs = clip_specs_for_chapters(_chapters_for(_report()), Path("/out"), pad=3.0)
        assert [s.kind for s in specs] == ["pull", "death", "boss_pull"]


class TestLoadChapters:
    """Chapters-sidecar preference vs. build_chapters() fallback."""

    def test_prefers_sidecar_when_present(self, tmp_path):
        report = _report()
        report_path = tmp_path / "20260828-foo.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")

        # Deliberately different offsets from what a fallback recompute
        # would produce, so the test can tell which path was taken.
        sidecar_path = tmp_path / "20260828-foo.chapters.json"
        custom_chapters = [
            {"offset_s": 999.0, "end_s": 1010.0, "label": "Pull 1",
             "kind": "pull", "pull": 1},
        ]
        sidecar_path.write_text(json.dumps(custom_chapters), encoding="utf-8")

        chapters = load_chapters(report_path, report)
        assert chapters == custom_chapters

    def test_falls_back_to_build_chapters_when_no_sidecar(self, tmp_path):
        report = _report()
        report_path = tmp_path / "20260828-bar.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        # no .chapters.json sidecar written next to it

        chapters = load_chapters(report_path, report)
        assert chapters == _chapters_for(report)
        pull1 = next(c for c in chapters if c["kind"] == "pull")
        # video assumed to start exactly at run start -> offset == t_start
        assert pull1["offset_s"] == pytest.approx(10.0)

    def test_malformed_sidecar_falls_back_instead_of_crashing(self, tmp_path):
        report = _report()
        report_path = tmp_path / "20260828-baz.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        sidecar_path = tmp_path / "20260828-baz.chapters.json"
        sidecar_path.write_text("{not valid json", encoding="utf-8")

        chapters = load_chapters(report_path, report)
        assert chapters == _chapters_for(report)


class TestFfmpegMissing:
    """A missing ffmpeg must be a clean, message-bearing failure -- never
    a raw subprocess.FileNotFoundError traceback."""

    def test_cut_clips_raises_ffmpeg_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr("mythic_analyzer.clips.shutil.which", lambda name: None)
        specs = [ClipSpec(0.0, 10.0, "pull", "Pull 1", tmp_path / "pull01.mp4")]
        with pytest.raises(FfmpegNotFoundError):
            cut_clips(tmp_path / "video.mp4", specs)

    def test_cli_clean_systemexit_when_ffmpeg_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr("mythic_analyzer.cli.shutil.which", lambda name: None)
        report_path = tmp_path / "run.json"
        report_path.write_text(json.dumps(_report()), encoding="utf-8")
        video_path = tmp_path / "video.mp4"
        video_path.write_text("not a real video", encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            main(["clips", str(video_path), str(report_path)])
        assert "ffmpeg" in str(excinfo.value).lower()


class TestFakeFfmpegEndToEnd:
    """A real (fake) executable on PATH -- proves cmd_clips actually
    shells out to `ffmpeg` with the arguments expected, with no real
    video processing and no real ffmpeg binary involved."""

    @pytest.fixture()
    def fake_ffmpeg_log(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        log_path = tmp_path / "ffmpeg_invocations.log"
        script = bin_dir / "ffmpeg"
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{log_path}"\n'
            "exit 0\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        return log_path

    def test_cmd_clips_invokes_fake_ffmpeg_with_expected_args(self, fake_ffmpeg_log, tmp_path):
        report = _report()
        report_path = tmp_path / "20260828-run.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        video_path = tmp_path / "video.mp4"
        video_path.write_text("fake video bytes", encoding="utf-8")
        out_dir = tmp_path / "out"

        rc = main([
            "clips", str(video_path), str(report_path),
            "--out", str(out_dir), "--pad", "2",
        ])
        assert rc == 0
        assert out_dir.is_dir()

        log_lines = [ln for ln in fake_ffmpeg_log.read_text().splitlines() if ln.strip()]
        # 1 trash pull + 1 boss pull + 1 death == 3 invocations (run_start/
        # lust are not clipped)
        assert len(log_lines) == 3

        invocations = [shlex.split(line) for line in log_lines]

        for argv in invocations:
            assert "-ss" in argv and "-i" in argv
            assert argv.index("-ss") < argv.index("-i")
            assert str(video_path) in argv
            assert "-t" in argv
            assert "-to" not in argv
            assert "-c" in argv and "copy" in argv[argv.index("-c") + 1]

        # chronological order: pull(10) -> death(66.5) -> boss_pull(100)
        pull_argv, death_argv, boss_argv = invocations

        def val(argv, flag):
            return float(argv[argv.index(flag) + 1])

        assert val(pull_argv, "-ss") == pytest.approx(8.0)    # 10 - 2
        assert val(pull_argv, "-t") == pytest.approx(34.0)    # (40+2)-(10-2)
        assert pull_argv[-1].endswith(".mp4")
        assert Path(pull_argv[-1]).name.startswith("pull01_")

        assert val(death_argv, "-ss") == pytest.approx(64.5)  # 66.5 - 2
        assert val(death_argv, "-t") == pytest.approx(4.0)    # 2*pad
        assert Path(death_argv[-1]).name.startswith("death01_")

        assert val(boss_argv, "-ss") == pytest.approx(98.0)   # 100 - 2
        assert val(boss_argv, "-t") == pytest.approx(34.0)    # (130+2)-(100-2)
        assert Path(boss_argv[-1]).name.startswith("pull02_")
