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
from postmortem.history.store import Store, ingest, query_runs
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
