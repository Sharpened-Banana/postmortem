"""Python <-> JS bridge for the pywebview desktop app.

Every public method on :class:`DesktopAPI` is JSON-serializable in both
directions and never raises an uncaught exception across the bridge --
a JS caller has no way to catch a Python traceback, so every expected
failure is reported back as ``{"ok": False, "error": "..."}`` instead
(the two settings getters/setters and the three ``pick_*`` dialog
methods are the exceptions: see their own docstrings).

Every business-logic method here does exactly what one ``cli.py``
subcommand already does -- see that module's ``cmd_analyze``,
``cmd_runs``, ``cmd_extract_data``, ``cmd_index`` and its
``_load_route``/``_load_store``/``_load_avoidable``/``_pick_run``
helpers -- just returning a JSON-able dict instead of writing files or
printing to stdout. Those helpers are imported and called directly
(not reimplemented) so this module can't drift from the CLI's own
tolerant-error-handling behavior; the only adaptation is that a
``SystemExit`` cli.py would raise (and let propagate to the process
exit code) is instead caught here and turned into an error dict --
along with any other exception that manages to escape, since a bridge
method's contract ("never raise") is stricter than the CLI's own.

pywebview is only imported -- locally, inside a method body -- by the
three native-dialog picker methods at the bottom of this file
(``pick_log_file``, ``pick_route_file``, ``pick_folder``). Every other
method is plain Python with zero GUI-framework dependency, so it's
testable exactly like the rest of this codebase (see
``tests/test_desktop_api.py``).
"""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from .. import cli as _cli
from ..analysis.pulls import DEFAULT_PULL_GAP_S
from ..combatlog.parser import parse_file
from ..combatlog.segmenter import segment_runs
from ..mdt.decode import MDTDecodeError, decode_mdt_string
from ..mdt.extract import write_dungeon_data
from ..mdt.route import Route
from ..history.store import has_run as _has_run
from ..history.store import has_uploaded_run as _has_uploaded_run
from ..history.store import mark_uploaded as _mark_uploaded
from ..recorder import Recorder
from ..report.html import render_html
from ..report.index import collect_report_files, render_index
from . import config as _config
from . import updater as _updater


def _load_tank_knowledge_quietly():
    """The addon's spellbook capture for the tank death post-mortem, or
    None -- never raising, whatever is on disk.

    Deliberately softer than ``cli._load_tank_knowledge``, and for a
    reason that isn't style. There, the user named a file with an explicit
    ``--tank-db``, so a file that doesn't parse is a mistake worth
    stopping on. Here nobody asked for anything: the path was discovered
    (see ``config.resolve_tank_db_path``), and a SavedVariables file half
    written by a client that is running *right now* is an ordinary
    condition, not an error. Failing the whole analysis over it would cost
    the user their run's report to improve one section of it.

    So every failure degrades to None, which costs only the spellbook
    resolution: the tank post-mortem still runs, it just keeps its "may
    not be talented" hedge.
    """
    try:
        path = _config.resolve_tank_db_path(_config.load_settings())
        if path is None:
            return None
        from ..analysis.tank_death import knowledge_from_savedvariables

        table = _cli._read_savedvariables_table(path, "PostmortemTankDB")
        return knowledge_from_savedvariables(table)
    except (OSError, ValueError, KeyError, TypeError, SystemExit):
        # SystemExit is in that list on purpose: the shared
        # _read_savedvariables_table raises it for a missing assignment or
        # unparseable Lua, which is right for a CLI and wrong here.
        return None


#: What a History row's link carries instead of a file name -- see
#: list_history(). Not a real URL scheme: the frame's click handler below
#: intercepts it before the browser tries to navigate anywhere.
_HISTORY_REF_SCHEME = "pm-run:"

#: Appended to the History page. The frame is sandboxed without
#: same-origin, so the app cannot reach into it; the page tells the app
#: which run was clicked instead. Only the opaque ref crosses.
_HISTORY_OPEN_SCRIPT = """<script>
document.addEventListener("click", function (e) {
  var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
  if (!a) return;
  var href = a.getAttribute("href") || "";
  if (href.indexOf("pm-run:") !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  window.parent.postMessage({type: "postmortem-open-run", ref: href.slice(7)}, "*");
}, true);
</script>"""


#: How many already-finished keys a starting watch will replay. A fresh
#: install pointed at a months-old log otherwise analyzes and uploads every
#: key it has ever recorded (2026-09-11).
_CATCH_UP_LIMIT = 3


def _snapshot_headlines(report: dict) -> list[dict]:
    """``snapshot_headline`` for each of the report's snapshots -- what the
    report screen's Snapshots strip labels its buttons with. Best-effort:
    a snapshot the headline can't summarise still gets a button (role and
    time only), and a report without the key gets an empty list."""
    from ..analysis.snapshot import snapshot_headline

    out = []
    for i, snap in enumerate(report.get("snapshots") or [], start=1):
        if not isinstance(snap, dict):
            continue
        try:
            head = snapshot_headline(snap)
        except Exception:
            meta = snap.get("snapshot") or {}
            head = {"n": snap.get("n"), "role": meta.get("role") or "general",
                    "t": _mmss(meta.get("t_marker")), "focus": None, "line": ""}
        if head.get("n") is None:
            head["n"] = i
        out.append(head)
    return out


def _render_report_snapshot(report: dict, n, label: Optional[str]) -> dict:
    """``{"ok": True, "html", "label"}`` for ``report["snapshots"][n-1]``,
    or ``{"ok": False, "error"}``; ``n`` arrives from JS so it is checked
    rather than trusted. Never raises."""
    try:
        snapshots = report.get("snapshots") or []
        try:
            index = int(n)
        except (TypeError, ValueError):
            return {"ok": False, "error": "that is not a snapshot number"}
        if not (1 <= index <= len(snapshots)):
            return {"ok": False,
                    "error": f"this run has {len(snapshots)} snapshot(s); "
                             f"there is no snapshot {index}"}
        snap = snapshots[index - 1]
        from ..report.snapshot import render_snapshot_html
        meta = snap.get("snapshot") or {}
        role = meta.get("role") or "general"
        return {
            "ok": True,
            "html": render_snapshot_html(snap),
            "label": f"{label or 'Run'} — Snapshot {index} ({role} at "
                     f"{_mmss(meta.get('t_marker'))})",
        }
    except Exception as exc:  # never raise across the bridge
        return {"ok": False, "error": f"could not render that snapshot: {exc}"}


def _snapshot_seconds(value, default: int, lo: int, hi: int) -> int:
    """A snapshot window setting as a clamped int; anything unusable is
    the default (settings.json is user-editable)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _mmss(seconds) -> str:
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "?"
    return f"{total // 60}:{total % 60:02d}"


def _normalized_site_url(raw: Any) -> str:
    """A saveable site URL, or ValueError. Accepts a bare hostname (the
    same normalization uploads apply) but nothing that is not http(s)."""
    from urllib.parse import urlsplit

    from ..upload import site_base_url

    import re

    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("site URL must be text")
    raw = raw.strip()
    # Check the scheme on the RAW text: site_base_url() prepends https://
    # to anything that has no "//", which turns "javascript:x" into a
    # nominally https URL whose host is "javascript".
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", raw)
    if scheme and scheme.group(1).lower() not in ("http", "https"):
        raise ValueError("site URL must be an http(s) address")
    url = site_base_url(raw)
    parts = urlsplit(url)
    try:
        parts.port
    except ValueError:
        raise ValueError("site URL must be an http(s) address") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("site URL must be an http(s) address")
    return url


def _same_origin(a: str, b: str) -> bool:
    """Do two URLs share scheme, host and port?"""
    from urllib.parse import urlsplit

    from ..upload import site_base_url

    pa, pb = urlsplit(site_base_url(a)), urlsplit(site_base_url(b))
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def _checked_site_url(explicit: Optional[str], configured: Optional[str]) -> str:
    """The site to talk to for a call that may name one itself.

    Every bridge method is reachable from any script running in the app
    window, and several of these carry this install's upload token. No
    render path can reach them today -- the interface escapes its inserts
    and both report frames are sandboxed -- but "one XSS away from
    exfiltrating the token" is not a line worth holding on one defence
    (2026-09-11), so a caller-supplied target must match the configured
    site's origin.

    With nothing configured yet there is no origin to compare against and
    the explicit value is taken: that is first-run setup, before any token
    exists to leak. Raises ValueError when the two disagree.
    """
    if not explicit:
        if not configured:
            raise ValueError("no site URL configured -- set one in Settings first")
        return configured
    if configured and not _same_origin(explicit, configured):
        raise ValueError(
            "refusing to contact a site other than the one configured in Settings"
        )
    return explicit


def _already_uploaded(db_path, zone, start_ts) -> bool:
    """Has this key already been analyzed AND uploaded?

    A lock or an unreadable history is deliberately NOT treated as "yes":
    the honest answer is "cannot tell", and re-processing one run is
    cheap and de-duplicated on ingest, where skipping one silently loses
    it forever.
    """
    try:
        return _has_uploaded_run(db_path, zone, start_ts)
    except Exception:
        return False


class DesktopAPI:
    """Bridge object exposed to the desktop app's JS runtime (typically
    as ``window.pywebview.api``). Construct one instance and hand it to
    ``webview.create_window(..., js_api=DesktopAPI())`` -- this class
    itself has no pywebview dependency, so nothing about constructing or
    calling its business-logic methods requires pywebview to be
    installed at all (only the three ``pick_*`` dialog methods do, at
    call time).
    """

    def __init__(self) -> None:
        # Watch-mode state (see start_watch/stop_watch below). None
        # until a watch is started; every other method on this class is
        # stateless, so this is the one place instance state lives.
        self._watch_recorder: Optional[Recorder] = None
        # Completed runs are handed from the watch (tailing) thread to a
        # separate worker via this queue, so analysis + upload never block
        # log reading -- see start_watch. None while not watching.
        self._watch_queue: Optional["queue.Queue"] = None
        self._watch_worker: Optional[threading.Thread] = None
        self._watch_stop_sentinel: Optional[object] = None
        self._watch_thread: Optional[threading.Thread] = None
        # Pending snapshot builds, one timer per keybind marker seen while
        # watching (docs/SNAPSHOT.md §3); cancelled by stop_watch.
        self._snapshot_timers: dict[tuple[str, float], threading.Timer] = {}
        self._hotkey: Optional["HotkeyListener"] = None
        # Auto-update state (see check_for_update/start_update below).
        self._update_thread: Optional[threading.Thread] = None
        # Opaque ref -> where that History row's report lives, for the rows
        # the last list_history() call produced. The History page asks to
        # open a run by ref, never by path, so a script in that frame can
        # only ever open something this class itself just listed.
        self._history_refs: dict[str, tuple[str, str, Optional[int]]] = {}
        # The report behind the report screen right now -- what analyze()
        # produced or open_history_run() loaded last. The Snapshots strip
        # asks for one of its snapshots by number (open_report_snapshot),
        # by number rather than by shipping the report back over the
        # bridge: with map art embedded a report is megabytes.
        self._last_report: Optional[dict] = None

    # -- run listing --------------------------------------------------------

    def list_runs(self, log_path: str) -> dict:
        """List every Mythic+ run found in a combat log.

        Mirrors ``cli.py``'s ``cmd_runs``: streams
        ``segment_runs(parse_file(log_path))`` one segment at a time
        (never ``list(...)``-ing the whole thing up front) and drops
        each segment's ``events`` immediately after summarizing it, so a
        log with many runs never holds more than one run's full event
        list in memory at once -- same memory-conscious pattern as
        ``cli.py``'s ``_pick_run`` (see that module's WP-A0 notes).

        Returns ``{"ok": True, "runs": [...]}`` where each entry is
        ``RunSegment.summary()`` plus a 1-based ``"index"`` field (so a
        JS picker can show "which run in this log" and pass the index
        straight back as ``analyze()``'s ``run_selector``), or
        ``{"ok": False, "error": "..."}`` for a missing/unreadable log
        file or any other failure. Never raises.
        """
        try:
            runs = []
            for i, seg in enumerate(segment_runs(parse_file(log_path)), start=1):
                summary = seg.summary()
                seg.events = []  # never needed again once summarized
                summary["index"] = i
                runs.append(summary)
            return {"ok": True, "runs": runs}
        except Exception as exc:  # never raise across the JS bridge
            return {"ok": False, "error": str(exc)}

    # -- analysis -------------------------------------------------------

    def analyze(self, params: dict) -> dict:
        """Run the full post-mortem pipeline on one run from a combat
        log: parse -> segment -> pick run -> build route/dungeon-data/
        avoidable-data/par_ms -> ``analyze_run()`` -> optional Raider.io
        enrichment. Exactly the pipeline ``cli.py``'s ``cmd_analyze``
        runs, reusing its own loader/picker helpers directly.

        ``params`` (a plain dict -- e.g. straight off a JS call):

        - ``log_path`` (str, required) -- path to a WoWCombatLog.txt (or
          a recorded run slice).
        - ``run_selector`` (str, default ``"last"``) -- a 1-based run
          number (as a string or int) from ``list_runs()``, or
          ``"last"``.
        - ``route`` (str, optional) -- an MDT export string, or a path
          to a file containing one (same tolerant string-or-path
          detection as ``cli.py``'s ``_load_route``).
        - ``dungeon_data_path`` (str, optional) -- extracted dungeon
          data JSON (see ``extract_dungeon_data``).
        - ``avoidable_data_path`` (str, optional) -- avoidable-damage
          tagging JSON.
        - ``raiderio_region`` (str, optional) -- ``us``/``eu``/``kr``/
          ``tw``/``cn``; enables Raider.io enrichment (needs network
          access) and, together with ``timer_data_path``/
          ``expansion_id``, keystone-timer par-time resolution.
        - ``raiderio_no_cache`` (bool, default False) -- bypass the
          on-disk Raider.io lookup cache for this run.
        - ``timer_data_path`` (str, optional) -- JSON file mapping
          challenge_map_id -> par time in ms (falls back to the bundled
          example seed when omitted and ``raiderio_region`` is set).
        - ``expansion_id`` (int, optional) -- only used together with
          ``raiderio_region``, for a live Raider.io static-data par-time
          fetch.
        - ``pull_gap_seconds`` (float, default analysis.pulls.DEFAULT_PULL_GAP_S).
        - ``death_penalty_s`` (float, default 15.0).
        - ``full_cast_timeline`` (bool, default True) -- include the
          full per-cast timeline in the report (CLI default; the CLI's
          ``--no-cast-timeline`` flag flips this off).

        Returns ``{"ok": True, "report": <dict>, "html": <str>}`` on
        success -- ``report`` is guaranteed to round-trip losslessly
        through ``json.dumps``/``json.loads``, and ``html`` is a
        complete self-contained document from
        ``report.html.render_html()``. Returns
        ``{"ok": False, "error": "<message>"}`` for any expected failure
        (missing/unreadable log, a route string that doesn't decode, a
        dungeon-data/avoidable-data path that doesn't load, a log with
        no Mythic+ runs, an out-of-range ``run_selector``, ...). Never
        raises.
        """
        try:
            return self._analyze_impl(dict(params or {}))
        except SystemExit as exc:
            # cli.py's reused helpers (_load_route/_load_store/
            # _load_avoidable/_pick_run) raise SystemExit for exactly
            # this class of expected, user-facing failure.
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # belt-and-suspenders: never raise
            return {"ok": False, "error": str(exc)}

    def _analyze_impl(self, params: dict) -> dict:
        log_path = params.get("log_path")
        if not log_path:
            raise SystemExit("error: log_path is required")

        route = _cli._load_route(params["route"]) if params.get("route") else None
        store = _cli._load_store(
            params.get("dungeon_data_path")
            # zero-config fallback: app data folder, then the packaged copy
            or _config.resolve_dungeon_data_path(_config.load_settings())
        )
        avoidable = _cli._load_avoidable(
                params.get("avoidable_data_path")
                # zero-config fallback: <config dir>/avoidable_spells.json
                or _config.resolve_avoidable_data_path(_config.load_settings())
            )
        interrupt_data = _cli._load_effective_interrupt_data(
            params.get("interrupt_data_path")
            # zero-config fallback: app data folder, then the packaged
            # community-sourced database (see bundled.py) -- until
            # 2026-09-04 this wasn't loaded here at all, so kick-efficiency
            # reporting silently ran on the plain "kicked at least once"
            # heuristic for every desktop analysis, not just Watch Live.
            or _config.resolve_interrupt_data_path(_config.load_settings()),
            # ...with whatever this account's own logs have proven on top
            _config.resolve_learned_interrupts_path(_config.load_settings()),
        )
        stealable = _cli._load_stealable(
            params.get("stealable_data_path")
            # zero-config fallback: <config dir>/stealable_spells.json --
            # no packaged copy (unlike interrupt_data): there's no
            # community database to bundle for this one, see
            # analysis/stealable.py.
            or _config.resolve_stealable_data_path(_config.load_settings())
        )

        run_selector = str(params.get("run_selector") or "last")
        segment = _cli._pick_run(segment_runs(parse_file(log_path)), run_selector)
        # Fold what this run proves about interruptibility back into the
        # accumulated file, so the next analysis knows more than this one
        # did (see analysis/interrupt_learning.py). Best-effort: a cache
        # that can't be written must never cost the user their report.
        try:
            from ..analysis.interrupt_learning import update_from_events
            update_from_events(
                segment.events,
                _config.resolve_learned_interrupts_path(_config.load_settings()),
            )
        except Exception:
            pass
        if route is None:
            # No route pasted for this analysis: fall back to the saved
            # default for whichever dungeon this run turns out to be.
            route = self._default_route_for(
                segment.challenge_map_id, segment.zone_name, store,
            )

        # _resolve_timer_par_ms only reads a handful of attributes off
        # its `args` -- a SimpleNamespace with the same names stands in
        # for the argparse.Namespace cli.py normally passes it, without
        # this module having to reimplement its (fairly involved)
        # live-fetch/fallback resolution logic.
        timer_args = SimpleNamespace(
            timer_data=params.get("timer_data_path"),
            raiderio=params.get("raiderio_region"),
            raiderio_no_cache=bool(params.get("raiderio_no_cache", False)),
            expansion_id=params.get("expansion_id"),
        )
        par_ms = _cli._resolve_timer_par_ms(timer_args, segment.challenge_map_id)

        from ..analysis.run_analyzer import analyze_run

        report = analyze_run(
            segment,
            route=route,
            store=store,
            avoidable=avoidable,
            interrupt_data=interrupt_data,
            stealable=stealable,
            pull_gap_seconds=float(params.get("pull_gap_seconds", DEFAULT_PULL_GAP_S)),
            full_cast_timeline=bool(params.get("full_cast_timeline", True)),
            death_penalty_s=float(params.get("death_penalty_s", 15.0)),
            par_ms=par_ms,
            spell_damage_history_path=_config.resolve_learned_spell_damage_path(
                _config.load_settings()),
            community_spell_damage=_cli._load_community_spell_damage(),
            # The addon's spellbook capture, found rather than configured
            # (see config.resolve_tank_db_path). Without it the tank death
            # post-mortem still runs, it just keeps its "may not be
            # talented" hedge -- so a failure to locate it degrades the
            # report rather than the run.
            tank_knowledge=_load_tank_knowledge_quietly(),
        )

        raiderio_region = params.get("raiderio_region")
        if raiderio_region:
            from ..raiderio import _default_fetcher, enrich_report

            if params.get("raiderio_no_cache"):
                fetcher = _default_fetcher
            else:
                from ..cache import cached_fetcher
                fetcher = cached_fetcher(_default_fetcher)
            enrich_report(report, raiderio_region, fetcher=fetcher)

        # Route-map background from the user's own MDT install (mapart.py):
        # best-effort, embedded into the local report only -- upload.py
        # strips it before anything goes to the public site.
        from .. import mapart
        mdt_dir = mapart.mdt_dir_from_log_path(log_path) or mapart.mdt_dir_from_log_path(
            _config.load_settings().get("wow_log_path") or ""
        )
        mapart.attach_map_backgrounds(report, mdt_dir, store)
        # The run's snapshots ride inside the report (report["snapshots"],
        # docs/SNAPSHOT.md) -- the same contract Watch Live's
        # _write_recorded_reports keeps -- so the saved JSON, the history
        # row and an upload all carry them. Best-effort inside.
        _cli.attach_snapshots(
            report, segment, store=store, avoidable=avoidable,
            interrupt_data=interrupt_data, stealable=stealable,
            pull_gap_seconds=float(params.get("pull_gap_seconds", DEFAULT_PULL_GAP_S)),
        )

        html = render_html(report)
        saved = self._save_report_locally(report, html)
        self._last_report = report
        return {"ok": True, "report": report, "html": html, "saved": saved,
                "snapshot_headlines": _snapshot_headlines(report)}

    def _save_report_locally(self, report: dict, html: str) -> Optional[dict]:
        """Best-effort: write this analyzed report's JSON/HTML next to
        every other locally-saved report and ingest it into the local
        run-history database, so a "New Analysis" run shows up on the
        History screen exactly like a Watch Live run already does --
        with zero required setup (see ``config.resolve_output_dir``/
        ``resolve_history_db_path``'s own "works with zero setup"
        fallback, the same philosophy ``start_watch()`` already
        established for its own recorded-run output).

        Returns ``{"ok": True, "json_path", "html_path", "run_id"}`` on
        success, or ``{"ok": False, "error": "..."}`` if saving failed for
        any reason (an unwritable directory, a locked database, ...) --
        this must never block showing the report itself, the same
        "best-effort bonus step" philosophy as Raider.io enrichment and
        site uploads elsewhere in this codebase.

        Until 2026-09-11 a failure returned None and the interface simply
        hid the "saved" line, so an unwritable output directory meant the
        report was never written and the run never appeared in History,
        with nothing shown anywhere. Reporting it is the whole point:
        best-effort is about not blocking, not about staying quiet.
        """
        try:
            settings = _config.load_settings()
            out_dir = _config.resolve_output_dir(settings, "analyzed-runs")
            out_dir.mkdir(parents=True, exist_ok=True)
            base = out_dir / _cli._report_basename(report)
            json_path = base.with_suffix(".json")
            html_path = base.with_suffix(".html")
            json_path.write_text(json.dumps(report, indent=1), encoding="utf-8")
            html_path.write_text(html, encoding="utf-8")

            from ..history.store import ingest as ingest_history
            db_path = _config.resolve_history_db_path(settings)
            run_id = ingest_history(report, db_path, source_path=json_path, html_path=html_path)
            return {"ok": True, "json_path": str(json_path),
                    "html_path": str(html_path), "run_id": run_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # -- history ----------------------------------------------------------

    def get_default_paths(self) -> dict:
        """The effective output-folder/history-database paths reports get
        saved to right now -- the saved ``default_output_dir``/
        ``history_db_path`` settings when set, otherwise the same
        zero-config defaults ``_save_report_locally``/``start_watch``
        actually use (see ``config.resolve_output_dir``/
        ``resolve_history_db_path``). Purely informational (the UI shows
        these as hints -- e.g. Settings' placeholder text, History's
        pre-filled database field) -- nothing here writes anything.
        Never raises.
        """
        try:
            settings = _config.load_settings()
            return {
                "ok": True,
                "output_dir": str(_config.resolve_output_dir(settings, "analyzed-runs")),
                "history_db_path": str(_config.resolve_history_db_path(settings)),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def list_history(
        self, db_path: Optional[str] = None, directory: Optional[str] = None,
    ) -> dict:
        """Browse previously-analyzed runs, from either a SQLite history
        database or a directory of saved report JSON files -- the two
        sources ``cli.py``'s ``index`` subcommand supports side by side
        (see ``cmd_index``). Pass exactly one of ``db_path``/
        ``directory``; if both are given, ``db_path`` wins (matching
        ``cmd_index``'s own ``--db`` precedence).

        Returns ``{"ok": True, "rows": [...], "html": "<str>"}`` --
        ``rows`` in the same shape either source produces
        (``history.store.query_runs`` and ``report.index.collect_reports``
        are already proven to return matching row shapes; this method
        just wires one of them up), and ``html`` from
        ``report.index.render_index(rows)`` directly (not reimplemented).
        Returns ``{"ok": False, "error": "..."}`` if neither argument is
        given, or the underlying lookup fails. Never raises.
        """
        try:
            if db_path:
                from ..history.store import Store
                with Store(db_path) as store:
                    located = [(("db", str(db_path), run_id), row)
                               for run_id, row in store.query_runs_with_ids()]
            elif directory:
                located = [(("file", str(path), None), row)
                           for path, row in collect_report_files(directory)]
            else:
                return {"ok": False, "error": "db_path or directory is required"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        # Every row's "open" link used to be the report's bare HTML file
        # NAME, which only resolves when the index page sits in the same
        # folder as the reports (the CLI's `serve`). Inside this app the
        # page is a srcdoc frame, so the link resolved against the app's
        # own shell and every click was a 404 -- and a run saved without
        # an HTML file (every Watch Live run in a database) had no link at
        # all. Each row now carries an opaque ref instead, the page posts
        # it to the app on click, and open_history_run() renders the
        # report from its stored JSON.
        import secrets

        refs: dict[str, tuple[str, str, Optional[int]]] = {}
        rows = []
        for where, row in located:
            ref = secrets.token_urlsafe(9)
            refs[ref] = where
            rows.append({**row, "html": f"{_HISTORY_REF_SCHEME}{ref}"})
        self._history_refs = refs
        html = render_index(rows)
        marker = "</body>"
        cut = html.rfind(marker)
        html = (html[:cut] + _HISTORY_OPEN_SCRIPT + html[cut:]) if cut != -1 \
            else html + _HISTORY_OPEN_SCRIPT
        return {"ok": True, "rows": rows, "html": html}

    def open_history_run(self, ref: str) -> dict:
        """Render one run the last ``list_history()`` call listed.

        ``ref`` is the opaque token that call put in the row -- never a
        path, so the History frame cannot use this to read an arbitrary
        file. Returns ``{"ok": True, "html", "report", "label"}`` or
        ``{"ok": False, "error"}``. Never raises.
        """
        where = self._history_refs.get(ref) if isinstance(ref, str) else None
        if where is None:
            return {"ok": False,
                    "error": "that run is no longer listed -- reload History and try again"}
        kind, location, run_id = where
        try:
            if kind == "db":
                from ..history.store import Store
                with Store(location) as store:
                    report = store.get_report(int(run_id))
            else:
                with open(location, "r", encoding="utf-8") as fh:
                    report = json.load(fh)
            if not isinstance(report, dict):
                return {"ok": False, "error": "that run's saved report could not be found"}
            run = report.get("run") or {}
            level = run.get("keystone_level")
            label = " ".join(
                part for part in (run.get("zone"), f"+{level}" if level is not None else None)
                if part
            )
            self._last_report = report
            return {"ok": True, "html": render_html(report), "report": report,
                    "label": label, "snapshot_headlines": _snapshot_headlines(report)}
        except Exception as exc:  # never raise across the bridge
            return {"ok": False, "error": f"could not open that run: {exc}"}

    def open_history_snapshot(self, ref: str, n: int) -> dict:
        """Render snapshot ``n`` (1-based) of a run the last
        ``list_history()`` call listed -- the run's ``report["snapshots"]``
        (docs/SNAPSHOT.md), not a loose file. Returns ``{"ok": True,
        "html", "label"}`` or ``{"ok": False, "error"}``. Never raises."""
        opened = self.open_history_run(ref)
        if not opened.get("ok"):
            return opened
        return _render_report_snapshot(opened["report"], n, opened.get("label"))

    def open_report_snapshot(self, n: int) -> dict:
        """Render snapshot ``n`` (1-based) of the report the report screen
        is showing -- the last analyze() or open_history_run() result, so
        the Snapshots strip works the same after New Analysis and from
        History. ``{"ok": True, "html", "label"}`` or ``{"ok": False,
        "error"}``. Never raises."""
        report = self._last_report
        if not isinstance(report, dict):
            return {"ok": False, "error": "no report is open -- analyze a run first"}
        run = report.get("run") or {}
        level = run.get("keystone_level")
        label = " ".join(
            part for part in (run.get("zone"), f"+{level}" if level is not None else None)
            if part
        )
        return _render_report_snapshot(report, n, label)

    # -- public tracker upload -----------------------------------------------

    def upload_report(self, report: dict, url: Optional[str] = None) -> dict:
        """Upload an already-analyzed ``report`` (as returned by
        ``analyze()``'s own ``"report"`` field) to a public
        postmortem tracker site (see ``site/postmortem_site/`` and
        ``postmortem.upload``).

        ``url`` defaults to the saved ``site_url`` setting when not
        given explicitly. Returns ``{"ok": False, "error": "no site URL
        configured"}`` if neither is set. Otherwise delegates entirely
        to ``upload.upload_report()``, which never raises -- every
        failure (network error, validation rejection, rate limit,
        ownership conflict) already comes back as a plain
        ``{"ok": False, "error": "..."}`` dict from that function, so
        this method just returns whatever it returns.
        """
        try:
            target = _checked_site_url(url, _config.load_settings().get("site_url"))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        from .. import upload as _upload
        try:
            result = _upload.upload_report(report, target)
            groupmate_url = _upload.duplicate_of(result, target)
            if groupmate_url:
                result["url"] = groupmate_url
            return result
        except Exception as exc:  # noqa: BLE001
            # upload_report() is documented never to raise, and now does
            # not -- but this method is called across the JS bridge, where
            # an exception is a raw traceback in the interface rather than
            # a message. Belt and braces at the boundary that has to hold.
            return {"ok": False, "error": str(exc)}

    # -- live watch mode (auto-analyze + auto-upload every run) -------------
    #
    # Unlike every other method here, start_watch() doesn't do its work
    # synchronously and return a result -- Recorder.watch() blocks for
    # the whole play session, so it runs on a background thread, and
    # progress is pushed to the UI via _emit_watch_event() (which calls
    # webview.windows[0].evaluate_js(...) to invoke window.onWatchEvent()
    # in shell/app.js) rather than a return value, since there's no
    # "return value" for something that happens minutes after the JS
    # call that started it already returned.
    #
    # Every event is a dict with a "type" key; window.onWatchEvent()
    # switches on it. The full set, in the order a normal session emits
    # them:
    #   {"type": "watching", "log_path": str}
    #     -- start_watch() succeeded; the watch thread is running. Note
    #        this fires immediately even if the log file doesn't exist
    #        yet (a completely normal thing -- see "waiting_for_log"
    #        below): "watching" means the thread is alive and will start
    #        tailing the moment there's something to tail, not that it's
    #        actively reading lines right now.
    #   {"type": "waiting_for_log", "log_path": str}
    #     -- log_path doesn't exist yet (WoW hasn't started writing it
    #        this session -- see Recorder.watch()'s own docstring). Not an
    #        error: watching continues, waiting for the file to appear
    #        (e.g. once the first key of the session starts). No event
    #        marks the transition out of this state -- the next event
    #        (run_complete, or another watching session's normal
    #        progress) means it resolved.
    #   {"type": "run_complete", "zone": str, "level": int|None}
    #     -- a key just ended; analysis is starting.
    #   {"type": "analyzed", "zone": str, "level": int|None, "timed": bool|None}
    #     -- analysis finished; upload is starting.
    #   {"type": "results_written", "zone": str, "level": int|None}
    #     -- the crunched stats were written into the installed addon's
    #        folder (PostmortemResults.lua); the player can /reload in WoW
    #        to see them in-game. Only emitted when the addon is installed
    #        (addon_dir derivable + exists); silently absent otherwise.
    #   {"type": "uploaded", "url": str}
    #   {"type": "uploaded_by_groupmate", "url": str}  -- someone in the
    #       group uploaded this key first; url is their copy
    #   {"type": "snapshot_marker", "role": str, "t": "mm:ss", "after_s": int}
    #       -- the addon's snapshot keybind was pressed (docs/SNAPSHOT.md)
    #   {"type": "snapshot_ready", "zone", "level", "role", "t", "path"}
    #       -- its report was written; open_snapshot(path) shows it
    #   {"type": "snapshot_failed", "error": str}
    #   {"type": "snapshot_hotkey", "ok": bool, "message": str}
    #       -- whether the desktop's own hotkey could be set up
    #     -- the full URL (site_url + the site's own path) of the report.
    #   {"type": "scanning", "bytes_read": int, "bytes_total": int}
    #     -- the initial scan of a pre-existing log is in progress. Only
    #        emitted for logs big enough to take a while (every 64MB);
    #        tailing has not begun yet.
    #   {"type": "catch_up_skipped", "count": int, "limit": int}
    #     -- that many finished keys found in the log were NOT replayed,
    #        because only the newest few are (see _CATCH_UP_LIMIT).
    #   {"type": "log_unavailable", "log_path": str}
    #     -- the watched file has gone (deleted, drive disconnected) and
    #        no newer sibling appeared. The watch stays alive and recovers
    #        by itself if the file comes back, but nothing will be picked
    #        up until it does.
    #   {"type": "log_switched", "log_path": str}
    #     -- WoW restarted and began a new session log; the watch followed
    #        it automatically (any key left open in the old log is
    #        reported as run_abandoned first).
    #   {"type": "run_started", "zone": str, "level": int|None}
    #     -- a key's CHALLENGE_MODE_START was just seen; recording it.
    #   {"type": "run_abandoned", "zone": str, "level": int|None}
    #     -- that key was closed without a real end (a different key
    #        started, or WoW's phantom end): nothing to analyze.
    #   {"type": "run_failed", "error": str}
    #     -- this one run's analysis raised; watching continues.
    #   {"type": "analyze_failed", "error": str} / {"type": "upload_failed", "error": str}
    #     -- this one run's analysis/upload came back as a clean failure
    #        (not an exception); watching continues either way.
    #   {"type": "crashed", "error": str}
    #     -- the watch thread itself exited (e.g. log_path stopped being
    #        readable); NOT watching anymore, unlike every event above.
    #   {"type": "stopped"}
    #     -- stop_watch() completed.

    def start_watch(self, params: dict) -> dict:
        """Start live-watching a combat log: as each Mythic+ run
        completes, it's automatically analyzed and uploaded, with no
        further clicks. Runs until ``stop_watch()`` is called or the app
        closes.

        ``params``: ``log_path`` (str, required), ``route``/
        ``dungeon_data_path``/``avoidable_data_path`` (optional, same
        meaning as ``analyze()``'s own params), ``site_url`` (str,
        optional -- defaults to the saved Settings value), ``out_dir``
        (str, optional -- defaults to the saved ``default_output_dir``
        setting, then a per-user app-data folder so this works with zero
        setup).

        Returns ``{"ok": True}`` once the watch thread is actually
        running, or ``{"ok": False, "error": "..."}`` if a watch is
        already active, ``log_path``/a site URL is missing, or route/
        dungeon-data/avoidable-data fail to load. Never raises.
        """
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return {"ok": False, "error": "already watching"}

        params = dict(params or {})
        log_path = params.get("log_path")
        if not log_path:
            return {"ok": False, "error": "log_path is required"}
        # ``log_path`` may be the WoW ``Logs`` folder itself (what the
        # "Choose Logs folder…" pickers store since 2026-09-06) or a log
        # file inside it. Either way, re-resolve against the *current*
        # newest log in that folder rather than trusting a saved filename:
        # a path picked (or auto-started) in an earlier WoW session can
        # point at a filename that will never be written to again on
        # installs that timestamp every session's log instead of reusing
        # a stable "WoWCombatLog.txt" -- see config.resolve_watch_log_path.
        # This is what makes watch_auto_start actually zero-click session
        # over session on those installs instead of silently waiting
        # forever on a stale path (confirmed real 2026-09-01).
        log_path = str(_config.resolve_watch_log_path(_config.watch_log_folder(log_path)))

        settings = _config.load_settings()
        site_url = params.get("site_url") or settings.get("site_url")
        if not site_url:
            return {"ok": False, "error": "no site URL configured -- set one in Settings first"}

        try:
            route = _cli._load_route(params["route"]) if params.get("route") else None
            store = _cli._load_store(
                params.get("dungeon_data_path")
                # zero-config fallback: app data folder, then the packaged copy
                or _config.resolve_dungeon_data_path(settings)
            )
            avoidable = _cli._load_avoidable(
                params.get("avoidable_data_path")
                # zero-config fallback: <config dir>/avoidable_spells.json
                or _config.resolve_avoidable_data_path(_config.load_settings())
            )
            # zero-config fallback: app data folder, then the packaged
            # community-sourced database (see bundled.py) -- Watch Live
            # never loaded this at all before 2026-09-04, so every
            # watched run's kick-efficiency reporting silently ran on the
            # plain "kicked at least once" heuristic regardless of what
            # was configured.
            interrupt_data = _cli._load_effective_interrupt_data(
                params.get("interrupt_data_path")
                or _config.resolve_interrupt_data_path(settings),
                _config.resolve_learned_interrupts_path(settings),
            )
            # zero-config fallback: <config dir>/stealable_spells.json --
            # no packaged copy, see analysis/stealable.py.
            stealable = _cli._load_stealable(
                params.get("stealable_data_path")
                or _config.resolve_stealable_data_path(settings)
            )
        except SystemExit as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        out_dir = params.get("out_dir") or str(_config.resolve_output_dir(settings, "watch-runs"))
        history_db_path = _config.resolve_history_db_path(settings)
        # The installed Postmortem addon folder, derived from the same log
        # path being watched (see addon_results.addon_dir_from_log_path) --
        # None when the addon isn't installed / the layout doesn't match,
        # in which case the in-game writeback is simply skipped.
        from ..addon_results import addon_dir_from_log_path
        addon_dir = addon_dir_from_log_path(log_path)
        # ...and the installed MythicDungeonTools folder, for the route
        # map's background art (mapart.py). None when MDT isn't installed
        # -> reports simply have no background.
        from .. import mapart
        mdt_dir = mapart.mdt_dir_from_log_path(log_path)

        # Two threads, not one. Recorder.watch() (the *tailing* thread)
        # calls on_run_complete synchronously from inside its read loop,
        # so doing the analysis + upload right there stalled log reading
        # for the whole duration -- 37 minutes on one real 100MB+ run
        # (2026-09-02), during which a new key's start went unseen and
        # the UI sat silent. Completed runs are instead handed to a
        # dedicated worker over a queue: tailing stays realtime, runs are
        # still analyzed strictly in order, and "run_started" for the
        # next key shows up the moment it begins.
        work: "queue.Queue" = queue.Queue()
        _STOP = object()

        def on_run_complete(run: Any) -> None:
            work.put(run)

        def worker() -> None:
            while True:
                run = work.get()
                if run is _STOP:
                    return
                # "run_failed" (this one run didn't analyze/upload) is
                # deliberately a different event type than run_watch()'s
                # "crashed" (the whole watch stopped) -- the UI tells
                # "still watching, one run had a problem" apart from "not
                # watching anymore". Keep this resilient: an unhandled
                # exception here would silently kill the worker and every
                # later run would just pile up unprocessed.
                try:
                    self._handle_watched_run(
                        run, route, store, avoidable, site_url, history_db_path, addon_dir,
                        mdt_dir, interrupt_data=interrupt_data, stealable=stealable,
                    )
                except Exception as exc:
                    self._emit_watch_event({"type": "run_failed", "error": str(exc)})

        snapshot_before_s = _snapshot_seconds(settings.get("snapshot_before_s"), 120, 30, 600)
        snapshot_after_s = _snapshot_seconds(settings.get("snapshot_after_s"), 60, 10, 300)

        recorder = Recorder(
            log_path=Path(log_path),
            out_dir=Path(out_dir),
            on_run_start=lambda run: self._emit_watch_event({
                "type": "run_started", "zone": run.zone, "level": run.keystone_level,
            }),
            # The addon's snapshot keybind (docs/SNAPSHOT.md): build the
            # role-focused window report once the "after" part of the
            # window has been logged, from the run's own slice file.
            on_snapshot_marker=lambda run, ts, role: self._on_snapshot_marker(
                run, ts, role, snapshot_before_s, snapshot_after_s, store,
                avoidable, interrupt_data, mdt_dir,
            ),
            # Follow WoW to a new session log mid-watch (a real timed key
            # was lost to a rotation before this, 2026-09-02) and tell the
            # UI which file is being watched now.
            on_log_switched=lambda path: self._emit_watch_event({
                "type": "log_switched", "log_path": str(path),
            }),
            # Catch-up: a key that finished before this watch started (or
            # while the app was closed) is replayed through the normal
            # analyze+upload pipeline unless it has already been dealt
            # with -- so "every finished key gets uploaded" holds across
            # app restarts too, and nothing is ever uploaded twice.
            #
            # "Dealt with" means UPLOADED, not merely present in local
            # history. Analysing a key by hand puts it in history, which
            # used to make catch-up skip it forever, so a key the user
            # analysed but never pressed Upload on could never reach the
            # site (2026-09-11).
            already_processed=lambda zone, ts: _already_uploaded(
                history_db_path, zone, ts),
            on_run_abandoned=lambda run: self._emit_watch_event({
                "type": "run_abandoned", "zone": run.zone, "level": run.keystone_level,
            }),
            on_run_complete=on_run_complete,
            # A first watch on a months-old log found every finished key
            # unprocessed and replayed all of them -- minutes of analysis
            # and an upload each (2026-09-11). Take the newest few and say
            # how many were left.
            max_catch_up_runs=_CATCH_UP_LIMIT,
            on_catch_up_skipped=lambda n: self._emit_watch_event({
                "type": "catch_up_skipped", "count": n,
                "limit": _CATCH_UP_LIMIT,
            }),
            # The initial scan of a pre-existing log reads the whole file
            # before tailing begins; without this the app says it is
            # watching and then sits silent for minutes on a large log.
            on_scan_progress=lambda done, total: self._emit_watch_event({
                "type": "scanning", "bytes_read": done, "bytes_total": total,
            }),
            # A deleted log or a disconnected drive looked exactly like
            # "no keys yet", forever.
            on_log_unavailable=lambda path: self._emit_watch_event({
                "type": "log_unavailable", "log_path": str(path),
            }),
            on_waiting_for_log=lambda: self._emit_watch_event(
                {"type": "waiting_for_log", "log_path": str(log_path)}
            ),
            echo=lambda _msg: None,  # the UI gets structured events instead
        )

        def run_watch() -> None:
            # watch() itself never raises for anything mid-session (every
            # per-line/per-hook failure is already caught internally),
            # but opening log_path for the first time can (missing file,
            # unreadable) -- report that as a "crashed" event (distinct
            # from the worker's "run_failed" above: this means the watch
            # thread itself exited, not just one run) instead of letting
            # the thread die with an unhandled exception no one would see.
            try:
                recorder.watch()
            except Exception as exc:
                self._emit_watch_event({"type": "crashed", "error": str(exc)})

        self._watch_recorder = recorder
        self._start_snapshot_hotkey(settings, snapshot_before_s, snapshot_after_s,
                                    store, avoidable, interrupt_data, mdt_dir)
        self._watch_queue = work
        self._watch_stop_sentinel = _STOP
        self._watch_worker = threading.Thread(
            target=worker, name="postmortem-watch-worker", daemon=True,
        )
        self._watch_worker.start()
        self._watch_thread = threading.Thread(
            target=run_watch, name="postmortem-watch", daemon=True,
        )
        self._watch_thread.start()
        self._emit_watch_event({"type": "watching", "log_path": str(log_path)})
        return {"ok": True}

    def stop_watch(self) -> dict:
        """Stop a watch started by ``start_watch()``. Returns
        ``{"ok": True}`` whether or not a watch was actually running
        (idempotent, matching this codebase's other start/stop
        conventions -- e.g. the addon's own combat-logging toggle).
        Blocks briefly (up to ``poll_interval``, 0.5s by default) for the
        watch thread to actually exit. Never raises."""
        if self._watch_recorder is not None:
            self._watch_recorder.request_stop()
        for timer in list(self._snapshot_timers.values()):
            timer.cancel()
        self._snapshot_timers.clear()
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None
        still_running = False
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=5.0)
            still_running = self._watch_thread.is_alive()
        # Let the worker finish whatever run it's on (it's daemon, so it
        # can't outlive the app), then tell it to exit once the queue
        # drains. Joined only briefly: a run mid-analysis can take
        # minutes, and Stop must never freeze the UI for that.
        if self._watch_queue is not None:
            self._watch_queue.put(self._watch_stop_sentinel)
        if self._watch_worker is not None:
            self._watch_worker.join(timeout=1.0)
        if still_running:
            # The watch thread is mid catch-up (each replayed key can take
            # minutes) and has not seen the stop flag yet. Clearing the
            # state here is what made this unrecoverable: the recorder
            # reference went away so a second Stop could do nothing, and
            # start_watch()'s "is a thread already running" guard passed --
            # so a SECOND recorder could tail the same log and duplicate
            # every slice, analysis and upload (2026-09-11).
            #
            # Keep the handles, report honestly, and let the next Stop
            # finish the job once the thread notices.
            self._emit_watch_event({
                "type": "stopping",
                "detail": "finishing the run it is on -- this can take a few minutes",
            })
            return {"ok": True, "stopping": True}
        self._watch_recorder = None
        self._watch_thread = None
        self._watch_queue = None
        self._watch_worker = None
        self._emit_watch_event({"type": "stopped"})
        return {"ok": True}

    # -- default routes ---------------------------------------------------------

    def add_default_route(self, text: str) -> dict:
        """Save an MDT export string as the default route for the dungeon
        it belongs to (the string carries MDT's dungeon index itself, so
        nothing has to be labeled). One entry per dungeon: pasting a
        second route for the same dungeon replaces the first.

        Returns ``{"ok": True, "default_routes": [...]}`` (the full saved
        list, for the UI to re-render) or ``{"ok": False, "error": ...}``
        for an undecodable string. Never raises.
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "paste an MDT export string first"}
        try:
            route = Route.from_preset(decode_mdt_string(text))
        except (MDTDecodeError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "error": f"that doesn't decode as an MDT export string: {exc}"}
        if route.dungeon_idx is None:
            return {"ok": False, "error": "that route doesn't say which dungeon it's for"}

        # Fill in the ids/name a run will be matched on later, from
        # whatever dungeon data is available *now* -- so matching never
        # needs a data file at analysis time.
        settings = _config.load_settings()
        name: Optional[str] = None
        map_id: Optional[int] = None
        data_path = _config.resolve_dungeon_data_path(settings)
        if data_path is not None:
            try:
                data = _cli._load_store(str(data_path)).by_dungeon_idx(route.dungeon_idx)
            except Exception:
                data = None
            if data is not None:
                name, map_id = data.name, data.map_id

        entry = {
            "dungeon_idx": route.dungeon_idx,
            "dungeon_name": name,
            "challenge_map_id": map_id,
            "route_name": route.name or None,
            "route": text,
        }
        routes = [
            r for r in (settings.get("default_routes") or [])
            if isinstance(r, dict) and r.get("dungeon_idx") != route.dungeon_idx
        ]
        routes.append(entry)
        routes.sort(key=lambda r: (r.get("dungeon_name") or "", r.get("dungeon_idx") or 0))
        settings["default_routes"] = routes
        try:
            _config.save_settings(settings)
        except OSError as exc:
            return {"ok": False, "error": f"could not save settings: {exc}"}
        return {"ok": True, "default_routes": routes}

    def remove_default_route(self, dungeon_idx: int) -> dict:
        """Drop the saved default route for one dungeon. Idempotent."""
        settings = _config.load_settings()
        routes = [
            r for r in (settings.get("default_routes") or [])
            if isinstance(r, dict) and r.get("dungeon_idx") != dungeon_idx
        ]
        settings["default_routes"] = routes
        try:
            _config.save_settings(settings)
        except OSError as exc:
            return {"ok": False, "error": f"could not save settings: {exc}"}
        return {"ok": True, "default_routes": routes}

    def sync_keystone_guru_routes(self, profile: str) -> dict:
        """Pull every published route on a Keystone.guru profile into the
        per-dungeon defaults (see keystoneguru.py for the two public
        endpoints this uses -- no API key, no login).

        One default per dungeon: the first listed route for a dungeon is
        taken, later ones for the same dungeon are reported as skipped so
        the user can see what was and wasn't used. A single route that
        fails to export/decode is skipped with its reason, never fatal.

        Returns ``{"ok": True, "added": [...], "skipped": [...],
        "default_routes": [...]}`` or ``{"ok": False, "error": ...}``
        for a bad profile / unreachable site. Never raises.
        """
        from .. import keystoneguru as kg

        try:
            user_id = kg.parse_profile_id(profile)
            routes = kg.list_public_routes(user_id)
        except kg.KeystoneGuruError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # never raise across the bridge
            return {"ok": False, "error": f"Keystone.guru sync failed: {exc}"}

        added: list[dict] = []
        skipped: list[dict] = []
        seen_dungeons: set[str] = set()
        for r in routes:
            label = f"{r.get('dungeon_name') or r.get('dungeon_slug') or '?'} — {r.get('title') or r['public_key']}"
            if not r.get("mdt_supported", True):
                skipped.append({"route": label, "reason": "dungeon not supported by MDT"})
                continue
            slug = r.get("dungeon_slug") or r["public_key"]
            if slug in seen_dungeons:
                skipped.append({"route": label, "reason": "already have a route for this dungeon"})
                continue
            try:
                text = kg.fetch_mdt_string(r["public_key"])
            except kg.KeystoneGuruError as exc:
                skipped.append({"route": label, "reason": str(exc)})
                continue
            result = self.add_default_route(text)
            if result.get("ok"):
                seen_dungeons.add(slug)
                added.append({"route": label})
            else:
                skipped.append({"route": label, "reason": result.get("error", "could not add")})

        return {
            "ok": True,
            "added": added,
            "skipped": skipped,
            "default_routes": _config.load_settings().get("default_routes") or [],
        }

    def _default_route_for(self, challenge_map_id, zone_name, store) -> Optional[Route]:
        """The saved default route for a run, decoded -- or None. Any
        problem (no match, a string that no longer decodes) just means
        "no route", never an error: a default is a convenience, not a
        requirement. Applied wherever a run has no explicit route: Watch
        Live (per run, since one watch spans many dungeons) and one-off
        analysis."""
        settings = _config.load_settings()
        dungeon_idx = None
        if store is not None and challenge_map_id is not None:
            data = store.by_challenge_map_id(challenge_map_id)
            if data is not None:
                dungeon_idx = data.dungeon_idx
        text = _config.resolve_default_route(
            settings, challenge_map_id=challenge_map_id,
            zone_name=zone_name, dungeon_idx=dungeon_idx,
        )
        if not text:
            return None
        try:
            return Route.from_preset(decode_mdt_string(text))
        except Exception:
            return None

    def resolve_wow_log_path(self, folder: str) -> str:
        """The log file to watch inside a WoW ``Logs`` folder the user
        just picked: see ``config.resolve_watch_log_path`` for why this
        can't just be ``folder + "WoWCombatLog.txt"`` (some WoW installs
        never write that plain filename at all). Never raises -- an
        unreadable/nonexistent folder just falls back to that plain-name
        guess, same as before this existed."""
        return str(_config.resolve_watch_log_path(folder))

    # -- snapshot keybind (docs/SNAPSHOT.md) ---------------------------------

    def _start_snapshot_hotkey(self, settings, before_s, after_s, store, avoidable,
                               interrupt_data, mdt_dir) -> None:
        """The app's own snapshot trigger (desktop/hotkey.py): a global
        key combination that timestamps the press here, so the log is
        never touched. Best-effort -- a hotkey that cannot be set up is
        reported to the watch log and Watch Live carries on."""
        from .hotkey import HotkeyListener

        combo = str(settings.get("snapshot_hotkey") or "").strip()
        if not combo:
            return
        focus = str(settings.get("snapshot_focus") or "healer").strip().lower()
        if focus not in ("healer", "tank", "general"):
            focus = "healer"
        character = str(settings.get("snapshot_character") or "").strip()

        def on_press() -> None:
            recorder = self._watch_recorder
            run = getattr(recorder, "_current", None) if recorder is not None else None
            if run is None:
                self._emit_watch_event({
                    "type": "snapshot_failed",
                    "error": "hotkey pressed, but no key is being recorded right now",
                })
                return
            self._on_snapshot_marker(run, time.time(), focus, before_s, after_s, store,
                                     avoidable, interrupt_data, mdt_dir,
                                     focus_name=character or None, source="hotkey")

        listener = HotkeyListener(combo, on_press)
        ok, message = listener.start()
        self._hotkey = listener if ok else None
        # ``focus`` rides along so the Watch screen can say what a press
        # will build ("tank snapshot"), not just that a key is armed.
        self._emit_watch_event({"type": "snapshot_hotkey", "ok": ok, "message": message,
                                "focus": character or focus})

    def _on_snapshot_marker(self, run, marker_ts: float, role: str,
                            before_s: int, after_s: int, store, avoidable,
                            interrupt_data, mdt_dir, focus_name=None,
                            source: str = "marker") -> None:
        """Called on the tailing thread the moment a marker cluster is
        complete. The report needs the ``after_s`` seconds that follow, so
        the build is scheduled that far ahead (plus a little slack for
        WoW's write buffering) on a timer thread; the run's slice file is
        still being appended to and is re-read then."""
        rel = max(0.0, marker_ts - run.started_at) if getattr(run, "started_at", None) else 0.0
        self._emit_watch_event({
            "type": "snapshot_marker", "role": role, "t": _mmss(rel), "after_s": after_s,
        })
        key = (str(run.path), round(marker_ts, 1))
        if key in self._snapshot_timers:
            return
        timer = threading.Timer(
            after_s + 5.0,
            self._build_snapshot_for_marker,
            args=(run, marker_ts, role, before_s, after_s, store, avoidable,
                  interrupt_data, mdt_dir, key),
            kwargs={"focus_name": focus_name, "source": source},
        )
        timer.daemon = True
        self._snapshot_timers[key] = timer
        timer.start()

    def _build_snapshot_for_marker(self, run, marker_ts, role, before_s, after_s,
                                   store, avoidable, interrupt_data, mdt_dir, key,
                                   focus_name=None, source: str = "marker") -> None:
        self._snapshot_timers.pop(key, None)
        try:
            from ..analysis.snapshot import build_snapshot
            from ..combatlog.parser import parse_file
            from ..combatlog.segmenter import segment_runs
            from ..report.snapshot import render_snapshot_html

            segment = None
            for seg in segment_runs(parse_file(str(run.path))):
                if seg.start_ts <= marker_ts <= (seg.end_ts or float("inf")):
                    segment = seg
                    break
                segment = seg  # the slice file holds one run; keep the last
            if segment is None:
                raise ValueError("no run found in the recorded slice")
            report = build_snapshot(
                segment, marker_ts, before_s=before_s, after_s=after_s, role=role,
                store=store, avoidable=avoidable, interrupt_data=interrupt_data,
                focus_name=focus_name, source=source,
            )
            role = report.get("snapshot", {}).get("role") or role
            n = 1
            while (Path(run.path).with_name(f"{Path(run.path).stem}-snapshot-{n}.html")).exists():
                n += 1
            out = Path(run.path).with_name(f"{Path(run.path).stem}-snapshot-{n}.html")
            out.write_text(render_snapshot_html(report), encoding="utf-8")
            rel = report.get("snapshot", {}).get("t_marker")
            self._emit_watch_event({
                "type": "snapshot_ready", "zone": run.zone, "level": run.keystone_level,
                "role": role, "t": _mmss(rel if rel is not None else marker_ts - segment.start_ts),
                "path": str(out),
            })
        except Exception as exc:
            self._emit_watch_event({"type": "snapshot_failed", "error": str(exc)})

    def open_snapshot(self, path: str) -> dict:
        """Load a snapshot report written by Watch Live (or the CLI) for
        the report screen: ``{"ok": True, "html", "label"}``. Only a file
        named like our own ``*-snapshot-N.html`` is read -- the path
        arrives from the UI, which got it from our own event, but the
        bridge does not take that on trust."""
        try:
            p = Path(str(path))
            if not re.fullmatch(r".*-snapshot-\d+\.html", p.name) or not p.is_file():
                return {"ok": False, "error": "that is not a snapshot report"}
            html = p.read_text(encoding="utf-8")
            return {"ok": True, "html": html, "label": f"Snapshot — {p.stem}"}
        except Exception as exc:  # never raise across the bridge
            return {"ok": False, "error": f"could not open the snapshot: {exc}"}

    def _handle_watched_run(self, run, route, store, avoidable, site_url,
                            history_db_path, addon_dir=None, mdt_dir=None,
                            interrupt_data=None, stealable=None) -> None:
        """One completed run, from Recorder's ``on_run_complete``:
        analyze it and upload it automatically, pushing progress to the
        UI as each step happens. Reuses ``cli.py``'s own
        ``_write_recorded_reports`` (not reimplemented) so this writes
        the exact same JSON/HTML/text/chapters files the CLI's
        ``record --analyze`` does, then uploads via the same
        ``upload.upload_report()`` every other upload path in this app
        uses.
        """
        self._emit_watch_event({
            "type": "run_complete", "zone": run.zone, "level": run.keystone_level,
        })

        if route is None:
            # No route pasted on the Watch Live screen: use the saved
            # default for *this run's* dungeon. Resolved per run, not once
            # in start_watch, because one watch spans a whole session of
            # different dungeons.
            route = self._default_route_for(run.challenge_map_id, run.zone, store)

        # Pass avoidable through: start_watch loads (and validates) the
        # avoidable-damage data file, but until 2026-09-01 this call
        # dropped it on the floor -- a Watch Live run silently produced
        # no avoidable-damage breakdown even with the file set in the UI.
        from .. import mapart
        report = _cli._write_recorded_reports(
            run, route, store, avoidable=avoidable, interrupt_data=interrupt_data,
            stealable=stealable,
            learned_path=_config.resolve_learned_interrupts_path(_config.load_settings()),
            spell_damage_history_path=_config.resolve_learned_spell_damage_path(
                _config.load_settings()),
            # Read fresh per run rather than once per watch session: the
            # capture is written by a client that is still playing, so a
            # key finished twenty minutes into a session has a newer
            # spellbook than the one that existed when Watch Live started.
            tank_knowledge=_load_tank_knowledge_quietly(),
            # embed the floor's map art from the user's MDT install into the
            # local report (best-effort; upload.py strips it before the site)
            enrich=lambda r: mapart.attach_map_backgrounds(r, mdt_dir, store),
        )
        if report is None:
            self._emit_watch_event({
                "type": "analyze_failed",
                "error": "no run found in the recorded slice",
            })
            return

        self._emit_watch_event({
            "type": "analyzed",
            "zone": report["run"].get("zone"),
            "level": report["run"].get("keystone_level"),
            "timed": report["run"].get("timed"),
        })

        # Best-effort, same reasoning as _save_report_locally's own try/
        # except: a Watch Live run should land in the same local history
        # a "New Analysis" run does (one unified History screen, not two
        # separate silos), but a database hiccup here must never stop
        # the run from still uploading below.
        try:
            base = run.path.with_suffix("")
            from ..history.store import ingest as ingest_history
            ingest_history(
                report, history_db_path,
                source_path=f"{base}.json", html_path=f"{base}.html",
            )
        except Exception:
            pass

        # Best-effort in-game writeback: drop the crunched headline stats
        # into the addon folder as PostmortemResults.lua, which the addon
        # reads on the player's next /reload (see addon_results). Skipped
        # cleanly when the addon isn't installed (addon_dir is None), and
        # a write failure never blocks the upload below.
        if addon_dir is not None:
            try:
                from ..addon_results import write_addon_results
                write_addon_results(report, addon_dir)
                self._emit_watch_event({
                    "type": "results_written",
                    "zone": report["run"].get("zone"),
                    "level": report["run"].get("keystone_level"),
                })
            except Exception:
                pass

        from .. import upload as _upload
        result = _upload.upload_report(report, site_url)
        groupmate_url = _upload.duplicate_of(result, site_url)
        if result.get("ok") or groupmate_url:
            # ...and record that it got there, so catch-up knows this run
            # needs no further work (see already_processed above). A key
            # a groupmate uploaded first IS there -- marking it too is
            # what stops every restart from re-trying it (2026-09-14).
            try:
                _mark_uploaded(history_db_path, report["run"].get("zone"),
                               report["run"].get("start_ts"))
            except Exception:
                pass
            if groupmate_url:
                self._emit_watch_event({
                    "type": "uploaded_by_groupmate", "url": groupmate_url,
                })
            else:
                self._emit_watch_event({
                    "type": "uploaded",
                    "url": f"{site_url.rstrip('/')}{result.get('url', '')}",
                })
        else:
            # A response with no error key rendered as the word "null" in
            # the watch log.
            self._emit_watch_event({
                "type": "upload_failed",
                "error": result.get("error") or "the site rejected the upload",
            })

    def _emit_watch_event(self, event: dict) -> None:
        """Push a live status update to the UI (``window.onWatchEvent``
        in shell/app.js). Best-effort: with no active window (closed
        mid-watch, or pywebview not running at all -- e.g. under test),
        the event is just dropped rather than raising. This always runs
        on the background watch thread, which must never crash the app.
        A separate method (rather than inlined at each call site) so
        tests can monkeypatch it to capture emitted events without a
        real pywebview window.
        """
        try:
            import json as _json
            import webview
            webview.windows[0].evaluate_js(f"window.onWatchEvent({_json.dumps(event)})")
        except Exception:
            pass

    # -- auto-update ----------------------------------------------------------
    #
    # Same "runs on a background thread, progress pushed via events"
    # shape as watch mode above (see that section's own comment) --
    # downloading+applying an update takes real time and ends with this
    # process exiting itself, so there's no single return value that
    # could describe the whole operation.
    #
    # Events (window.onUpdateEvent in shell/app.js):
    #   {"type": "downloading", "written": int, "total": int|None}
    #     -- streamed periodically while the update zip downloads.
    #     "total" is None when the server didn't send a Content-Length.
    #   {"type": "applying"}
    #     -- download done, extracting and validating the new build.
    #   {"type": "relaunching"}
    #     -- the swap is handed off to a detached helper; this process
    #        is about to exit. The UI should show this as "success" --
    #        there's no further event coming.
    #   {"type": "failed", "error": str}
    #     -- the download, extraction, or validation failed. The
    #        current install was never touched (the swap only happens
    #        after everything downloaded and validated cleanly).

    def check_for_update(self) -> dict:
        """Check GitHub for a newer ``alpha-desktop-N`` build than the
        one currently running. Returns ``{"ok": True, "update": {...}}``
        (see ``updater.check_for_update()``'s own docstring for the
        dict's shape) when one's available, or
        ``{"ok": True, "update": None}`` when there isn't one -- a dev
        build, no network, and "already on the latest" all look like
        this alike, since none of them are errors. Never raises.
        """
        try:
            channel = self._update_channel()
            return {"ok": True, "update": _updater.check_for_update(channel=channel),
                    "channel": channel}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _update_channel() -> str:
        """The release channel from settings ("stable" unless the user
        opted into "beta" -- docs/RELEASE_CHANNELS.md), falling back to
        stable for anything unrecognised. Shared by check_for_update()
        and start_update() so both resolve against the *same* channel:
        the two used to disagree (start_update() re-resolved on the
        stable default), which made every beta-channel update fail with
        "no longer matches the published release" (2026-09-17).
        """
        channel = str(_config.load_settings().get("update_channel") or "stable")
        return channel if channel in _updater.CHANNELS else "stable"

    def start_update(self, download_url: str) -> dict:
        """Start downloading and applying an update (the
        ``download_url`` from a prior ``check_for_update()`` call).
        Returns ``{"ok": True}`` once the update thread is running, or
        ``{"ok": False, "error": "..."}`` if one's already in progress,
        this isn't a packaged build (nothing to self-update out of a
        source checkout), or ``download_url`` isn't a trusted GitHub
        host. Never raises -- everything past that point is reported via
        ``window.onUpdateEvent`` instead, since this process exits
        itself on success.
        """
        if self._update_thread is not None and self._update_thread.is_alive():
            return {"ok": False, "error": "an update is already in progress"}
        if not getattr(sys, "frozen", False):
            return {"ok": False, "error": "auto-update only works in a packaged build"}
        if not _updater._is_trusted_download_url(download_url):
            return {"ok": False, "error": "refusing to download from an untrusted source"}
        # Before downloading anything: an install this account can't
        # replace (Program Files) used to download, report success, and
        # relaunch the old build unchanged -- every single time.
        if not _updater.install_location_writable():
            return {"ok": False, "error": _updater.unwritable_install_message()}

        def run_update() -> None:
            try:
                work_dir = Path(tempfile.mkdtemp(prefix="postmortem-update-dl-"))

                def on_progress(p: dict) -> None:
                    self._emit_update_event({"type": "downloading", **p})

                # Re-resolve the release from the API rather than
                # trusting anything the page passed in. start_update() is
                # a bridge method, so its argument is only as trustworthy
                # as whatever is rendering in that window -- and an
                # attacker who could choose the URL could otherwise also
                # choose the digest it is checked against, which would
                # make the check theatre. The URL is required to match
                # what the API itself reports for the current release --
                # on the same channel the check ran on, or a beta build
                # never matches the stable release it's compared against.
                available = _updater.check_for_update(channel=self._update_channel())
                if not available or available.get("download_url") != download_url:
                    self._emit_update_event({
                        "type": "failed",
                        "error": "this update no longer matches the published release",
                    })
                    return
                if available.get("sha256_error"):
                    # The release publishes a checksum that could not be
                    # read. Installing anyway would skip the one check
                    # standing between a substituted archive and a relaunch.
                    self._emit_update_event({
                        "type": "failed",
                        "error": ("could not verify this update's published checksum ("
                                  f"{available['sha256_error']}) -- try again later"),
                    })
                    return
                new_install = _updater.perform_update(
                    download_url, work_dir, on_progress=on_progress,
                    expected_sha256=available.get("sha256"),
                )
                self._emit_update_event({"type": "applying"})
                _updater.apply_update_and_relaunch(new_install)
                self._emit_update_event({"type": "relaunching"})
                time.sleep(1.5)  # let the UI actually render that message first
                os._exit(0)
            except Exception as exc:
                self._emit_update_event({"type": "failed", "error": str(exc)})

        self._update_thread = threading.Thread(
            target=run_update, name="postmortem-update", daemon=True,
        )
        self._update_thread.start()
        return {"ok": True}

    def _emit_update_event(self, event: dict) -> None:
        """Push a live status update to the UI (``window.onUpdateEvent``
        in shell/app.js). Best-effort, same reasoning as
        ``_emit_watch_event`` above."""
        try:
            import json as _json
            import webview
            webview.windows[0].evaluate_js(f"window.onUpdateEvent({_json.dumps(event)})")
        except Exception:
            pass

    # -- settings -----------------------------------------------------------

    def get_version(self) -> dict:
        """The build tag this running app was stamped with (see
        ``_version.py`` -- ``"dev"`` for a source checkout). Purely
        informational: shown in Settings so there's some visible answer
        to "which build am I on", which otherwise has no answer anywhere
        in the UI even though it's exactly the thing the update banner
        (see ``check_for_update``) is comparing against. Never raises.
        """
        from ._version import VERSION
        return {"ok": True, "version": VERSION}

    # -- account linking (phase 3 of ACCOUNTS_AND_PROGRESSION_PLAN.md) ------
    #
    # The app never touches Battle.net or a password: it asks the site for
    # a short code (start_device_link), shows it and opens the site's own
    # confirmation page in the user's browser, then polls
    # (poll_device_link) until they confirm it there while already signed
    # in. account_status is the passive check (e.g. on Settings load) for
    # whether that has already happened.

    def open_url(self, url: str) -> dict:
        """Open ``url`` in the system's default browser (used to send the
        user to the site's device-link confirmation page -- a pywebview
        window is not a normal browser tab, so a plain link/JS navigation
        would hijack the app's own UI instead). ``http(s)://`` only, to
        avoid the bridge being used to launch an arbitrary local scheme.
        Returns ``{"ok": True}`` or ``{"ok": False, "error": "..."}``.
        Never raises.
        """
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "error": "only http(s) URLs may be opened"}
        # Same origin as the configured site, too. The one caller opens a
        # verify_url that comes straight out of the site's own response,
        # so a compromised (or mistyped) site could otherwise hand the app
        # any address and get a drive-by navigation out of a click the
        # user believes goes to Postmortem.
        import urllib.parse

        try:
            configured = _normalized_site_url(_config.load_settings().get("site_url"))
        except (ValueError, OSError):
            configured = ""
        if not configured:
            return {"ok": False, "error": "no site URL is configured"}
        want = urllib.parse.urlsplit(configured)
        got = urllib.parse.urlsplit(url)
        if (got.scheme.lower(), got.netloc.lower()) != (want.scheme.lower(), want.netloc.lower()):
            return {"ok": False,
                    "error": "refusing to open a URL outside the configured site"}
        try:
            import webbrowser
            webbrowser.open(url)
            return {"ok": True}
        except Exception as exc:  # pragma: no cover -- platform browser launch
            return {"ok": False, "error": str(exc)}

    def start_device_link(self, params: Optional[dict] = None) -> dict:
        """Begin linking this install's upload token to a Postmortem
        account. ``params``: ``site_url`` (str, optional -- defaults to
        the saved Settings value).

        Returns ``{"ok": True, "code", "verify_url", "poll_token",
        "expires_in"}`` -- show ``code``, open ``verify_url``, then poll
        with ``poll_device_link(poll_token)`` -- or ``{"ok": False,
        "error": "..."}`` if no site URL is configured or the site
        doesn't answer. Never raises.
        """
        settings = _config.load_settings()
        try:
            site_url = _checked_site_url((params or {}).get("site_url"),
                                         settings.get("site_url"))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        import socket

        from .. import upload as _upload
        label = f"desktop app on {socket.gethostname()}"
        return _upload.start_device_link(site_url, label=label)

    def poll_device_link(self, params: dict) -> dict:
        """Check a code started with ``start_device_link``. ``params``:
        ``poll_token`` (str, required), ``site_url`` (str, optional).

        Returns ``{"status": "pending"}``, ``{"status": "approved",
        "display_name": "..."}``, ``{"status": "expired"}``, or
        ``{"status": "error", "error": "..."}``. Never raises -- meant
        to be polled on a short timer (e.g. every few seconds) until the
        status stops being ``"pending"``.
        """
        params = params or {}
        poll_token = params.get("poll_token")
        if not poll_token:
            return {"status": "error", "error": "poll_token is required"}
        try:
            site_url = _checked_site_url(params.get("site_url"),
                                         _config.load_settings().get("site_url"))
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        from .. import upload as _upload
        return _upload.poll_device_link(site_url, poll_token)

    def account_status(self) -> dict:
        """Whether this install's upload token is already linked to a
        Postmortem account (e.g. shown once in Settings on load).

        Returns ``{"ok": True, "linked": bool, "display_name":
        Optional[str]}``. With no site URL configured yet, this is
        always ``{"ok": True, "linked": False}`` rather than an error --
        "no account linked" is simply true in that case, same as before
        any site existed. Never raises.
        """
        site_url = _config.load_settings().get("site_url")
        if not site_url:
            return {"ok": True, "linked": False}
        from .. import upload as _upload
        result = _upload.whoami(site_url)
        return {"ok": True, "linked": bool(result.get("linked")),
                "display_name": result.get("display_name")}

    def send_feedback(self, params: dict) -> dict:
        """Send feedback from the Feedback screen. ``params``:
        ``message`` (str, required), ``kind`` ("bug"/"idea"/"other"),
        ``contact`` (str, optional). The app's version is attached here
        rather than trusted from the page.

        Returns ``{"ok": True}`` or ``{"ok": False, "error": "..."}``.
        Never raises.
        """
        params = params if isinstance(params, dict) else {}
        message = str(params.get("message") or "").strip()
        if not message:
            return {"ok": False, "error": "Write your feedback first."}
        from .. import upload as _upload
        from ._version import VERSION
        return _upload.send_feedback(
            message, kind=str(params.get("kind") or "other"),
            contact=str(params.get("contact") or ""), source="app", version=VERSION,
        )

    def get_settings(self) -> dict:
        """Return persisted desktop settings (see ``desktop/config.py``),
        merged with defaults for any field never saved. Always succeeds
        -- ``config.load_settings()`` is itself tolerant of a missing or
        corrupt settings file."""
        return _config.load_settings()

    def validate_hotkey(self, text: str) -> dict:
        """Whether ``text`` is a hotkey the listener would accept:
        ``{"ok": True, "combo": "ctrl+alt+s"}`` (the normalized form) or
        ``{"ok": False, "error": "..."}``. An empty string is fine -- it
        means "no hotkey". The Settings screen calls this as the user
        types so a bare key is flagged before Save. Never raises."""
        from .hotkey import parse_combo
        raw = str(text or "").strip()
        if not raw:
            return {"ok": True, "combo": ""}
        try:
            return {"ok": True, "combo": str(parse_combo(raw))}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def get_settings_status(self) -> dict:
        """Whether the last settings load fell back to defaults because the
        file could not be read.

        ``{"reset": False}`` normally, or ``{"reset": True, "kept_path":
        "...", "message": "..."}`` when a torn or corrupt settings file was
        moved aside. The interface shows this on the home screen: resetting
        in silence lost the site URL and left Watch Live refusing to start
        with nothing to explain it (2026-09-11). Never raises.
        """
        try:
            error = _config.last_load_error()
        except Exception:
            return {"reset": False}
        if not error:
            return {"reset": False}
        kept, message = error
        return {"reset": True, "kept_path": kept, "message": message}

    def save_settings(self, settings: dict) -> dict:
        """Persist desktop settings (see ``desktop/config.py``).

        Returns ``{"ok": True}`` on success, or
        ``{"ok": False, "error": "..."}`` if writing failed (e.g. an
        unwritable config directory) or a value was rejected. Never
        raises.

        ``site_url`` is checked here because this method can permanently
        repoint every future upload, and it is reachable from any script
        running in the app window (2026-09-11). Changing it is the user's
        own decision, so the check is what it can be: a real http(s)
        address, not a ``javascript:``/``file:`` URL or junk.
        """
        settings = dict(settings or {})
        if settings.get("snapshot_hotkey"):
            # A hotkey the listener would refuse (a bare "`", say) used to
            # save fine and only fail as one line in the watch log when
            # Watch Live started (2026-09-17: a whole evening of presses
            # that never armed anything). Refuse it here, with the same
            # message, so the Settings screen says so at save time.
            from .hotkey import parse_combo
            try:
                parse_combo(str(settings["snapshot_hotkey"]))
            except ValueError as exc:
                return {"ok": False, "error": f"Snapshot hotkey: {exc}"}
        if "site_url" in settings:
            raw = settings["site_url"]
            if raw in (None, ""):
                settings["site_url"] = None
            else:
                try:
                    settings["site_url"] = _normalized_site_url(raw)
                except ValueError as exc:
                    return {"ok": False, "error": str(exc)}
        try:
            _config.save_settings(settings)
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    # -- dungeon data extraction --------------------------------------------

    def extract_dungeon_data(self, addon_path: str, output_path: str) -> dict:
        """Extract dungeon/enemy data from a Mythic Dungeon Tools addon
        folder into a JSON file at ``output_path``. Mirrors ``cli.py``'s
        ``cmd_extract_data``, wrapping ``mdt.extract.write_dungeon_data``.

        Returns ``{"ok": True, "output_path": ..., "dungeon_count": N,
        "dungeons": [{"dungeon_idx", "name", "enemy_count"}, ...]}`` --
        the same per-dungeon info ``cmd_extract_data`` prints, sorted by
        dungeon_idx -- on success. Returns
        ``{"ok": False, "error": "..."}`` if ``addon_path`` isn't a
        valid directory or extraction otherwise fails (e.g. an
        unwritable ``output_path``). Never raises.
        """
        try:
            payload = write_dungeon_data(addon_path, output_path)
        except (OSError, ValueError, KeyError) as exc:
            return {"ok": False, "error": str(exc)}
        dungeons = sorted(payload["dungeons"].values(), key=lambda d: d["dungeon_idx"])
        return {
            "ok": True,
            "output_path": str(output_path),
            "dungeon_count": len(dungeons),
            "dungeons": [
                {
                    "dungeon_idx": d["dungeon_idx"],
                    "name": d["name"],
                    "enemy_count": len(d["enemies"]),
                }
                for d in dungeons
            ],
        }

    # -- native file/folder dialogs ------------------------------------------
    #
    # pywebview is intentionally imported locally (inside each method
    # body) rather than at module scope, so importing this module -- and
    # calling every other method on it -- never requires pywebview to be
    # installed.
    #
    # Verified against a real pywebview 6.x install (pip install pywebview
    # in an isolated venv, orchestrator review pass): webview.FileDialog.
    # OPEN/FOLDER/SAVE, webview.windows (a plain list), and
    # create_file_dialog(dialog_type, directory, allow_multiple,
    # save_filename, file_types) -> Sequence[str] | None all match exactly
    # what's used below. These five methods are still the only ones in
    # this file that can't be meaningfully unit tested (no fake dialogs to
    # trigger) -- confirm the actual dialog UX by hand once this runs
    # inside a real pywebview window (WP-3).

    def pick_log_file(self) -> Optional[str]:
        """Native "open file" dialog for choosing a WoWCombatLog.txt.
        Returns the chosen path, or ``None`` if the user canceled (or no
        pywebview window is active). Requires a running pywebview app
        (``webview.windows[0]``) -- not usable outside of it."""
        import webview  # local import: see module-level caveat above

        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN,
            file_types=("Combat log files (*.txt)", "All files (*.*)"),
        )
        return result[0] if result else None

    def pick_route_file(self) -> Optional[str]:
        """Native "open file" dialog for choosing a file containing an
        MDT route export string. Returns the chosen path, or ``None`` if
        the user canceled. See ``pick_log_file`` for the pywebview-API
        caveat that also applies here."""
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN,
            file_types=("Route files (*.txt)", "All files (*.*)"),
        )
        return result[0] if result else None

    def pick_folder(self, title: str = "") -> Optional[str]:
        """Native "choose folder" dialog (e.g. for an MDT addon folder,
        or a reports directory). Returns the chosen path, or ``None`` if
        the user canceled. See ``pick_log_file`` for the pywebview-API
        caveat that also applies here."""
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.FOLDER,
        )
        return result[0] if result else None

    def pick_dungeon_data_file(self) -> Optional[str]:
        """Native "open file" dialog for choosing an extracted dungeon
        data JSON file. Returns the chosen path, or ``None`` if the user
        canceled. See ``pick_log_file`` for the pywebview-API caveat
        that also applies here."""
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN,
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        return result[0] if result else None

    def pick_avoidable_data_file(self) -> Optional[str]:
        """Native "open file" dialog for choosing an avoidable-damage
        tagging JSON file. Returns the chosen path, or ``None`` if the
        user canceled. See ``pick_log_file`` for the pywebview-API
        caveat that also applies here."""
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN,
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        return result[0] if result else None
