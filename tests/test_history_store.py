"""Tests for the SQLite run-history store (WP-B1): idempotent ingest,
the query_runs()/collect_reports() row-shape contract, zone filtering,
and the `index --db` / `analyze --history-db` CLI wiring.
"""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest
from conftest import ROUTE_PRESET, build_run_log

from postmortem.analysis.run_analyzer import analyze_run
from postmortem.cli import main
from postmortem.combatlog.parser import iter_events
from postmortem.combatlog.segmenter import segment_runs
from postmortem.history.store import Store, ingest, party_fingerprint, query_runs
from postmortem.mdt.dungeon_data import DungeonDataStore
from postmortem.mdt.route import Route
from postmortem.report.index import collect_reports, render_index


@pytest.fixture()
def run_segment():
    (run,) = list(segment_runs(iter_events(build_run_log().lines)))
    return run


@pytest.fixture()
def route() -> Route:
    return Route.from_preset(ROUTE_PRESET)


@pytest.fixture()
def report(run_segment, route, dungeon_data_file) -> dict:
    store = DungeonDataStore.load(dungeon_data_file)
    return analyze_run(run_segment, route=route, store=store)


def _table_counts(db_path) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "players", "deaths")
        }
    finally:
        conn.close()


class TestIngestIdempotent:
    def test_ingest_twice_yields_one_row(self, tmp_path, report):
        db_path = tmp_path / "runs.db"
        with Store(db_path) as store:
            id1 = store.ingest(report)
            id2 = store.ingest(report)
        assert id1 == id2

        counts = _table_counts(db_path)
        assert counts["runs"] == 1
        assert counts["players"] == len(report["players"])
        assert counts["deaths"] == len(report["deaths"])

    def test_ingest_via_module_function_twice(self, tmp_path, report):
        db_path = tmp_path / "runs.db"
        ingest(report, db_path)
        ingest(report, db_path)
        assert _table_counts(db_path)["runs"] == 1


class TestReplaceRunId:
    """A groupmate's log stamps the same key with a start_ts a few
    milliseconds off. The site decides those are one run; the store must
    then update THAT row (keeping its id and share link) rather than
    insert a second one keyed on the new timestamp."""

    def test_replaces_the_named_row_and_moves_its_start_ts(self, tmp_path, report):
        db_path = tmp_path / "runs.db"
        groupmate = copy.deepcopy(report)
        groupmate["run"]["start_ts"] = report["run"]["start_ts"] + 0.317
        with Store(db_path) as store:
            first = store.ingest(report)
            second = store.ingest(groupmate, replace_run_id=first)
        assert second == first
        counts = _table_counts(db_path)
        assert counts["runs"] == 1
        assert counts["players"] == len(report["players"])
        conn = sqlite3.connect(str(db_path))
        try:
            (start_ts,) = conn.execute("SELECT start_ts FROM runs").fetchone()
        finally:
            conn.close()
        assert start_ts == pytest.approx(groupmate["run"]["start_ts"])

    def test_a_missing_row_is_an_error_not_a_silent_insert(self, tmp_path, report):
        with Store(tmp_path / "runs.db") as store:
            with pytest.raises(LookupError):
                store.ingest(report, replace_run_id=999)
        assert _table_counts(tmp_path / "runs.db")["runs"] == 0


class TestPartyFingerprint:
    def test_same_players_in_any_order_give_one_key(self):
        a = [{"guid": "Player-1-B", "name": "Bee"}, {"guid": "Player-1-A", "name": "Ay"}]
        b = [{"guid": "Player-1-A", "name": "Ay"}, {"guid": "Player-1-B", "name": "Bee"}]
        assert party_fingerprint(a) == party_fingerprint(b) == "Player-1-A|Player-1-B"

    def test_pets_bucket_is_ignored_and_names_stand_in_for_missing_guids(self):
        players = [{"guid": "_pets", "name": "Pets"}, {"guid": "", "name": "Solo"}]
        assert party_fingerprint(players) == "name:Solo"

    def test_nothing_identifiable_is_none(self):
        assert party_fingerprint([]) is None
        assert party_fingerprint(None) is None
        assert party_fingerprint([{"guid": "_pets"}]) is None

    def test_a_different_party_is_a_different_key(self, report):
        other = copy.deepcopy(report["players"])
        other[0]["guid"] = "Player-9999-DEADBEEF"
        assert party_fingerprint(report["players"]) != party_fingerprint(other)


class TestQueryRunsShape:
    def test_matches_collect_reports_keys(self, tmp_path, report):
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        (reports_dir / "run.json").write_text(json.dumps(report), encoding="utf-8")
        scan_rows = collect_reports(reports_dir)
        assert len(scan_rows) == 1

        db_path = tmp_path / "runs.db"
        ingest(report, db_path)
        db_rows = query_runs(db_path)
        assert len(db_rows) == 1

        assert set(db_rows[0].keys()) == set(scan_rows[0].keys())
        # spot-check the fields that came straight from the report, not just
        # path-derived bookkeeping (file/html can legitimately differ in
        # value between a directory scan and a DB row)
        for key in ("zone", "level", "start_ts", "completed", "timed",
                    "duration_ms", "deaths", "adherence_pct",
                    "kick_efficiency_pct", "affixes",
                    # leaderboard-row fields: nested JSON round-trips
                    # through sqlite TEXT back to identical structures
                    "party", "threshold", "margin_ms", "deaths_detail"):
            assert db_rows[0][key] == scan_rows[0][key], key

    def test_party_excludes_pet_bucket_and_keeps_class_role(self, tmp_path, report):
        db_path = tmp_path / "runs.db"
        ingest(report, db_path)
        row = query_runs(db_path)[0]
        assert row["party"], "expected at least one player in party"
        assert all(set(p) == {"name", "class", "role"} for p in row["party"])
        assert all(not str(p["name"]).startswith("Pets") for p in row["party"])
        assert len(row["party"]) == len([
            p for p in report["players"] if not str(p.get("guid", "")).startswith("_")
        ])

    def test_deaths_detail_carries_killing_spell(self, tmp_path, report):
        db_path = tmp_path / "runs.db"
        ingest(report, db_path)
        row = query_runs(db_path)[0]
        assert len(row["deaths_detail"]) == row["deaths"] == len(report["deaths"])
        for d, src in zip(row["deaths_detail"], report["deaths"]):
            assert set(d) == {"t", "player", "spell"}
            assert d["player"] == src["player"]


class TestMigration:
    def test_old_schema_db_gains_new_columns_on_open(self, tmp_path, report):
        # A database created before the leaderboard columns existed: the
        # original ``runs`` DDL, minus party/threshold/margin_ms/deaths_json.
        db_path = tmp_path / "runs.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, zone TEXT, level INTEGER,
                start_ts REAL, end_ts REAL, completed INTEGER, timed INTEGER,
                duration_ms INTEGER, wall_s REAL, deaths INTEGER, death_cost_s REAL,
                forces_pct REAL, adherence_pct REAL, kick_efficiency_pct REAL,
                affixes TEXT, file_name TEXT, html_name TEXT, source_path TEXT,
                report_json TEXT NOT NULL, ingested_at REAL, UNIQUE(zone, start_ts));
            INSERT INTO runs (zone, start_ts, completed, report_json)
                VALUES ('Old Zone', 1.0, 1, '{}');
            INSERT INTO runs (zone, start_ts, completed, report_json)
                VALUES ('Broken Zone', 2.0, 1, 'not json');
        """)
        # A row ingested by the old code, whose report_json carries a
        # party and timer the new columns should be filled from.
        conn.execute(
            "INSERT INTO runs (zone, start_ts, completed, report_json) VALUES (?, ?, 1, ?)",
            ("Old Full", 3.0, json.dumps(report)),
        )
        conn.commit()
        conn.close()

        # Opening the store migrates; the pre-existing rows read back with
        # the new fields normalized to empty/None, not an error ...
        rows = {r["zone"]: r for r in query_runs(db_path)}
        assert set(rows) == {"Old Zone", "Broken Zone", "Old Full"}
        assert rows["Old Zone"]["party"] == []
        assert rows["Old Zone"]["deaths_detail"] == []
        assert rows["Old Zone"]["threshold"] is None
        assert rows["Old Zone"]["margin_ms"] is None
        assert rows["Broken Zone"]["party"] == []

        # ... and the row with a real stored report is backfilled to exactly
        # what a fresh ingest of the same report writes.
        fresh_db = tmp_path / "fresh.db"
        ingest(report, fresh_db)
        fresh = query_runs(fresh_db)[0]
        for key in ("party", "deaths_detail", "threshold", "margin_ms"):
            assert rows["Old Full"][key] == fresh[key], key
        assert rows["Old Full"]["party"]

        cols = {r[1] for r in sqlite3.connect(str(db_path)).execute("PRAGMA table_info(runs)")}
        assert {"party", "threshold", "margin_ms", "deaths_json"} <= cols

        # And ingesting into the migrated db fills them.
        ingest(report, db_path)
        fresh = [r for r in query_runs(db_path) if r["zone"] != "Old Zone"][0]
        assert fresh["party"]

    def test_opening_twice_is_idempotent(self, tmp_path):
        db_path = tmp_path / "runs.db"
        Store(db_path).close()
        Store(db_path).close()  # second _migrate() must not re-ALTER
        assert query_runs(db_path) == []


class TestZoneFilter:
    def test_filter_by_zone(self, tmp_path, report):
        other = copy.deepcopy(report)
        other["run"]["zone"] = "Other Dungeon"
        other["run"]["start_ts"] = report["run"]["start_ts"] + 100000

        db_path = tmp_path / "runs.db"
        with Store(db_path) as store:
            store.ingest(report)
            store.ingest(other)

        murder_row = query_runs(db_path, zone="Murder Row")
        assert len(murder_row) == 1
        assert murder_row[0]["zone"] == "Murder Row"

        other_rows = query_runs(db_path, zone="Other Dungeon")
        assert len(other_rows) == 1
        assert other_rows[0]["zone"] == "Other Dungeon"

        assert len(query_runs(db_path)) == 2
        assert query_runs(db_path, zone="Nonexistent Dungeon") == []


class TestCLIIndexDB:
    def test_index_db_builds_from_db_not_fresh_scan(self, tmp_path, report):
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        json_path = reports_dir / "run.json"
        json_path.write_text(json.dumps(report), encoding="utf-8")

        # Pre-seed the db with a deliberately stale/mismatched zone for the
        # same start_ts as the on-disk report -- this string appears nowhere
        # in any file on disk, so it can only show up in the rendered page
        # if that page was actually built from query_runs(), not from a
        # fresh collect_reports() scan of reports_dir.
        stale = copy.deepcopy(report)
        stale["run"]["zone"] = "Stale Zone From DB"
        db_path = tmp_path / "runs.db"
        with Store(db_path) as store:
            store.ingest(stale, source_path=json_path)

        assert main(["index", str(reports_dir), "--db", str(db_path)]) == 0
        html = (reports_dir / "index.html").read_text()
        assert "Stale Zone From DB" in html
        # and it really is DB-sourced, not something collect_reports would
        # ever have produced from the files actually on disk
        assert "Stale Zone From DB" not in json.dumps(collect_reports(reports_dir))

    def test_index_db_is_idempotent_on_rerun(self, tmp_path, report):
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        (reports_dir / "run.json").write_text(json.dumps(report), encoding="utf-8")
        db_path = tmp_path / "runs.db"

        assert main(["index", str(reports_dir), "--db", str(db_path)]) == 0
        assert main(["index", str(reports_dir), "--db", str(db_path)]) == 0
        assert _table_counts(db_path)["runs"] == 1


class TestIndexWithoutDBUnchanged:
    def test_no_db_flag_matches_direct_render(self, tmp_path, report):
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        (reports_dir / "run.json").write_text(json.dumps(report), encoding="utf-8")

        assert main(["index", str(reports_dir)]) == 0
        cli_html = (reports_dir / "index.html").read_text()

        expected = render_index(collect_reports(reports_dir))
        assert cli_html == expected


class TestAnalyzeHistoryDB:
    def test_history_db_alongside_json_html_out(self, log_file, route_string,
                                                 dungeon_data_file, tmp_path, capsys):
        out_dir = tmp_path / "reports"
        db_path = tmp_path / "nested" / "runs.db"  # parent doesn't exist yet
        assert main([
            "analyze", str(log_file),
            "--route", route_string,
            "--dungeon-data", str(dungeon_data_file),
            "--format", "json,html",
            "--out", str(out_dir),
            "--history-db", str(db_path),
        ]) == 0

        assert db_path.exists()
        rows = query_runs(db_path)
        assert len(rows) == 1
        assert rows[0]["zone"] == "Murder Row"
        assert rows[0]["html"] is not None and rows[0]["html"].endswith(".html")
        # normal output still happened too
        assert len(list(out_dir.iterdir())) == 2

    def test_history_db_with_text_only_format(self, log_file, route_string,
                                              dungeon_data_file, tmp_path, capsys):
        db_path = tmp_path / "runs.db"
        assert main([
            "analyze", str(log_file),
            "--route", route_string,
            "--dungeon-data", str(dungeon_data_file),
            "--format", "text",
            "--history-db", str(db_path),
        ]) == 0

        rows = query_runs(db_path)
        assert len(rows) == 1
        assert rows[0]["zone"] == "Murder Row"
        assert rows[0]["file"] is not None
        assert rows[0]["html"] is None

    def test_history_db_without_flag_creates_no_db(self, log_file, route_string,
                                                    dungeon_data_file, tmp_path):
        db_path = tmp_path / "runs.db"
        assert main([
            "analyze", str(log_file),
            "--route", route_string,
            "--dungeon-data", str(dungeon_data_file),
            "--format", "text",
        ]) == 0
        assert not db_path.exists()


class TestReportJsonLast:
    """``report_json`` is kept as the last column of ``runs`` so reading any
    other column never walks a report's overflow pages (2026-09-22: the
    site's run list read the whole 175MB database on a cold disk)."""

    @staticmethod
    def _cols(db_path):
        return [r[1] for r in sqlite3.connect(str(db_path)).execute("PRAGMA table_info(runs)")]

    def test_fresh_database(self, tmp_path):
        db_path = tmp_path / "runs.db"
        Store(db_path).close()
        assert self._cols(db_path)[-1] == "report_json"

    def test_old_database_is_rebuilt_keeping_ids_children_and_sequence(self, tmp_path, report):
        db_path = tmp_path / "runs.db"
        ingest(report, db_path)
        second = copy.deepcopy(report)
        second["run"]["start_ts"] = report["run"]["start_ts"] + 5000
        ingest(second, db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        # Simulate the shape every real database has: a column added by
        # ALTER TABLE after the blob, and a deleted run leaving a gap in ids.
        conn.execute("ALTER TABLE runs ADD COLUMN later_col TEXT")
        conn.execute("UPDATE runs SET later_col = 'kept' WHERE id = 2")
        conn.execute("INSERT INTO runs (zone, start_ts, report_json) VALUES ('Gone', 9e9, '{}')")
        conn.execute("DELETE FROM runs WHERE zone = 'Gone'")
        conn.commit()
        players_before = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        conn.close()
        assert self._cols(db_path)[-1] == "later_col"
        assert players_before > 0

        with Store(db_path) as store:
            assert store.get_report(1)["run"]["zone"] == report["run"]["zone"]

        cols = self._cols(db_path)
        assert cols[-1] == "report_json" and "later_col" in cols
        conn = sqlite3.connect(str(db_path))
        assert [r[0] for r in conn.execute("SELECT id FROM runs ORDER BY id")] == [1, 2]
        assert conn.execute("SELECT later_col FROM runs WHERE id = 2").fetchone()[0] == "kept"
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == players_before
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'runs'").fetchone()[0] == 3
        # the UNIQUE(zone, start_ts) constraint survived
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO runs (zone, start_ts, report_json) "
                         "SELECT zone, start_ts, '{}' FROM runs WHERE id = 1")
        conn.close()
        # a new run never reuses the deleted run's id
        third = copy.deepcopy(report)
        third["run"]["start_ts"] = report["run"]["start_ts"] + 9000
        ingest(third, db_path)
        ids = [r[0] for r in sqlite3.connect(str(db_path)).execute("SELECT id FROM runs ORDER BY id")]
        assert ids == [1, 2, 4]

    def test_deleting_a_run_still_cascades_after_the_rebuild(self, tmp_path, report):
        db_path = tmp_path / "runs.db"
        ingest(report, db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("ALTER TABLE runs ADD COLUMN later_col TEXT")
        conn.commit(); conn.close()
        Store(db_path).close()
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM runs")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
        conn.close()
