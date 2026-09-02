"""Live recording mode: tail WoWCombatLog.txt while you play.

Watches the log, detects CHALLENGE_MODE_START/END, saves each run's raw
log slice to its own file, prints live status (pull kills, deaths, forces)
and can auto-analyze the run the moment the key ends.

Shell hooks fire on run start/end (``--on-run-start`` / ``--on-run-end``),
with MA_ZONE, MA_LEVEL and MA_PATH in the environment — point them at
anything, most usefully video capture, e.g. with obs-cmd (OBS WebSocket):

    postmortem record ... \\
        --on-run-start "obs-cmd recording start" \\
        --on-run-end   "obs-cmd recording stop"

so every key gets its own video alongside the log slice and reports.

As of WP-D1, OBS can also be driven natively (no third-party ``obs-cmd``
needed) via ``--obs [ws://host:port]`` (default ``ws://127.0.0.1:4455``
when passed with no value), ``--obs-password`` and
``--obs-replay-on-death`` (saves the replay buffer on every detected
player death) -- see :mod:`postmortem.obsws`. If a shell hook is
*also* configured for a given event (start/end), the shell hook takes
precedence for that event and the native client is not additionally
invoked, to avoid double-triggering OBS; ``--obs-replay-on-death`` always
uses the native client when ``--obs`` is set, since there's no
equivalent shell hook to conflict with. Any native-OBS failure (can't
connect, bad auth, a request errors) is caught and reported as a
warning; the log-slice recording itself is never interrupted by it.

WoW only writes the combat log when logging is enabled — either
`/combatlog` or the "advanced combat logging" checkbox (Options → Network).
Advanced combat logging is strongly recommended: it adds positions and HP
to events, which improves death recaps and pull mapping.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .combatlog.parser import parse_line

_START_RE = re.compile(r"  CHALLENGE_MODE_START,")
_END_RE = re.compile(r"  CHALLENGE_MODE_END,")
_DEATH_RE = re.compile(r"  UNIT_DIED,")

# CHALLENGE_MODE_START,"<zone>",<instance_id>,<challenge_map_id>,<keystone_level>,[<affixes>]
_START_FIELDS_RE = re.compile(
    r'CHALLENGE_MODE_START,"([^"]*)",([^,]*),([^,]*),([^,]*)'
)
# CHALLENGE_MODE_END,<instance_id>,<timed 0|1>,<level>,<totalTimeMs>,...
_END_FIELDS_RE = re.compile(r"CHALLENGE_MODE_END,([^,]*),([^,]*),([^,]*),([^,\s]*)")


def _to_int(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    try:
        return int(text.strip())
    except (ValueError, AttributeError):
        return None


def _parse_start_key(line: str) -> tuple[Optional[int], Optional[int]]:
    """``(challenge_map_id, keystone_level)`` for a CHALLENGE_MODE_START
    line -- the pair segment_runs() uses to tell a mid-key ``/reload``
    (the same key re-logging its own start) apart from a genuinely
    different key starting."""
    m = _START_FIELDS_RE.search(line)
    if not m:
        return None, None
    return _to_int(m.group(3)), _to_int(m.group(4))


def _start_identity(line: str) -> tuple[Optional[str], Optional[float]]:
    """``(zone, start_ts)`` for a CHALLENGE_MODE_START line -- the same
    identity the run history de-duplicates on (``Store.ingest``: zone +
    ``report["run"]["start_ts"]``). The timestamp goes through the real
    combat-log parser so it's bit-identical to what analysis produces."""
    m = _START_FIELDS_RE.search(line)
    if not m:
        return None, None
    event = parse_line(line)
    if event is None:
        return None, None
    return m.group(1), event.ts


def _is_phantom_end(line: str) -> bool:
    """Whether a CHALLENGE_MODE_END line is WoW's all-zeroed *phantom*
    end rather than a real one.

    Some client versions fire ``...END,<id>,0,0,0,0.000000,0.000000``
    immediately before every real CHALLENGE_MODE_START -- totalTimeMs
    (field 4) == 0 is its unambiguous signature, since a run that
    reaches a real end always ran for nonzero time. See segment_runs()'s
    own comment for the real-log confirmation behind this. A phantom on
    an open run means that run is being abandoned, NOT completed, so it
    must not close the run as a (false) success.

    Anything unparseable is treated as a real end -- conservative, and
    matches the behavior this had before phantoms were handled here.
    """
    m = _END_FIELDS_RE.search(line)
    if not m:
        return False
    return _to_int(m.group(4)) == 0


def newest_combat_log(folder: str | Path) -> Optional[Path]:
    """The most recently modified ``WoWCombatLog*.txt`` in ``folder`` --
    the one WoW is writing to right now, whatever it's named -- or None.

    WoW does not reliably reuse a stable ``WoWCombatLog.txt``: many
    installs start a fresh ``WoWCombatLog-<session stamp>.txt`` every
    time the client launches. The active log is always the newest by
    mtime, since only an open log keeps being touched.
    """
    try:
        candidates = list(Path(folder).glob("WoWCombatLog*.txt"))
    except OSError:
        return None
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


@dataclass
class RecordedRun:
    path: Path
    zone: str
    keystone_level: Optional[int]
    started_at: float
    line_count: int = 0
    player_deaths: int = 0
    completed: bool = False
    obs_output_path: Optional[str] = None  # from OBS's StopRecord, if native OBS was used
    # Kept only so _feed() can tell a mid-key /reload (same key re-logging
    # its start) apart from a different key starting -- see its own
    # comment, and segment_runs()'s matching same_key check.
    challenge_map_id: Optional[int] = None


@dataclass
class Recorder:
    log_path: Path
    out_dir: Path
    from_start: bool = False
    poll_interval: float = 0.5
    on_run_complete: Optional[Callable[[RecordedRun], None]] = None
    # Fired the moment a key's CHALLENGE_MODE_START is seen / the moment an
    # open run is closed *without* a real end (a different key started, or
    # WoW's phantom end). on_run_complete alone left a live UI silent for
    # the whole 20-30 minutes of a key -- nothing at all between "Watching
    # <path>" and "complete", which reads as "not working" (real report,
    # 2026-09-02). Both best-effort; a raising callback never stops the
    # recording itself.
    on_run_start: Optional[Callable[[RecordedRun], None]] = None
    on_run_abandoned: Optional[Callable[[RecordedRun], None]] = None
    # Follow WoW across log files. Many installs start a brand-new
    # ``WoWCombatLog-<stamp>.txt`` every time the client launches, and a
    # watch that stays on the file it opened goes quietly blind the moment
    # the player restarts the game -- a real, timed key was lost exactly
    # that way (2026-09-02). While idle, the newest sibling log is checked
    # every ``rotation_check_s``; a newer one means WoW restarted: any run
    # still open can never get its end (reported abandoned), the new file
    # is read from its start (everything in it is this session's), and
    # ``on_log_switched`` fires so a UI can say so.
    follow_rotation: bool = True
    rotation_check_s: float = 5.0
    on_log_switched: Optional[Callable[[Path], None]] = None
    # Catch-up: when given, every COMPLETED run already sitting in the log
    # at open time is replayed through the normal pipeline (slice +
    # on_run_complete) unless ``already_processed(zone, start_ts)`` says it
    # was handled before -- the desktop app answers that from its run
    # history. None (the CLI) keeps the old "only tail what's new" start.
    already_processed: Optional[Callable[[str, float], bool]] = None
    on_start_cmd: Optional[str] = None  # shell hook (e.g. start OBS recording)
    on_end_cmd: Optional[str] = None    # shell hook (e.g. stop OBS recording)
    obs_url: Optional[str] = None       # e.g. ws://127.0.0.1:4455 -- native OBS control
    obs_password: Optional[str] = None
    obs_replay_on_death: bool = False
    echo: Callable[[str], None] = print
    # Called once, right before the wait loop below begins, if log_path
    # doesn't exist yet when watch() starts -- lets a caller (the desktop
    # app) show a dedicated "waiting for your first key" status instead of
    # a flat "watching" that would otherwise look identical to actively
    # tailing a real file. CLI usage doesn't need this (the echo() message
    # below already covers it); left None there.
    on_waiting_for_log: Optional[Callable[[], None]] = None
    _current: Optional[RecordedRun] = None
    _out_fh: Optional[object] = field(default=None, repr=False)
    _obs: Optional[object] = field(default=None, repr=False)  # live OBSClient for this run
    _stop_requested: bool = field(default=False, repr=False)

    def request_stop(self) -> None:
        """Ask a running ``watch()`` loop to stop after its current poll
        tick. Cooperative, not a hard interrupt -- checked once per
        iteration, so stopping can lag by up to ``poll_interval``. Exists
        for callers that can't send this process a KeyboardInterrupt,
        e.g. the desktop app's watch mode, which runs ``watch()`` on a
        background thread."""
        self._stop_requested = True

    def watch(self, stop_after_runs: Optional[int] = None) -> list[RecordedRun]:
        """Blocking watch loop. Ctrl-C (or another thread calling
        ``request_stop()``) to stop. Returns completed runs.

        ``log_path`` not existing yet is not an error: WoW only creates the
        combat log the moment logging is actually enabled (``/combatlog``,
        the advanced-combat-logging checkbox, or -- with this project's own
        addon installed -- automatically at the start of your first key
        this session, see CombatLogging.lua). Starting a watch *before*
        that has happened (a completely normal thing to do -- open the app,
        click Start, then go play) used to crash immediately with
        ``FileNotFoundError``; it now waits quietly for the file to show
        up, same as it already waits for new lines once open.

        Starting a watch *after* a key is already underway is also normal
        (open the app mid-pull, or a multi-key session where an earlier
        key already wrote a CHALLENGE_MODE_START before this watch began)
        -- confirmed as a real report (2026-09-01): a key that finished
        completely normally was never picked up because watch() had
        skipped straight to end-of-file on open, past the START that was
        already sitting there, so the run's own CHALLENGE_MODE_END later
        matched nothing (``_feed()`` only reacts to an END while
        ``_current`` is set) and was silently dropped -- no error, no
        event, nothing. Fixed by scanning a pre-existing file once on
        open for its most recent CHALLENGE_MODE_START with no
        CHALLENGE_MODE_END after it, and resuming from there instead of
        EOF when one is found.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        runs: list[RecordedRun] = []
        self.echo(f"watching {self.log_path} (Ctrl-C to stop)")
        self.echo("make sure combat logging is on in game: /combatlog")
        waited_for_file = False
        if not self.log_path.exists():
            waited_for_file = True
            self.echo(f"{self.log_path} doesn't exist yet -- waiting for it "
                      "to appear (start a key, or enable combat logging)")
            if self.on_waiting_for_log is not None:
                self.on_waiting_for_log()
            while not self._stop_requested and not self.log_path.exists():
                time.sleep(self.poll_interval)
            if self._stop_requested:
                return runs
        fh = self._open()
        # from_start=False's usual "skip whatever's already in the file"
        # behavior only makes sense for a file that already existed when
        # watch() started (skip past a previous session's history). A file
        # that just appeared *because we were waiting for it* has no such
        # history to skip -- everything in it is fresh, from right now --
        # so seeking to its end here would silently miss it entirely.
        if not self.from_start and not waited_for_file:
            resume_at, in_progress, completed = self._find_resume_point(fh)
            # Keys that finished before this watch began (and weren't
            # handled by an earlier watch) get replayed through the normal
            # pipeline first -- see _catch_up / already_processed.
            runs.extend(self._catch_up(fh, completed))
            if stop_after_runs and len(runs) >= stop_after_runs:
                fh.close()
                return runs
            fh.seek(resume_at)
            if in_progress:
                self.echo("  found a key already in progress -- resuming it")
        try:
            while not self._stop_requested:
                line = fh.readline()
                if not line:
                    if self._truncated(fh):
                        fh.close()
                        fh = self._open()
                        continue
                    # Idle at end-of-file: the natural moment to notice WoW
                    # has moved on to a new session's log file.
                    rotated = self._maybe_rotate(fh)
                    if rotated is not None:
                        fh = rotated
                        continue
                    time.sleep(self.poll_interval)
                    continue
                run = self._feed(line)
                if run is not None:
                    runs.append(run)
                    if stop_after_runs and len(runs) >= stop_after_runs:
                        return runs
        except KeyboardInterrupt:
            self.echo("\nstopped.")
        finally:
            if self._current is not None:
                self._close_run(completed=False)
                runs.append(self._current)
            fh.close()
        return runs

    _last_rotation_check: float = field(default=0.0, repr=False)

    def _maybe_rotate(self, fh):
        """If a newer sibling ``WoWCombatLog*.txt`` has appeared, switch to
        it: close any open run as abandoned (WoW restarted -- that key can
        never get its end from the old file), reopen on the new file from
        its START (everything in it belongs to this fresh session), fire
        ``on_log_switched``, and return the new handle. None = no change.
        Rate-limited to ``rotation_check_s``; every failure is treated as
        "no change" so a transient stat error never kills the watch."""
        if not self.follow_rotation:
            return None
        now = time.monotonic()
        if now - self._last_rotation_check < self.rotation_check_s:
            return None
        self._last_rotation_check = now
        try:
            candidate = newest_combat_log(self.log_path.parent)
            if candidate is None or candidate.resolve() == self.log_path.resolve():
                return None
            if candidate.stat().st_mtime <= self.log_path.stat().st_mtime:
                return None
        except OSError:
            return None
        if self._current is not None:
            self._close_run(completed=False)
            self._notify(self.on_run_abandoned, self._current)
            self._current = None
        fh.close()
        self.log_path = candidate
        self.echo(f"  WoW started a new log -- now watching {candidate}")
        self._notify(self.on_log_switched, candidate)  # type: ignore[arg-type]
        return self._open()

    def _catch_up(self, fh, completed) -> list[RecordedRun]:
        """Replay each ``(start_offset, end_offset, zone, start_ts)`` range
        from ``_find_resume_point`` through ``_feed`` -- i.e. record its
        slice and fire ``on_run_complete`` exactly as if it had been tailed
        live -- unless ``already_processed(zone, start_ts)`` says it was.
        Returns the runs that completed. Leaves ``fh`` positioned wherever
        the last replay ended; the caller seeks to the real resume point
        afterwards."""
        done: list[RecordedRun] = []
        if self.already_processed is None:
            return done
        for start_off, end_off, zone, start_ts in completed:
            try:
                if self.already_processed(zone, start_ts):
                    continue
            except Exception:
                continue
            self.echo(f"  catching up on a finished key found in the log: {zone}")
            fh.seek(start_off)
            while fh.tell() < end_off:
                line = fh.readline()
                if not line:
                    break
                run = self._feed(line)
                if run is not None:
                    done.append(run)
            # a range that somehow didn't close cleanly must not bleed into
            # the live tail as an open run
            if self._current is not None:
                self._close_run(completed=False)
                self._current = None
        return done

    def _open(self):
        return open(self.log_path, "r", encoding="utf-8", errors="replace")

    def _find_resume_point(self, fh) -> tuple[int, bool, list]:
        """Scan a pre-existing log file once (only called right after
        opening it in ``watch()``) for its most recent CHALLENGE_MODE_START
        with no CHALLENGE_MODE_END after it -- an already-in-progress run
        at the moment watching started. Returns ``(offset, in_progress)``:
        the byte offset ``watch()`` should seek to (right at that START
        line when one is found and still unmatched, otherwise end-of-file
        -- the normal "only tail what's new" behavior), and whether an
        in-progress run was actually found.

        Uses ``fh.tell()``/``readline()`` (not manual byte-length math) so
        the returned offset is safe to pass back to this same text-mode
        file handle's ``seek()`` -- see the Python docs' own caveat that a
        text stream's ``tell()`` value is only meaningful when taken at a
        line boundary like this, not derived by summing decoded string
        lengths (multi-byte UTF-8 content would throw that off).
        """
        pending_start_offset: Optional[int] = None
        pending_start_line: Optional[str] = None
        # Every completed run seen on the way, as (start_offset,
        # end_offset_after_END, zone, start_ts) -- what _catch_up() replays
        # for keys that finished before this watch began (2026-09-02: a
        # timed key sat fully written in the current log at open time and
        # the old "skip to EOF" start silently walked past it).
        completed: list[tuple[int, int, str, float]] = []
        offset = fh.tell()
        line = fh.readline()
        while line:
            if _START_RE.search(line):
                pending_start_offset = offset
                pending_start_line = line
            elif _END_RE.search(line):
                if (pending_start_offset is not None and pending_start_line
                        and not _is_phantom_end(line)):
                    zone, start_ts = _start_identity(pending_start_line)
                    if zone is not None and start_ts is not None:
                        completed.append((pending_start_offset, fh.tell(), zone, start_ts))
                pending_start_offset = None
                pending_start_line = None
            offset = fh.tell()
            line = fh.readline()
        if pending_start_offset is not None:
            return pending_start_offset, True, completed
        return offset, False, completed

    def _truncated(self, fh) -> bool:
        try:
            return os.path.getsize(self.log_path) < fh.tell()
        except OSError:
            return False

    def _feed(self, line: str) -> Optional[RecordedRun]:
        """Process one raw log line; returns the run if one just completed."""
        if self._current is None:
            if _START_RE.search(line):
                self._start_run(line)
            return None

        if _START_RE.search(line):
            # A CHALLENGE_MODE_START arriving while a run is already open.
            # Told apart exactly the way segment_runs() tells it apart:
            #
            #   same key (same challenge_map_id AND keystone_level)
            #     -- a mid-key /reload re-logging its own start; keep
            #        accumulating into the run already being recorded.
            #   different key
            #     -- the open run never wrote a CHALLENGE_MODE_END
            #        (abandoned, vote-to-abandon, a crash, the log cut
            #        off). Close it as incomplete and start recording the
            #        new key.
            #
            # Before this, neither case was handled at all: a new key's
            # events were appended to the PREVIOUS run's slice file, the
            # new key never got a recording of its own, and its END later
            # closed -- and auto-analyzed/uploaded -- the wrong run under
            # the wrong dungeon's name. Confirmed against a real slice
            # (2026-09-02): a 132MB file named for a Kings' Rest key held
            # that key's start and a later Altar of Fangs key's start,
            # still growing, with neither ever uploading correctly.
            map_id, level = _parse_start_key(line)
            same_key = (
                map_id is not None
                and map_id == self._current.challenge_map_id
                and level == self._current.keystone_level
            )
            if same_key:
                self._out_fh.write(line)
                self._current.line_count += 1
                return None
            self._close_run(completed=False)
            self._notify(self.on_run_abandoned, self._current)
            self._current = None
            self._start_run(line)
            return None

        self._out_fh.write(line)
        self._current.line_count += 1

        if _DEATH_RE.search(line):
            # crude but cheap live counter; the real analysis is authoritative
            if re.search(r'UNIT_DIED,[^,]*,[^,]*,[^,]*,[^,]*,Player-', line):
                self._current.player_deaths += 1
                self.echo(f"  death #{self._current.player_deaths}")
                if self.obs_replay_on_death and self._obs is not None:
                    self._obs_call(self._obs.save_replay_buffer, "SaveReplayBuffer")

        if _END_RE.search(line):
            if _is_phantom_end(line):
                # WoW's all-zeroed phantom end (see _is_phantom_end) means
                # this run is being abandoned, not completed. Closing it
                # as completed=True here would report a bogus success and,
                # in the desktop app, auto-analyze and auto-upload a run
                # that never actually finished.
                self._close_run(completed=False)
                self._notify(self.on_run_abandoned, self._current)
                self._current = None
                return None
            self._close_run(completed=True)
            run = self._current
            self._current = None
            if self.on_run_complete is not None and run is not None:
                self.on_run_complete(run)
            return run
        return None

    def _start_run(self, line: str) -> None:
        m = _START_FIELDS_RE.search(line)
        zone = m.group(1) if m else "unknown"
        level = None
        map_id = None
        if m:
            level = _to_int(m.group(4))
            map_id = _to_int(m.group(3))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_zone = re.sub(r"[^A-Za-z0-9]+", "", zone) or "run"
        path = self.out_dir / f"{stamp}_{safe_zone}_{level or 'x'}.txt"
        # The name is only unique to the second, and two runs of the same
        # dungeon at the same level really can start within one second of
        # each other here: not while tailing live (keys are minutes apart),
        # but whenever the reader is catching up on already-buffered log --
        # a watch resumed mid-session, or the stretch that piled up while a
        # long analysis blocked the read loop. Opening "w" then silently
        # overwrote the earlier run's slice. Seen in a real log (2026-09-02)
        # that holds two separate Altar of Fangs +7 keys.
        suffix = 2
        while path.exists():
            path = self.out_dir / f"{stamp}_{safe_zone}_{level or 'x'}_{suffix}.txt"
            suffix += 1
        self._out_fh = open(path, "w", encoding="utf-8")
        self._out_fh.write(line)
        self._current = RecordedRun(
            path=path, zone=zone, keystone_level=level, started_at=time.time(),
            line_count=1, challenge_map_id=map_id,
        )
        self.echo(f"▶ recording: {zone} +{level or '?'} -> {path}")
        self._notify(self.on_run_start, self._current)
        self._run_hook(self.on_start_cmd, "on-run-start")

        self._obs = self._connect_obs() if self._obs_wanted() else None
        if self._obs is not None and self.on_start_cmd is None:
            self._obs_call(self._obs.start_record, "StartRecord")

    def _close_run(self, completed: bool) -> None:
        if self._current is None:
            return
        self._out_fh.close()
        self._current.completed = completed
        state = "complete" if completed else "incomplete (interrupted)"
        self.echo(
            f"■ run {state}: {self._current.zone} — "
            f"{self._current.line_count} events -> {self._current.path}"
        )
        self._run_hook(self.on_end_cmd, "on-run-end")

        if self._obs is not None:
            if self.on_end_cmd is None:
                output_path = self._obs_call(self._obs.stop_record, "StopRecord")
                if output_path:
                    self._current.obs_output_path = output_path
            self._obs_call(self._obs.close, "close")
            self._obs = None

    def _obs_wanted(self) -> bool:
        """Whether a native OBS connection is worth opening for this run.

        Shell-hook precedence means the native client only has a start or
        stop call to make when the corresponding shell hook is *not*
        configured; when both hooks are set and replay-on-death isn't
        requested, there's nothing for the native client to do, so we
        skip connecting to OBS at all (rather than connecting and simply
        not calling anything) -- see the docstring at the top of this
        file and WP-D1's acceptance criteria on shell-hook precedence.
        """
        return bool(self.obs_url) and (
            self.on_start_cmd is None or self.on_end_cmd is None
            or self.obs_replay_on_death
        )

    def _connect_obs(self):
        """Open (and Identify on) a fresh OBS WebSocket connection for the
        run that's just starting. Connect failures -- unreachable OBS,
        failed handshake, bad password -- are warnings, never fatal."""
        from .obsws import OBSClient

        try:
            client = OBSClient(self.obs_url, self.obs_password)
            client.connect()
            return client
        except Exception as exc:
            self.echo(f"  warning: obs connect failed: {exc}")
            return None

    def _notify(self, callback: Optional[Callable[[RecordedRun], None]],
                run: Optional[RecordedRun]) -> None:
        """Invoke an optional run-lifecycle callback (on_run_start /
        on_run_abandoned) best-effort: a raising callback is reported as a
        warning and never interrupts the recording itself."""
        if callback is None or run is None:
            return
        try:
            callback(run)
        except Exception as exc:
            self.echo(f"  warning: run callback failed: {exc}")

    def _obs_call(self, fn: Callable[[], object], label: str):
        """Run one OBS request, turning any failure into a warning."""
        try:
            return fn()
        except Exception as exc:
            self.echo(f"  warning: obs {label} failed: {exc}")
            return None

    def _run_hook(self, cmd: Optional[str], label: str) -> None:
        """Fire-and-forget shell hook with run context in the environment."""
        if not cmd:
            return
        env = dict(os.environ)
        if self._current is not None:
            env["MA_ZONE"] = self._current.zone
            env["MA_LEVEL"] = str(self._current.keystone_level or "")
            env["MA_PATH"] = str(self._current.path)
        try:
            subprocess.Popen(cmd, shell=True, env=env)
            self.echo(f"  ({label} hook fired)")
        except OSError as exc:
            self.echo(f"  warning: {label} hook failed: {exc}")
