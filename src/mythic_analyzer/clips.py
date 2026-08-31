"""Per-pull/death video clip cutter (WP-D3).

Turns a chapters list (see :mod:`mythic_analyzer.chapters`) into one
lossless ``ffmpeg`` clip per ``pull``/``boss_pull`` chapter (the real
pull duration, padded on both ends) and one per ``death`` chapter (a
padded window around that point event). ``run_start``/``lust`` chapters
aren't clipped.

Where the chapter/offset data comes from
-----------------------------------------
``clips`` is invoked as ``clips VIDEO REPORT_JSON`` -- no explicit
chapters file -- but cutting needs the *video-relative* offsets that
:mod:`mythic_analyzer.chapters` already knows how to compute (a video's
first frame doesn't necessarily line up with
``report["run"]["start_ts"]``; see that module's docstring for why).
:func:`load_chapters` resolves this the same way a real user's workflow
naturally does:

1. Prefer an existing ``<report_basename>.chapters.json`` sidecar next
   to the report (the naming convention
   :func:`mythic_analyzer.chapters.write_chapter_files` already
   produces) -- the common case, since it already carries the real
   recorder wall-clock reference from when the video was recorded.
2. Otherwise, fall back to recomputing chapters assuming the video's
   first frame *is* the moment the run started
   (``video_started_at=report["run"]["start_ts"]``) -- the only
   reasonable assumption when no sidecar exists (e.g. a report from
   plain ``analyze`` rather than ``record --analyze``, or a video the
   user manually trimmed to start exactly at run start).

Cutting: ``-ss``/``-t``, not ``-ss``/``-to``
---------------------------------------------
Fast, lossless cuts need an input-side ``-ss`` (before ``-i``, so ffmpeg
seeks to the nearest keyframe instead of decoding from the start) and
``-c copy`` (no re-encode). The natural-looking way to express an
absolute end time alongside that would be an output-side ``-to``, but
that combination is a well-documented ffmpeg gotcha: whether ``-to`` is
read as an absolute timestamp on the original (pre-seek) input timeline
or as relative to the seek point depends on the ffmpeg version and
exactly how the seek was expressed.

This was verified empirically in this environment (ffmpeg 8.1.2):

    ffmpeg -ss 5 -i in.mp4 -to 12 out.mp4   # re-encode (no -c copy)

produced a clip **12 seconds long**, not the 7-second clip a naive
"absolute stop at 12s, started at 5s" reading would predict -- i.e.
``-to`` behaved like a *duration measured from the seek point* here,
not an absolute stop time. That's exactly the ambiguity to avoid.

``-t <duration>`` has no such ambiguity: it always means "how long the
output should be, starting from wherever it starts" regardless of
ffmpeg version or how the seek was applied. So every cut here is
expressed as ``-ss <start> -i <video> -t <end - start> -c copy``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .chapters import build_chapters

# Matches the plan doc's own example invocation (`clips ... [--pad 3]`).
DEFAULT_PAD_S = 3.0

# Chapter kinds that become a clip; "pull"/"boss_pull" use their own
# start/end, "death" (handled separately below) is a point event.
_PULL_KINDS = {"pull", "boss_pull"}

_SLUG_MAX_LEN = 40


class FfmpegNotFoundError(RuntimeError):
    """Raised when ``ffmpeg`` isn't on PATH; the CLI turns this into a
    clean ``SystemExit`` rather than letting a subprocess.FileNotFoundError
    surface as a raw traceback."""


@dataclass
class ClipSpec:
    """One planned clip: the video-relative ``[start_s, end_s)`` window
    to cut, which chapter it came from, and where to write it."""

    start_s: float
    end_s: float
    kind: str
    label: str
    out_path: Path


def _slugify(label: str, limit: int = _SLUG_MAX_LEN) -> str:
    """Short, filesystem-safe fragment of a chapter label, for output
    filenames (e.g. "Pull 1: 2x Felwyrm" -> "pull-1-2x-felwyrm")."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower()
    return slug[:limit].strip("-") or "clip"


def sidecar_path_for(report_json_path: Path) -> Path:
    """The ``<report_basename>.chapters.json`` sidecar path for a given
    report JSON path -- matches the naming
    :func:`mythic_analyzer.chapters.write_chapter_files` already uses
    (``<base>.json`` alongside ``<base>.chapters.json``)."""
    base = report_json_path.with_suffix("")
    return Path(f"{base}.chapters.json")


def load_chapters(report_json_path: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the chapter list to cut clips from -- see the module
    docstring for the sidecar-preferred / recompute-as-fallback logic.

    Returns a list of chapter dicts in the same shape
    ``write_chapters_json`` writes (``offset_s``/``end_s``/``label``/
    ``kind``/``pull``), whether they came from the sidecar or were just
    recomputed.
    """
    sidecar = sidecar_path_for(report_json_path)
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, list):
            return data
        # malformed/unexpected sidecar contents: fall through and
        # recompute rather than crash on a file that happens to exist.

    run = report.get("run") or {}
    start_ts = run.get("start_ts")
    if not isinstance(start_ts, (int, float)):
        return []
    chapters = build_chapters(report, video_started_at=start_ts)
    return [c.to_dict() for c in chapters]


def clip_specs_for_chapters(
    chapters: list[dict[str, Any]], out_dir: Path, pad: float = DEFAULT_PAD_S,
) -> list[ClipSpec]:
    """Turn a chapters list into the ordered list of clips to cut.

    - Every ``pull``/``boss_pull`` chapter -> ``[offset_s - pad, end_s +
      pad]`` (the real pull duration, padded both ends; ``end_s`` falls
      back to ``offset_s`` if somehow missing, so a pull with no known
      end still yields a small clip rather than being skipped).
    - Every ``death`` chapter -> ``[offset_s - pad, offset_s + pad]``
      (deaths are point events -- no ``end_s``).
    - ``run_start``/``lust`` chapters are skipped: not pull/death clips.
    - Every window's start is clamped to >= 0. The end is never clamped
      against video length (we don't have it without an extra
      ``ffprobe`` call, out of scope here) -- ffmpeg handles a `-t`
      duration that runs past end-of-file gracefully on its own.

    Output filenames are numbered chronologically within each kind:
    ``pull01_<slug>.mp4``, ``pull02_<slug>.mp4``, ``death01_<slug>.mp4``,
    ... -- collision-free within one run of this command.
    """
    ordered = sorted(chapters, key=lambda c: c.get("offset_s", 0.0))
    specs: list[ClipSpec] = []
    pull_n = 0
    death_n = 0
    for chapter in ordered:
        kind = chapter.get("kind")
        offset = chapter.get("offset_s")
        if not isinstance(offset, (int, float)):
            continue
        label = chapter.get("label") or kind or "clip"

        if kind in _PULL_KINDS:
            pull_n += 1
            end = chapter.get("end_s")
            if not isinstance(end, (int, float)):
                end = offset
            start_s = max(0.0, offset - pad)
            end_s = end + pad
            name = f"pull{pull_n:02d}_{_slugify(label)}.mp4"
            specs.append(ClipSpec(start_s, end_s, kind, label, out_dir / name))
        elif kind == "death":
            death_n += 1
            start_s = max(0.0, offset - pad)
            end_s = offset + pad
            name = f"death{death_n:02d}_{_slugify(label)}.mp4"
            specs.append(ClipSpec(start_s, end_s, kind, label, out_dir / name))
        # else: run_start / lust / anything else -- not a clip kind.

    return specs


def build_ffmpeg_command(video: Path, start_s: float, end_s: float, out_path: Path) -> list[str]:
    """The argv for cutting ``[start_s, end_s)`` out of ``video`` into
    ``out_path``, losslessly (``-c copy``, no re-encode).

    ``-ss`` goes before ``-i`` (fast, keyframe-based *input* seeking --
    what makes ``-c copy`` cheap). The end is expressed as ``-t
    <duration>`` rather than ``-to <absolute end>`` -- see the module
    docstring for the empirically-verified reason. ``-y`` overwrites a
    same-named clip from a previous run of this command without
    prompting (re-running `clips` against the same video/report/--out
    is expected to just regenerate the same deterministic filenames).
    """
    duration = max(0.0, end_s - start_s)
    return [
        "ffmpeg", "-y",
        "-ss", f"{start_s:.3f}",
        "-i", str(video),
        "-t", f"{duration:.3f}",
        "-c", "copy",
        str(out_path),
    ]


def cut_clips(
    video: Path,
    specs: list[ClipSpec],
    *,
    ffmpeg_path: Optional[str] = None,
    runner: Callable[..., Any] = subprocess.run,
) -> list[Path]:
    """Cut every planned clip in ``specs``, in order, via ``runner``
    (defaults to :func:`subprocess.run`; swappable in tests for a fake
    ffmpeg that just records its invocation).

    Raises :class:`FfmpegNotFoundError` if ``ffmpeg`` isn't on PATH
    (when ``ffmpeg_path`` isn't given explicitly) -- callers (the CLI)
    are expected to turn that into a clean, non-crashing error message
    rather than let a raw ``FileNotFoundError`` from ``subprocess``
    propagate.
    """
    resolved = ffmpeg_path or shutil.which("ffmpeg")
    if not resolved:
        raise FfmpegNotFoundError("ffmpeg not found on PATH")

    written: list[Path] = []
    for spec in specs:
        spec.out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = build_ffmpeg_command(video, spec.start_s, spec.end_s, spec.out_path)
        cmd[0] = resolved
        runner(cmd, check=True, capture_output=True)
        written.append(spec.out_path)
    return written
