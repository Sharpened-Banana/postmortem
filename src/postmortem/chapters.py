"""Video chapter markers derived from an analyzed run report (WP-D2).

``mythic-analyzer record --analyze`` writes a JSON/HTML/text report the
moment a key ends (see ``cli.cmd_record``). This module turns that same
report into a *chapters sidecar* -- a list of labeled points/ranges along
the run's video, so a recording (from ``--on-run-start``/``--on-run-end``
shell hooks, native OBS via ``--obs``, or a manual recording the user
aligns afterward) can be scrubbed by pull/death/bloodlust/boss instead of
watched start to finish.

Two clocks, one offset
-----------------------
Every timestamp already in the report is relative to the run's own
*in-game log start* -- ``report["run"]["start_ts"]``, the absolute epoch
moment ``CHALLENGE_MODE_START`` was logged -- via a ``t`` (or, for pulls,
``t_start``/``t_end``) field giving seconds since that moment.

The *video's own start* is a different absolute epoch moment:
``RecordedRun.started_at``, ``time.time()`` captured when the recorder's
Python process first processed that same ``CHALLENGE_MODE_START`` line --
essentially simultaneous with when a shell hook or native OBS's
``StartRecord`` actually fired (see ``recorder.py``). Both are real POSIX
epoch seconds, so they're directly comparable:

    video_offset = (report["run"]["start_ts"] + event_t) - run.started_at

This can come out slightly negative for an event very close to run start
(the recorder's log-processing lagging the in-game log timestamp by a
beat, or ``--from-start`` picking up an already-in-progress run) -- see
:func:`video_offset`, which clamps to 0 rather than emitting a negative
video timestamp.

Output shape
------------
``<run>.chapters.json`` is a JSON list of objects, sorted by ``offset_s``
ascending::

    [
      {
        "offset_s": 12.3,     # seconds into the video (>= 0)
        "end_s": 45.0,        # real end offset if known (e.g. a pull's
                               # own t_end), else null for point events
        "label": "Pull 1: 2x Felwyrm, 1x Row Hooligan",
        "kind": "pull",       # run_start | pull | boss_pull | death | lust
        "pull": 1             # 1-based pull index this chapter is about,
                               # or null (run_start has none)
      },
      ...
    ]

This is deliberately simple/flat -- it's meant to be consumed
programmatically (a later work package cutting one video clip per
pull/death) as much as it's meant to be human-readable.

``<run>.vtt`` is the same chapters, rendered as WebVTT cues (``WEBVTT``
header, then ``HH:MM:SS.mmm --> HH:MM:SS.mmm`` / label blocks) --  many
players (including browsers) use VTT cues for chapter navigation.

Design note on "bosses"
------------------------
A boss pull is already one entry in ``report["pulls"]`` (labeled via its
``boss`` field) *and* ``report["encounters"]`` separately tracks the same
fight via WoW's own ENCOUNTER_START/END, with kill-vs-wipe and duration.
Rather than emit a second, near-duplicate chapter for the same moment,
:func:`build_chapters` emits one ``"boss_pull"`` chapter at the pull's own
``t_start`` (consistent with every other pull chapter) and, when a
same-named encounter can be matched, folds its kill/wipe verdict and
duration into that one chapter's label instead of adding another entry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# How many NPC types to name in a trash-pull summary before collapsing the
# rest into "+N more" -- keeps chapter labels short and scrubbable.
_PACK_SUMMARY_LIMIT = 4

# How close (in report-relative seconds) an encounters[] entry's start must
# be to a boss pull's t_start to be treated as "the same fight" rather than
# an unrelated same-named encounter (e.g. a wipe-and-retry far apart in the
# run). Generous on purpose: ENCOUNTER_START typically fires a few seconds
# before the pull's first recorded damage.
_ENCOUNTER_MATCH_WINDOW_S = 120.0

# Point-event (death/lust/run-start) cue length in the VTT output, when no
# real end offset is available and no following chapter forces something
# shorter.
_DEFAULT_POINT_DURATION_S = 3.0
_MIN_CUE_DURATION_S = 0.1


@dataclass
class Chapter:
    """One chapter/marker; see the module docstring for the JSON shape."""

    offset_s: float
    label: str
    kind: str  # "run_start" | "pull" | "boss_pull" | "death" | "lust"
    end_s: Optional[float] = None
    pull: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "offset_s": self.offset_s,
            "end_s": self.end_s,
            "label": self.label,
            "kind": self.kind,
            "pull": self.pull,
        }


def video_offset(report_start_ts: float, event_t: float, video_started_at: float) -> float:
    """Video-relative offset (seconds, clamped to >= 0) for a report event
    that is ``event_t`` seconds after the report's own ``start_ts``.

    See the module docstring for why these are two different clocks that
    are nonetheless directly comparable.
    """
    offset = (report_start_ts + event_t) - video_started_at
    return round(max(0.0, offset), 3)


def _pack_summary(npcs: Optional[list[dict[str, Any]]]) -> str:
    """Short "2x Felwyrm, 1x Row Hooligan" summary from a pull's per-NPC
    breakdown (``report["pulls"][i]["npcs"]``, already sorted by count
    descending -- see ``stats._pull_npcs``). Empty/missing -> ``""``."""
    if not npcs:
        return ""
    shown = npcs[:_PACK_SUMMARY_LIMIT]
    parts = []
    for entry in shown:
        name = entry.get("name") or "NPC {}".format(entry.get("npc_id"))
        parts.append(f"{entry.get('n', 1)}x {name}")
    summary = ", ".join(parts)
    extra = len(npcs) - len(shown)
    if extra > 0:
        summary += f", +{extra} more"
    return summary


def _match_encounter(
    encounters: list[dict[str, Any]], boss_name: str, pull_t_start: float
) -> Optional[dict[str, Any]]:
    """Find the ``report["encounters"]`` entry (if any) that's "the same
    fight" as a boss pull -- same name, nearest start time -- so the boss
    pull's chapter label can be enriched with kill/wipe + duration without
    emitting a second chapter for the same moment (see module docstring).
    """
    best: Optional[dict[str, Any]] = None
    best_diff: Optional[float] = None
    for enc in encounters:
        if enc.get("name") != boss_name:
            continue
        t = enc.get("t")
        if not isinstance(t, (int, float)):
            continue
        diff = abs(t - pull_t_start)
        if best_diff is None or diff < best_diff:
            best, best_diff = enc, diff
    if best is not None and best_diff is not None and best_diff <= _ENCOUNTER_MATCH_WINDOW_S:
        return best
    return None


def _boss_pull_label(boss_name: str, encounter: Optional[dict[str, Any]]) -> str:
    if encounter is None:
        return f"Boss: {boss_name}"
    verdict = "Kill" if encounter.get("kill") else "Wipe"
    duration = encounter.get("duration_s")
    if isinstance(duration, (int, float)):
        return f"Boss: {boss_name} ({verdict}, {duration:.0f}s)"
    return f"Boss: {boss_name} ({verdict})"


def build_chapters(report: dict[str, Any], video_started_at: float) -> list[Chapter]:
    """Build the chapter list for one analyzed run report.

    Tolerant by design: a report missing ``deaths``/``lust``/``encounters``
    (or missing individual fields on an entry) just yields fewer/plainer
    chapters, never raises. Only a report with no usable
    ``report["run"]["start_ts"]`` yields nothing at all, since every
    offset is computed from it.
    """
    run = report.get("run") or {}
    start_ts = run.get("start_ts")
    if not isinstance(start_ts, (int, float)):
        return []

    def offset(t: Any) -> Optional[float]:
        if not isinstance(t, (int, float)):
            return None
        return video_offset(start_ts, t, video_started_at)

    chapters: list[Chapter] = [
        Chapter(offset_s=offset(0.0) or 0.0, label="Run start", kind="run_start")
    ]

    encounters = [e for e in (report.get("encounters") or []) if isinstance(e, dict)]

    for pull in report.get("pulls") or []:
        off_start = offset(pull.get("t_start"))
        if off_start is None:
            continue
        off_end = offset(pull.get("t_end"))
        pull_index = pull.get("pull")
        boss = pull.get("boss")
        if boss:
            enc = _match_encounter(encounters, boss, pull.get("t_start"))
            label = _boss_pull_label(boss, enc)
            kind = "boss_pull"
        else:
            summary = _pack_summary(pull.get("npcs"))
            label = f"Pull {pull_index}: {summary}" if summary else f"Pull {pull_index}"
            kind = "pull"
        chapters.append(Chapter(
            offset_s=off_start, label=label, kind=kind, end_s=off_end, pull=pull_index,
        ))

    for death in report.get("deaths") or []:
        off = offset(death.get("t"))
        if off is None:
            continue
        player = death.get("player") or "Unknown"
        killing_blow = death.get("killing_blow") or {}
        spell = killing_blow.get("spell") if isinstance(killing_blow, dict) else None
        label = f"Death: {player} ({spell})" if spell else f"Death: {player}"
        chapters.append(Chapter(
            offset_s=off, label=label, kind="death", pull=death.get("pull"),
        ))

    for lust in report.get("lust") or []:
        off = offset(lust.get("t"))
        if off is None:
            continue
        spell = lust.get("spell") or "Bloodlust"
        source = lust.get("source")
        label = f"{spell} ({source})" if source else str(spell)
        chapters.append(Chapter(
            offset_s=off, label=label, kind="lust", pull=lust.get("pull"),
        ))

    chapters.sort(key=lambda c: c.offset_s)
    return chapters


def write_chapters_json(chapters: list[Chapter], path: Path) -> None:
    """Write ``<run>.chapters.json`` -- see the module docstring for the
    exact list-of-objects shape."""
    ordered = sorted(chapters, key=lambda c: c.offset_s)
    payload = [c.to_dict() for c in ordered]
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _format_vtt_timestamp(seconds: float) -> str:
    """``HH:MM:SS.mmm``, zero-padded per the WebVTT spec."""
    total_ms = round(max(0.0, seconds) * 1000)
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def write_vtt(chapters: list[Chapter], path: Path) -> None:
    """Write ``<run>.vtt`` -- valid WebVTT, one cue per chapter.

    A pull's own ``end_s`` (when known) is used as its cue's end; a
    point-event (run start/death/bloodlust, or a pull missing ``end_s``)
    gets a short fixed-length cue instead, further clipped so it never
    runs past the next chapter's start -- simple, and good enough for
    "every cue has a valid, non-zero-duration, non-badly-overlapping
    range" rather than trying to model real chapter boundaries exactly.
    """
    ordered = sorted(chapters, key=lambda c: c.offset_s)
    lines = ["WEBVTT", ""]
    for i, chapter in enumerate(ordered):
        start = chapter.offset_s
        end = chapter.end_s
        if end is None or end <= start:
            end = start + _DEFAULT_POINT_DURATION_S
        if i + 1 < len(ordered):
            next_start = ordered[i + 1].offset_s
            if next_start > start:
                end = min(end, next_start)
        if end - start < _MIN_CUE_DURATION_S:
            end = start + _MIN_CUE_DURATION_S
        lines.append(f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}")
        lines.append(chapter.label)
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_chapter_files(
    report: dict[str, Any], video_started_at: float, base: Path
) -> tuple[Path, Path]:
    """Build chapters for ``report`` and write both sidecars next to
    ``base`` (e.g. ``run.path.with_suffix("")``, matching the
    ``<base>.json``/``<base>.html`` naming already used for the other
    ``--analyze`` outputs). Returns ``(chapters_json_path, vtt_path)``.
    """
    chapters = build_chapters(report, video_started_at)
    json_path = Path(f"{base}.chapters.json")
    vtt_path = Path(f"{base}.vtt")
    write_chapters_json(chapters, json_path)
    write_vtt(chapters, vtt_path)
    return json_path, vtt_path
