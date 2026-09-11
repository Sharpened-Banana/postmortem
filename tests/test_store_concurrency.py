"""The history store under concurrency (audit findings STORE-3/4/5).

The public site opens a Store on essentially every request, against one
SQLite file on one small machine, while a desktop app and a CLI use the
same class locally. All three findings are about what happens when two
of those happen at once.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from postmortem.history.store import BUSY_TIMEOUT_S, Store, query_runs

# The schema as it stood before the leaderboard columns were added, so a
# migration genuinely has work to do.
_PRE_MIGRATION_SCHEMA = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, zone TEXT, level INTEGER,
    start_ts REAL, end_ts REAL, completed INTEGER, timed INTEGER,
    duration_ms INTEGER, wall_s REAL, deaths INTEGER, death_cost_s REAL,
    forces_pct REAL, adherence_pct REAL, kick_efficiency_pct REAL,
    affixes TEXT, file_name TEXT, html_name TEXT, source_path TEXT,
    report_json TEXT NOT NULL, ingested_at REAL, UNIQUE(zone, start_ts));
"""


@pytest.fixture()
def report(dungeon_data_file) -> dict:
    """A real analyzed run, so the backfill has real report bodies to read
    rather than empty ones (the point of the batching is how much each row
    costs). The equivalent fixture in test_history_store.py is module-local,
    so this file carries its own."""
    from conftest import ROUTE_PRESET, build_run_log
    from postmortem.analysis.run_analyzer import analyze_run
    from postmortem.combatlog.parser import iter_events
    from postmortem.combatlog.segmenter import segment_runs
    from postmortem.mdt.dungeon_data import DungeonDataStore
    from postmortem.mdt.route import Route

    (run,) = list(segment_runs(iter_events(build_run_log().lines)))
    return analyze_run(run, route=Route.from_preset(ROUTE_PRESET),
                       store=DungeonDataStore.load(dungeon_data_file))


def _old_database(path, rows=0, report=None):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_PRE_MIGRATION_SCHEMA)
        body = json.dumps(report) if report is not None else "{}"
        for i in range(rows):
            conn.execute(
                "INSERT INTO runs (zone, start_ts, completed, report_json) VALUES (?, ?, 1, ?)",
                (f"Zone {i}", float(i), body),
            )
        conn.commit()
    finally:
        conn.close()


class TestMigrationUnderConcurrency:
    """STORE-3. Reading PRAGMA table_info and then ALTER TABLE is a
    check-then-act. Two openers both saw the column missing, both tried to
    add it, and the loser raised "duplicate column name"."""

    def test_many_processes_can_open_at_once(self, tmp_path):
        db_path = tmp_path / "runs.db"
        _old_database(db_path, rows=5)

        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def open_it():
            barrier.wait()
            try:
                Store(db_path).close()
            except BaseException as exc:  # noqa: BLE001 -- recording, not handling
                errors.append(exc)

        threads = [threading.Thread(target=open_it) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, f"concurrent opens failed: {errors[:3]}"
        cols = {r[1] for r in sqlite3.connect(str(db_path)).execute("PRAGMA table_info(runs)")}
        assert {"party", "threshold", "margin_ms", "deaths_json"} <= cols

    def test_a_column_added_by_someone_else_mid_migration_is_tolerated(self, tmp_path):
        """The race, made deterministic. _migrate() reads which columns
        exist and then adds the missing ones; another process can commit
        its own ALTER in between. This feeds _migrate() a deliberately
        stale answer to that first question, which is exactly what the
        loser of the race sees."""
        from postmortem.history import store as store_mod

        db_path = tmp_path / "runs.db"
        _old_database(db_path, rows=1)
        Store(db_path).close()  # the columns now genuinely exist

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        class StaleView:
            """Reports the pre-migration column list, as a process that
            read it a moment before the winner's ALTER landed would."""

            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *a, **k):
                if "table_info(runs)" in sql:
                    return [(i, name, "", 0, None, 0) for i, name in enumerate(
                        ["id", "zone", "level", "start_ts", "report_json"])]
                return self._inner.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        try:
            store_mod._migrate(StaleView(conn))  # must not raise
        finally:
            conn.close()

        assert len(query_runs(db_path)) == 1

    def test_opening_an_already_migrated_database_is_still_fine(self, tmp_path):
        db_path = tmp_path / "runs.db"
        _old_database(db_path, rows=2)
        Store(db_path).close()
        Store(db_path).close()
        assert len(query_runs(db_path)) == 2


class TestBackfill:
    """STORE-5. The backfill read one whole report per row inside the
    constructor's single transaction, so a few thousand rows meant a long
    write lock -- and because it was uncommitted, every other opener
    started the same work over again."""

    def test_a_row_whose_report_will_not_parse_is_not_retried_forever(self, tmp_path):
        db_path = tmp_path / "runs.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(_PRE_MIGRATION_SCHEMA)
            conn.execute(
                "INSERT INTO runs (zone, start_ts, completed, report_json) "
                "VALUES ('Broken', 1.0, 1, 'not json')"
            )
            conn.commit()
        finally:
            conn.close()

        Store(db_path).close()
        left = sqlite3.connect(str(db_path)).execute(
            "SELECT COUNT(*) FROM runs WHERE party IS NULL"
        ).fetchone()[0]
        assert left == 0, (
            "an unparseable row stayed NULL, so every future open re-reads it"
        )
        # And it is still listed, just without leaderboard detail.
        assert query_runs(db_path)[0]["party"] == []

    def test_the_backfill_commits_as_it_goes(self, tmp_path, report, monkeypatch):
        """The property that stops concurrent openers repeating the work:
        rows finished early are committed, and therefore visible to another
        connection, before the open returns.

        Checked from inside the backfill rather than by polling from a
        thread -- polling raced the work and passed or failed on timing."""
        from postmortem.history import store as store_mod

        db_path = tmp_path / "runs.db"
        _old_database(db_path, rows=25, report=report)
        monkeypatch.setattr(store_mod, "_BACKFILL_BATCH", 10)

        real_fields = store_mod._leaderboard_fields
        calls = {"n": 0}
        visible_to_others: list[int] = []

        def counting_fields(rep):
            calls["n"] += 1
            # Once the first batch must have been committed, ask a separate
            # connection what it can see.
            if calls["n"] == 11:
                other = sqlite3.connect(str(db_path))
                try:
                    visible_to_others.append(other.execute(
                        "SELECT COUNT(*) FROM runs WHERE party IS NOT NULL"
                    ).fetchone()[0])
                finally:
                    other.close()
            return real_fields(rep)

        monkeypatch.setattr(store_mod, "_leaderboard_fields", counting_fields)
        Store(db_path).close()

        assert visible_to_others, "the backfill never reached a second batch"
        assert visible_to_others[0] >= 10, (
            f"another connection could see only {visible_to_others[0]} finished rows "
            "part-way through, so the whole backfill was one uncommitted "
            "transaction holding the write lock"
        )
        assert all(r["party"] is not None for r in query_runs(db_path))


class TestBusyTimeout:
    """STORE-4 (analyzer half). Neither connector set a timeout, so a slow
    write elsewhere turned a perfectly ordinary insert into a hard error."""

    def test_the_store_waits_for_a_held_write_lock(self, tmp_path):
        db_path = tmp_path / "runs.db"
        Store(db_path).close()

        holder = sqlite3.connect(str(db_path))
        # Python's sqlite3 manages transactions itself by default, which
        # swallows an explicit BEGIN. Autocommit mode hands that back, so
        # the lock this test depends on is genuinely held.
        holder.isolation_level = None
        outcome: dict[str, object] = {}

        def try_to_write():
            # Timed from BEFORE the open, because opening is itself a write
            # (the migration takes the lock) -- so that is where the wait
            # actually lands, and timing only the insert missed it.
            started = time.monotonic()
            store = None
            try:
                store = Store(db_path)
                store._conn.execute("INSERT INTO runs (zone, start_ts, report_json) "
                                    "VALUES ('Late', 9.0, '{}')")
                store._conn.commit()
                outcome["ok"] = True
            except sqlite3.OperationalError as exc:
                outcome["ok"] = False
                outcome["error"] = str(exc)
            finally:
                outcome["waited"] = time.monotonic() - started
                if store is not None:
                    store.close()

        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("INSERT INTO runs (zone, start_ts, report_json) "
                           "VALUES ('Held', 1.0, '{}')")
            worker = threading.Thread(target=try_to_write)
            worker.start()
            # Comfortably longer than two rounds of the library's old
            # five-second default, so a store without an explicit timeout
            # cannot squeak through by retrying.
            time.sleep(12.0)
            holder.commit()
            worker.join(timeout=40)
        finally:
            holder.close()

        assert outcome.get("ok"), f"the writer gave up: {outcome.get('error')}"
        assert outcome["waited"] > 10.0, (
            f"returned after only {outcome['waited']:.1f}s, so it never waited"
        )

    def test_a_local_database_is_in_wal_mode(self, tmp_path):
        """So a long write does not block the app's own reads."""
        db_path = tmp_path / "runs.db"
        Store(db_path).close()
        mode = sqlite3.connect(str(db_path)).execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
