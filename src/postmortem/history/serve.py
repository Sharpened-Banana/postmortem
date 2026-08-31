"""Local dev web server over a directory of saved reports (WP-B3).

``postmortem serve reports/`` serves the directory as a plain static
web root: ``GET /`` and ``GET /index.html`` return the history page, and
``GET /<run>.html`` serves an individual run's report page directly. Both
of those fall straight out of ``http.server.SimpleHTTPRequestHandler``'s
normal directory-relative static-file behavior once it's pointed at the
right root -- this module doesn't hand-write general file serving.

The one thing layered on top is freshness: before each request is
handled, a cheap per-request check (an ``os.stat`` scan of the
directory's report ``*.json`` files -- not a background watcher/polling
thread) compares the newest report mtime against ``index.html``'s own
mtime and rebuilds the page if any report is newer, or the page doesn't
exist yet. A per-run ``.html``/``.json`` report file never changes after
it's written, so ``index.html`` is the only file that ever needs this.

JSON-scan mode only: this calls ``report.index.build_index()`` directly,
the same function the default (no ``--db``) ``index`` command uses.
Detecting staleness of WP-B1's ``--db`` SQLite store would need a
different signal (the database's own mtime, or a content hash) than the
file-mtime scan here -- a reasonable future addition, not built now.
"""

from __future__ import annotations

import functools
import http.server
from pathlib import Path

from ..report.index import build_index

DEFAULT_PORT = 8765
DEFAULT_BIND = "127.0.0.1"  # loopback-only -- this is a local dev tool


def _needs_rebuild(directory: Path, index_path: Path) -> bool:
    """True if ``index_path`` is missing or older than the newest report
    JSON under ``directory``. Pure ``os.stat`` calls -- no JSON parsing,
    so this is cheap to run on every request."""
    if not index_path.exists():
        return True
    index_mtime = index_path.stat().st_mtime
    return any(
        report_path.stat().st_mtime > index_mtime
        for report_path in directory.rglob("*.json")
    )


class _Handler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with an index.html freshness check spliced
    in before every request. Static serving itself (path resolution,
    directory listing suppression via index.html, content-type guessing,
    etc.) is all inherited unchanged."""

    def do_GET(self) -> None:
        self._maybe_rebuild_index()
        super().do_GET()

    def do_HEAD(self) -> None:
        self._maybe_rebuild_index()
        super().do_HEAD()

    def _maybe_rebuild_index(self) -> None:
        directory = Path(self.directory)
        index_path = directory / "index.html"
        if _needs_rebuild(directory, index_path):
            build_index(directory, index_path)


def make_server(
    directory: str | Path,
    *,
    port: int = DEFAULT_PORT,
    bind: str = DEFAULT_BIND,
) -> http.server.ThreadingHTTPServer:
    """Build (but do not start) a threading HTTP server over ``directory``.

    Pass ``port=0`` to let the OS pick a free ephemeral port -- read the
    actual bound port back from the returned server's ``server_address``.
    Caller is responsible for calling ``serve_forever()`` (blocking) and,
    eventually, ``shutdown()``/``server_close()``.
    """
    root = Path(directory)
    handler_cls = functools.partial(_Handler, directory=str(root))
    return http.server.ThreadingHTTPServer((bind, port), handler_cls)
