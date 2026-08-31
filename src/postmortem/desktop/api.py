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

from types import SimpleNamespace
from typing import Any, Optional

from .. import cli as _cli
from ..combatlog.parser import parse_file
from ..combatlog.segmenter import segment_runs
from ..mdt.extract import write_dungeon_data
from ..report.html import render_html
from ..report.index import collect_reports, render_index
from . import config as _config


class DesktopAPI:
    """Bridge object exposed to the desktop app's JS runtime (typically
    as ``window.pywebview.api``). Construct one instance and hand it to
    ``webview.create_window(..., js_api=DesktopAPI())`` -- this class
    itself has no pywebview dependency, so nothing about constructing or
    calling its business-logic methods requires pywebview to be
    installed at all (only the three ``pick_*`` dialog methods do, at
    call time).
    """

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
        store = _cli._load_store(params.get("dungeon_data_path"))
        avoidable = _cli._load_avoidable(params.get("avoidable_data_path"))

        run_selector = str(params.get("run_selector") or "last")
        segment = _cli._pick_run(segment_runs(parse_file(log_path)), run_selector)

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

        return {"ok": True, "report": report, "html": render_html(report)}

    # -- history ----------------------------------------------------------

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

    # -- settings -----------------------------------------------------------

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
