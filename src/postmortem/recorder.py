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

_START_RE = re.compile(r"  CHALLENGE_MODE_START,")
_END_RE = re.compile(r"  CHALLENGE_MODE_END,")
_DEATH_RE = re.compile(r"  UNIT_DIED,")


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


@dataclass
class Recorder:
    log_path: Path
    out_dir: Path
    from_start: bool = False
    poll_interval: float = 0.5
    on_run_complete: Optional[Callable[[RecordedRun], None]] = None
    on_start_cmd: Optional[str] = None  # shell hook (e.g. start OBS recording)
    on_end_cmd: Optional[str] = None    # shell hook (e.g. stop OBS recording)
    obs_url: Optional[str] = None       # e.g. ws://127.0.0.1:4455 -- native OBS control
    obs_password: Optional[str] = None
    obs_replay_on_death: bool = False
    echo: Callable[[str], None] = print
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
        ``request_stop()``) to stop. Returns completed runs."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        runs: list[RecordedRun] = []
        self.echo(f"watching {self.log_path} (Ctrl-C to stop)")
        self.echo("make sure combat logging is on in game: /combatlog")
        fh = self._open()
        if not self.from_start:
            fh.seek(0, os.SEEK_END)
        try:
            while not self._stop_requested:
                line = fh.readline()
                if not line:
                    if self._truncated(fh):
                        fh.close()
                        fh = self._open()
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

    def _open(self):
        return open(self.log_path, "r", encoding="utf-8", errors="replace")

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
            self._close_run(completed=True)
            run = self._current
            self._current = None
            if self.on_run_complete is not None and run is not None:
                self.on_run_complete(run)
            return run
        return None

    def _start_run(self, line: str) -> None:
        m = re.search(r'CHALLENGE_MODE_START,"([^"]*)",([^,]*),([^,]*),([^,]*)', line)
        zone = m.group(1) if m else "unknown"
        level = None
        if m:
            try:
                level = int(m.group(4))
            except ValueError:
                level = None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_zone = re.sub(r"[^A-Za-z0-9]+", "", zone) or "run"
        path = self.out_dir / f"{stamp}_{safe_zone}_{level or 'x'}.txt"
        self._out_fh = open(path, "w", encoding="utf-8")
        self._out_fh.write(line)
        self._current = RecordedRun(
            path=path, zone=zone, keystone_level=level, started_at=time.time(),
            line_count=1,
        )
        self.echo(f"▶ recording: {zone} +{level or '?'} -> {path}")
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
