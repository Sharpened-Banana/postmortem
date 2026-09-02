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
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from .. import cli as _cli
from ..combatlog.parser import parse_file
from ..combatlog.segmenter import segment_runs
from ..mdt.decode import MDTDecodeError, decode_mdt_string
from ..mdt.extract import write_dungeon_data
from ..mdt.route import Route
from ..recorder import Recorder
from ..report.html import render_html
from ..report.index import collect_reports, render_index
from . import config as _config
from . import updater as _updater


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
        # Auto-update state (see check_for_update/start_update below).
        self._update_thread: Optional[threading.Thread] = None

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
        - ``pull_gap_seconds`` (float, default 5.0).
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

        run_selector = str(params.get("run_selector") or "last")
        segment = _cli._pick_run(segment_runs(parse_file(log_path)), run_selector)
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
            pull_gap_seconds=float(params.get("pull_gap_seconds", 5.0)),
            full_cast_timeline=bool(params.get("full_cast_timeline", True)),
            death_penalty_s=float(params.get("death_penalty_s", 15.0)),
            par_ms=par_ms,
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

        html = render_html(report)
        saved = self._save_report_locally(report, html)
        return {"ok": True, "report": report, "html": html, "saved": saved}

    def _save_report_locally(self, report: dict, html: str) -> Optional[dict]:
        """Best-effort: write this analyzed report's JSON/HTML next to
        every other locally-saved report and ingest it into the local
        run-history database, so a "New Analysis" run shows up on the
        History screen exactly like a Watch Live run already does --
        with zero required setup (see ``config.resolve_output_dir``/
        ``resolve_history_db_path``'s own "works with zero setup"
        fallback, the same philosophy ``start_watch()`` already
        established for its own recorded-run output).

        Returns ``{"json_path", "html_path", "run_id"}`` on success, or
        ``None`` if saving failed for any reason (an unwritable
        directory, a locked database, ...) -- this must never block
        showing the report itself, the same "best-effort bonus step"
        philosophy as Raider.io enrichment and site uploads elsewhere in
        this codebase.
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
            return {"json_path": str(json_path), "html_path": str(html_path), "run_id": run_id}
        except Exception:
            return None

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
                from ..history.store import query_runs
                rows = query_runs(db_path)
            elif directory:
                rows = collect_reports(directory)
            else:
                return {"ok": False, "error": "db_path or directory is required"}
            return {"ok": True, "rows": rows, "html": render_index(rows)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
        target = url or _config.load_settings().get("site_url")
        if not target:
            return {"ok": False, "error": "no site URL configured"}
        from .. import upload as _upload
        return _upload.upload_report(report, target)

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
    #     -- the full URL (site_url + the site's own path) of the report.
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
        # Re-resolve against the *current* newest log in that folder
        # rather than trusting the exact filename as saved: a path picked
        # (or auto-started) in an earlier WoW session can point at a
        # filename that will never be written to again on installs that
        # timestamp every session's log instead of reusing a stable
        # "WoWCombatLog.txt" -- see config.resolve_watch_log_path. This
        # is what makes watch_auto_start actually zero-click session over
        # session on those installs instead of silently waiting forever
        # on a stale path (confirmed real 2026-09-01).
        log_path = str(_config.resolve_watch_log_path(Path(log_path).parent))

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
                        mdt_dir,
                    )
                except Exception as exc:
                    self._emit_watch_event({"type": "run_failed", "error": str(exc)})

        recorder = Recorder(
            log_path=Path(log_path),
            out_dir=Path(out_dir),
            on_run_start=lambda run: self._emit_watch_event({
                "type": "run_started", "zone": run.zone, "level": run.keystone_level,
            }),
            on_run_abandoned=lambda run: self._emit_watch_event({
                "type": "run_abandoned", "zone": run.zone, "level": run.keystone_level,
            }),
            on_run_complete=on_run_complete,
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
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=5.0)
        # Let the worker finish whatever run it's on (it's daemon, so it
        # can't outlive the app), then tell it to exit once the queue
        # drains. Joined only briefly: a run mid-analysis can take
        # minutes, and Stop must never freeze the UI for that.
        if self._watch_queue is not None:
            self._watch_queue.put(self._watch_stop_sentinel)
        if self._watch_worker is not None:
            self._watch_worker.join(timeout=1.0)
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

    def _handle_watched_run(self, run, route, store, avoidable, site_url,
                            history_db_path, addon_dir=None, mdt_dir=None) -> None:
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
            run, route, store, avoidable=avoidable,
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
        if result.get("ok"):
            self._emit_watch_event({
                "type": "uploaded",
                "url": f"{site_url.rstrip('/')}{result.get('url', '')}",
            })
        else:
            self._emit_watch_event({"type": "upload_failed", "error": result.get("error")})

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
            return {"ok": True, "update": _updater.check_for_update()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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

        def run_update() -> None:
            try:
                work_dir = Path(tempfile.mkdtemp(prefix="postmortem-update-dl-"))

                def on_progress(p: dict) -> None:
                    self._emit_update_event({"type": "downloading", **p})

                new_install = _updater.perform_update(download_url, work_dir, on_progress=on_progress)
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

    def get_settings(self) -> dict:
        """Return persisted desktop settings (see ``desktop/config.py``),
        merged with defaults for any field never saved. Always succeeds
        -- ``config.load_settings()`` is itself tolerant of a missing or
        corrupt settings file."""
        return _config.load_settings()

    def save_settings(self, settings: dict) -> dict:
        """Persist desktop settings (see ``desktop/config.py``).

        Returns ``{"ok": True}`` on success, or
        ``{"ok": False, "error": "..."}`` if writing failed (e.g. an
        unwritable config directory). Never raises.
        """
        try:
            _config.save_settings(settings or {})
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
