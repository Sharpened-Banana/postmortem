"""The public site's own persistence layer, wrapping ``history.store``.

This module owns exactly one table that isn't part of ``Store``'s own
schema: ``uploads``, which records which upload token "owns" each run
row. ``Store.ingest()`` itself has no ownership/auth concept at all --
anyone calling it with an existing ``(zone, start_ts)`` silently
overwrites that row -- so this table is what lets the service reject a
griefing overwrite of someone else's run (see ``existing_run()`` below)
and enforce per-token/per-IP upload rate limits.

``Store``'s own tables (``runs``/``players``/``deaths``) are *not*
created here -- that's ``Store``'s job, via its own ``CREATE TABLE IF
NOT EXISTS`` DDL, run whenever a ``Store`` is constructed against the
same ``db_path``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# _to_index_row is "private" by underscore convention, but it is exactly
# the code that turns a stored `runs` row into the dict shape
# report.index's render_index() expects (see history/store.py's own
# module docstring: it's the seam that lets render_index() stay ignorant
# of where its rows came from). Reusing it here is the right call --
# re-deriving that same field list independently in this module would
# just be duplicated logic that can silently drift out of sync with
# Store's schema. Pinned by test_api.py's feed-shape contract test.
from postmortem.history.store import _to_index_row

_UPLOADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    uploaded_at REAL NOT NULL,
    remote_addr TEXT
);

CREATE INDEX IF NOT EXISTS idx_uploads_token_hash ON uploads(token_hash);
"""

# Explicit column list for the public feed -- deliberately excludes
# `report_json` (up to ~1.2MB per row on a real +10 log) and other
# store-internal bookkeeping (`source_path`, `ingested_at`) that
# render_index() doesn't need. A 200-row feed with report_json included
# would pull ~240MB into memory per request; this keeps it to the
# handful of scalar columns the index page actually renders.
_FEED_COLUMNS = (
    "id, zone, level, start_ts, end_ts, completed, timed, duration_ms, "
    "wall_s, deaths, death_cost_s, forces_pct, adherence_pct, "
    "kick_efficiency_pct, affixes, file_name, html_name"
)


def connect(db_path: "str | Path") -> sqlite3.Connection:
    """Open one connection to the site's SQLite db.

    Sets ``row_factory=sqlite3.Row``, turns on foreign keys and WAL
    journaling, and applies the ``uploads`` table DDL (idempotent, cheap
    to redo on every open). Does **not** create ``runs``/``players``/
    ``deaths`` -- callers that need those to already exist (e.g. before
    running a SELECT against ``runs``) should construct-and-close a
    ``Store`` against the same ``db_path`` first; its constructor's own
    ``CREATE TABLE IF NOT EXISTS`` calls are just as cheap/idempotent.

    A fresh connection is expected to be opened per request rather than
    shared across requests from a module-global: ``Store``/this function
    open plain ``sqlite3.connect()`` connections with no
    ``check_same_thread=False``, and FastAPI's threadpool dispatches
    concurrent requests to different threads.
    """
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_UPLOADS_SCHEMA)
    conn.commit()
    return conn


def feed_rows(
    conn: sqlite3.Connection, *, zone: Optional[str] = None, limit: int = 200
) -> list[dict[str, Any]]:
    """The public feed: newest-first run rows, ``report_json`` excluded.

    Assumes the ``runs`` table already exists (see ``connect()``'s
    docstring for why that's the caller's job, not this function's).
    """
    sql = f"SELECT {_FEED_COLUMNS} FROM runs"
    params: list[Any] = []
    if zone is not None:
        sql += " WHERE zone = ?"
        params.append(zone)
    sql += " ORDER BY start_ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    out = []
    for r in rows:
        row = _to_index_row(r)
        # _to_index_row() normally derives "html" from the html_name
        # column, which is meant for locally-saved reports with an HTML
        # sibling file on disk. Uploads never have a source_path/
        # html_path (there's nothing on this server's filesystem to point
        # at), so html_name is always None here -- without this override
        # every feed row would render as unclickable dim text instead of
        # a working link (see report/index.py's `r.html ? <a> : <span>`).
        row["html"] = f"/runs/{r['id']}"
        out.append(row)
    return out


def existing_run(
    conn: sqlite3.Connection, zone: Optional[str], start_ts: Optional[float]
) -> Optional[tuple[int, Optional[str]]]:
    """Look up the run at this ``(zone, start_ts)``, if any, and who
    uploaded it.

    Uses ``IS`` for the comparison, matching exactly the lookup
    ``Store.ingest()`` does internally -- that's what makes this safe to
    use as a pre-check: this function and the ``ingest()`` call that
    follows it are guaranteed to be looking at the same row.

    Returns ``(run_id, token_hash)``, or ``None`` if no such run exists.
    ``token_hash`` is ``None`` if the run exists but has no recorded
    uploader (shouldn't happen via this service -- every write here goes
    through ``record_upload()`` too -- but a row seeded some other way
    shouldn't be treated as a crash; it's treated as "no owner on file",
    i.e. the upload is allowed rather than rejected).
    """
    row = conn.execute(
        "SELECT r.id AS id, u.token_hash AS token_hash FROM runs r "
        "LEFT JOIN uploads u ON u.run_id = r.id "
        "WHERE r.zone IS ? AND r.start_ts IS ?",
        (zone, start_ts),
    ).fetchone()
    if row is None:
        return None
    return (row["id"], row["token_hash"])


def record_upload(
    conn: sqlite3.Connection,
    run_id: int,
    token_hash: str,
    remote_addr: Optional[str],
) -> None:
    """Record (or update) who owns ``run_id``. Does not commit -- the
    caller commits once after this and whatever else it did in the same
    transaction (see app.py's POST /api/runs handler)."""
    conn.execute(
        "INSERT OR REPLACE INTO uploads (run_id, token_hash, uploaded_at, remote_addr) "
        "VALUES (?, ?, ?, ?)",
        (run_id, token_hash, time.time(), remote_addr),
    )


def last_upload_at(conn: sqlite3.Connection, token_hash: str) -> Optional[float]:
    """Most recent ``uploaded_at`` across all runs owned by this token,
    or ``None`` if this token has never uploaded.

    ``SELECT MAX(...)`` always returns exactly one row even with zero
    matches (with ``m`` = NULL in that case), so there's no need to
    special-case an empty result set here.
    """
    row = conn.execute(
        "SELECT MAX(uploaded_at) AS m FROM uploads WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    return row["m"]


def last_upload_at_ip(conn: sqlite3.Connection, remote_addr: Optional[str]) -> Optional[float]:
    """Most recent ``uploaded_at`` across all runs uploaded from this IP,
    or ``None`` if unknown/never."""
    if remote_addr is None:
        return None
    row = conn.execute(
        "SELECT MAX(uploaded_at) AS m FROM uploads WHERE remote_addr = ?",
        (remote_addr,),
    ).fetchone()
    return row["m"]
