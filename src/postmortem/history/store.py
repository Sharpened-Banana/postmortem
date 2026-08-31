"""SQLite-backed run history store.

WP-B1: a durable alternative to re-scanning a folder of report JSON files
every time ``postmortem index`` runs. ``ingest()`` writes one run
(plus its players and deaths) into a small ``runs.db``, keyed idempotently
on ``(zone, start_ts)`` so re-ingesting the same run is a no-op update
rather than a duplicate row. ``query_runs()`` reads them back out in
**exactly** the row shape ``report.index.collect_reports()`` produces, so
``report.index.render_index()`` can build the history page from either
source without caring which one it got -- see cli.py's ``cmd_index`` and
``cmd_analyze`` for how the two paths (JSON-scan vs. this store) are wired
up side by side.

The full report JSON is also stored verbatim (as text) on each run row --
cheap to keep, and it means any field a future consumer wants (that isn't
already broken out into its own column) is still just one query away
instead of requiring a re-scan of the original files.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone TEXT,
    level INTEGER,
    start_ts REAL,
    end_ts REAL,
    completed INTEGER,
    timed INTEGER,
    duration_ms INTEGER,
    wall_s REAL,
    deaths INTEGER,
    death_cost_s REAL,
    forces_pct REAL,
    adherence_pct REAL,
    kick_efficiency_pct REAL,
    affixes TEXT,
    file_name TEXT,
    html_name TEXT,
    source_path TEXT,
    report_json TEXT NOT NULL,
    ingested_at REAL,
    UNIQUE(zone, start_ts)
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    guid TEXT,
    name TEXT,
    class TEXT,
    spec TEXT,
    role TEXT,
    damage_done INTEGER,
    healing_done INTEGER,
    dps REAL,
    hps REAL,
    deaths INTEGER,
    interrupts INTEGER
);

CREATE TABLE IF NOT EXISTS deaths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts REAL,
    player TEXT,
    pull INTEGER,
    killing_blow_spell TEXT,
    killing_blow_source TEXT,
    killing_blow_amount INTEGER
);

CREATE INDEX IF NOT EXISTS idx_players_run_id ON players(run_id);
CREATE INDEX IF NOT EXISTS idx_deaths_run_id ON deaths(run_id);
"""


class Store:
    """A connection to one run-history SQLite database.

    Opens (creating if needed, running the schema DDL) on construction;
    call ``close()`` when done, or use as a context manager. Kept open
    across a batch of ``ingest()`` calls (e.g. one per file in a directory
    scan) rather than reopened per call, since that's the common case from
    the CLI.
    """

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writes ---------------------------------------------------------

    def ingest(
        self,
        report: dict[str, Any],
        *,
        source_path: Optional[str | Path] = None,
        html_path: Optional[str | Path] = None,
    ) -> int:
        """Insert (or, if a run with the same zone+start_ts already exists,
        update) one run, replacing its players and deaths rows.

        ``source_path`` is the path to the report's own JSON (if any --
        used the same way ``collect_reports`` derives ``file``/``html``:
        the HTML sibling next to it, checked for existence). ``html_path``
        overrides that when the HTML report's path is already known
        precisely (e.g. `analyze --history-db`, where the html path is
        whatever ``--format``/``--out`` actually produced and may not sit
        next to a json file at all).

        Returns the run's row id either way.
        """
        row = _row_values(report, source_path, html_path)
        cur = self._conn.execute(
            "SELECT id FROM runs WHERE zone IS ? AND start_ts IS ?",
            (row["zone"], row["start_ts"]),
        )
        existing = cur.fetchone()

        columns = list(row)
        if existing is None:
            placeholders = ", ".join("?" for _ in columns)
            insert_cur = self._conn.execute(
                f"INSERT INTO runs ({', '.join(columns)}) VALUES ({placeholders})",
                [row[c] for c in columns],
            )
            run_id = insert_cur.lastrowid
        else:
            run_id = existing["id"]
            assignments = ", ".join(f"{c} = ?" for c in columns)
            self._conn.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                [row[c] for c in columns] + [run_id],
            )
            self._conn.execute("DELETE FROM players WHERE run_id = ?", (run_id,))
            self._conn.execute("DELETE FROM deaths WHERE run_id = ?", (run_id,))

        for player in report.get("players") or []:
            self._conn.execute(
                "INSERT INTO players (run_id, guid, name, class, spec, role, "
                "damage_done, healing_done, dps, hps, deaths, interrupts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    player.get("guid"),
                    player.get("name"),
                    player.get("class"),
                    player.get("spec"),
                    player.get("role"),
                    player.get("damage_done"),
                    player.get("healing_done"),
                    player.get("dps"),
                    player.get("hps"),
                    player.get("deaths"),
                    player.get("interrupts"),
                ),
            )

        for death in report.get("deaths") or []:
            killing_blow = death.get("killing_blow") or {}
            self._conn.execute(
                "INSERT INTO deaths (run_id, ts, player, pull, killing_blow_spell, "
                "killing_blow_source, killing_blow_amount) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    death.get("ts"),
                    death.get("player"),
                    death.get("pull"),
                    killing_blow.get("spell"),
                    killing_blow.get("source"),
                    killing_blow.get("amount"),
                ),
            )

        self._conn.commit()
        return int(run_id)

    # -- reads ------------------------------------------------------------

    def query_runs(self, *, zone: Optional[str] = None) -> list[dict[str, Any]]:
        """Return run rows in the same shape as ``report.index.collect_reports``,
        newest first (matches that function's sort by ``start_ts`` descending).
        """
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if zone is not None:
            sql += " WHERE zone = ?"
            params.append(zone)
        rows = self._conn.execute(sql, params).fetchall()
        out = [_to_index_row(r) for r in rows]
        out.sort(key=lambda r: r.get("start_ts") or 0, reverse=True)
        return out

    def get_report(self, run_id: int) -> Optional[dict[str, Any]]:
        """Return the full stored report JSON for one run, if it exists."""
        cur = self._conn.execute("SELECT report_json FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        return json.loads(row["report_json"]) if row else None


def _row_values(
    report: dict[str, Any],
    source_path: Optional[str | Path],
    html_path: Optional[str | Path],
) -> dict[str, Any]:
    run = report.get("run") or {}
    forces = report.get("forces") or {}
    comparison = report.get("comparison") or {}
    enemy_casts = report.get("enemy_casts") or {}
    death_cost = report.get("death_cost") or {}
    zone = run.get("zone")
    start_ts = run.get("start_ts")

    file_name: Optional[str] = None
    html_name: Optional[str] = None
    if source_path is not None:
        src = Path(source_path)
        file_name = src.name
        html_sibling = src.with_suffix(".html")
        if html_sibling.exists():
            html_name = html_sibling.name
    if html_path is not None:
        html_name = Path(html_path).name
    if file_name is None:
        # No JSON file backs this run on disk (e.g. `analyze --history-db`
        # with a --format that never wrote json/html) -- synthesize a label
        # so render_index still has something to show in the Report column,
        # mirroring _report_basename()'s naming in cli.py.
        safe_zone = re.sub(r"[^A-Za-z0-9]+", "", zone or "run")
        stamp = (
            time.strftime("%Y%m%d-%H%M%S", time.localtime(start_ts))
            if start_ts else "unknown"
        )
        file_name = f"{stamp}_{safe_zone}.json"

    return {
        "zone": zone,
        "level": run.get("keystone_level"),
        "start_ts": start_ts,
        "end_ts": run.get("end_ts"),
        "completed": bool(run.get("completed")),
        "timed": run.get("timed"),
        "duration_ms": run.get("duration_ms"),
        "wall_s": run.get("wall_duration_s"),
        "deaths": len(report.get("deaths") or []),
        "death_cost_s": death_cost.get("total_s"),
        "forces_pct": forces.get("pct"),
        "adherence_pct": comparison.get("adherence_pct"),
        "kick_efficiency_pct": enemy_casts.get("kick_efficiency_pct"),
        "affixes": json.dumps(run.get("affixes") or []),
        "file_name": file_name,
        "html_name": html_name,
        "source_path": str(source_path) if source_path is not None else None,
        "report_json": json.dumps(report),
        "ingested_at": time.time(),
    }


def _to_index_row(r: sqlite3.Row) -> dict[str, Any]:
    """The inverse of ``_row_values``: a stored run row -> a
    ``collect_reports()``-shaped dict. Keys and (as closely as sqlite's
    storage classes allow) types must match exactly -- this is the seam
    that lets ``report.index.render_index()`` stay ignorant of whether its
    rows came from a directory scan or from here."""
    start_ts = r["start_ts"]
    return {
        "file": r["file_name"],
        "html": r["html_name"],
        "zone": r["zone"],
        "level": r["level"],
        "start_ts": start_ts,
        "date": (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(start_ts))
            if start_ts else "?"
        ),
        "completed": bool(r["completed"]),
        "timed": bool(r["timed"]) if r["timed"] is not None else None,
        "duration_ms": r["duration_ms"],
        "wall_s": r["wall_s"],
        "deaths": r["deaths"],
        "death_cost_s": r["death_cost_s"],
        "forces_pct": r["forces_pct"],
        "adherence_pct": r["adherence_pct"],
        "kick_efficiency_pct": r["kick_efficiency_pct"],
        "affixes": json.loads(r["affixes"]) if r["affixes"] else [],
    }


def ingest(
    report: dict[str, Any],
    db_path: str | Path,
    *,
    source_path: Optional[str | Path] = None,
    html_path: Optional[str | Path] = None,
) -> int:
    """Convenience wrapper: open ``db_path``, ingest one report, close.

    See ``Store.ingest`` for details. Prefer opening a ``Store`` directly
    when ingesting many reports in a row (e.g. a directory scan) to avoid
    reopening the database once per file.
    """
    with Store(db_path) as store:
        return store.ingest(report, source_path=source_path, html_path=html_path)


def query_runs(db_path: str | Path, *, zone: Optional[str] = None) -> list[dict[str, Any]]:
    """Convenience wrapper: open ``db_path``, read back rows, close."""
    with Store(db_path) as store:
        return store.query_runs(zone=zone)
