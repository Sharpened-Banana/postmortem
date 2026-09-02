"""Tests for POST/GET /upload -- the raw-combat-log upload path that lets
a tester get a run onto the site without the desktop app or CLI at all.

Uses the raw_log_text fixture (site/tests/conftest.py, built from the
main suite's build_run_log()) to get a real, valid combat log's text --
not a hand-rolled fixture -- so these tests exercise the exact same
parse_file/segment_runs/analyze_run pipeline a real upload would.
"""

from __future__ import annotations

import io


def _upload_file(text: str):
    return {"logfile": ("WoWCombatLog.txt", io.BytesIO(text.encode("utf-8")), "text/plain")}


class TestUploadForm:
    def test_get_upload_returns_form(self, client):
        resp = client.get("/upload")
        assert resp.status_code == 200
        assert "<form" in resp.text
        assert 'enctype="multipart/form-data"' in resp.text

    def test_form_shows_a_wait_message_on_submit(self, client):
        # A large log's upload+analysis can take several minutes with no
        # visible change on screen (a plain multipart POST, not XHR/fetch)
        # -- a real report (2026-09-02) described that as "crashes the
        # webpage". This doesn't change the upload mechanism, just makes
        # the wait visible instead of looking dead.
        resp = client.get("/upload")
        assert "upload-wait-note" in resp.text
        assert "submit" in resp.text


class TestLogUpload:
    def test_successful_upload_lists_the_run(self, client, raw_log_text):
        resp = client.post("/upload", files=_upload_file(raw_log_text))
        assert resp.status_code == 200
        assert "runs/" in resp.text

    def test_multi_run_log_does_not_hit_a_database_lock(self, client, raw_log_text):
        """Regression test for a real bug (2026-08-31): _handle_log_upload
        committed `conn` once at the end of the whole batch instead of once
        per run. Python's sqlite3 module opens an implicit write
        transaction on the first INSERT and holds it open until commit(),
        so that one connection's still-open transaction (from
        record_upload()'s write on the first run) blocked every
        subsequent run's own Store(...) connection (a separate connection
        -- see db.connect()'s docstring) from writing at all, failing
        every run after the first in any multi-run batch with "database
        is locked". No earlier test caught this because none uploaded a
        log with more than one completed run in it. Concatenating two
        copies of the same synthetic run is enough to reproduce it (they
        dedupe to one feed row via the same X-Upload-Token, which is
        fine -- this test is about the write succeeding at all, not
        about ending up with two distinct rows)."""
        two_runs = raw_log_text + raw_log_text
        resp = client.post("/upload", files=_upload_file(two_runs))
        assert resp.status_code == 200
        assert "upload failed" not in resp.text.lower()
        assert "database is locked" not in resp.text.lower()

        feed = client.get("/api/runs").json()
        assert len(feed) == 1

    def test_upload_mints_a_cookie_and_reuses_it(self, client, raw_log_text):
        assert "pm_upload_token" not in client.cookies
        client.post("/upload", files=_upload_file(raw_log_text))
        assert "pm_upload_token" in client.cookies

    def test_no_completed_runs_in_log_is_not_an_error(self, client):
        garbage = "8/30/2026 20:00:00.000-4  SPELL_CAST_SUCCESS,a,b,c,d\n"
        resp = client.post("/upload", files=_upload_file(garbage))
        assert resp.status_code == 200
        assert "no completed" in resp.text.lower()
        assert client.get("/api/runs").json() == []

    def test_a_key_that_started_but_never_ended_gets_a_specific_message(self, client):
        # Regression test for a real report (2026-09-01): a real 22-minute
        # King's Rest key with a genuine CHALLENGE_MODE_START and no
        # CHALLENGE_MODE_END got the exact same generic "no completed
        # runs" message as a log with zero M+ content at all -- confusing
        # when you know you played a real key. No ZONE_CHANGE follows here,
        # so this is the ambiguous case (RunSegment.likely_abandoned is
        # False) -- ended-normally-but-cut-off and abandoned look
        # identical without one.
        started_not_ended = (
            '8/31/2026 22:50:23.411-4  CHALLENGE_MODE_START,"Kings\' Rest",1762,249,10,[158,9,10]\n'
        )
        resp = client.post("/upload", files=_upload_file(started_not_ended))
        assert resp.status_code == 200
        text = resp.text.lower()
        assert "started but never finished" in text
        assert "kings" in text  # html.escape() turns the apostrophe into &#x27;
        assert "no sign of the group leaving" in text
        assert client.get("/api/runs").json() == []

    def test_an_abandoned_key_gets_a_confident_message(self, client):
        # Same real report, but with the actual follow-up line the real
        # log had: a ZONE_CHANGE out of the instance right after the
        # unfinished CHALLENGE_MODE_START (see RunSegment.likely_abandoned
        # -- confirmed against the real reported log directly, not just
        # this synthetic reproduction). The message should sound confident
        # here, not hedge between two equally-likely explanations.
        abandoned = (
            '8/31/2026 22:50:23.411-4  CHALLENGE_MODE_START,"Kings\' Rest",1762,249,10,[158,9,10]\n'
            '8/31/2026 23:12:28.450-4  ZONE_CHANGE,1,"Stormwind City",1\n'
        )
        resp = client.post("/upload", files=_upload_file(abandoned))
        assert resp.status_code == 200
        text = resp.text.lower()
        assert "started but never finished" in text
        assert "left the dungeon without finishing" in text
        assert "no sign of the group leaving" not in text
        assert client.get("/api/runs").json() == []

    def test_second_upload_from_same_browser_is_rate_limited(self, client, raw_log_text):
        first = client.post("/upload", files=_upload_file(raw_log_text))
        assert first.status_code == 200

        second = client.post("/upload", files=_upload_file(raw_log_text))
        assert second.status_code == 429

    def test_file_over_the_size_cap_is_rejected(self, client, raw_log_text, monkeypatch):
        from postmortem_site import config as site_config

        monkeypatch.setattr(site_config, "MAX_LOG_BYTES", 10)
        resp = client.post("/upload", files=_upload_file(raw_log_text))
        assert resp.status_code == 413

    def test_a_run_over_the_per_run_event_cap_is_reported_not_crashed(
        self, client, raw_log_text, monkeypatch,
    ):
        """Regression test for a real bug (2026-08-31, same day as the
        MAX_LOG_BYTES cap itself): the first version of the per-run
        memory-safety fix enforced MAX_LOG_BYTES against the whole raw
        upload's total byte count -- which rejected perfectly safe
        multi-key session logs just for having a large total size, even
        though each individual run was well within the real memory
        ceiling. The real fix caps each run independently, during
        parsing (segment_runs()'s max_run_events, config.MAX_RUN_EVENTS),
        so a batch with one oversized run still succeeds overall --
        that one run is reported as a failed/skipped run, not a crash or
        an outright rejection of the whole upload."""
        from postmortem_site import config as site_config

        monkeypatch.setattr(site_config, "MAX_RUN_EVENTS", 3)
        resp = client.post("/upload", files=_upload_file(raw_log_text))
        assert resp.status_code == 200
        assert "database is locked" not in resp.text.lower()
        assert "too long" in resp.text.lower()

        feed = client.get("/api/runs").json()
        assert feed == []

    def test_raw_log_is_not_persisted_after_the_request(self, client, raw_log_text):
        import glob
        import tempfile

        before = set(glob.glob(f"{tempfile.gettempdir()}/*.txt"))
        client.post("/upload", files=_upload_file(raw_log_text))
        after = set(glob.glob(f"{tempfile.gettempdir()}/*.txt"))
        assert after - before == set()

    def test_large_upload_streams_correctly_across_many_chunks(self, client, raw_log_text, monkeypatch):
        """upload_log() now reads in fixed-size chunks instead of one
        logfile.read(cap) call (a real fix, not a hypothetical -- the old
        approach held the whole upload in memory, which is what made
        raising MAX_LOG_BYTES for a real multi-hour session's log unsafe
        on this service's 512MB VM). Shrinks the chunk size drastically
        so even this small fixture log spans dozens of chunks, to
        actually exercise the loop boundary instead of trivially
        finishing in one iteration."""
        from postmortem_site import app as app_module

        monkeypatch.setattr(app_module, "_UPLOAD_CHUNK_BYTES", 64)
        resp = client.post("/upload", files=_upload_file(raw_log_text))
        assert resp.status_code == 200
        assert "runs/" in resp.text
        assert client.get("/api/runs").json() != []


class TestMdtIntegration:
    """The bundled dungeon-data store (config.DUNGEON_DATA_PATH) makes
    forces progress populate automatically for every /upload run, with
    no route needed; pasting a route additionally gets route-adherence
    comparison. isolated_dungeon_store (conftest.py, autouse) keeps this
    class's "no data" tests from accidentally picking up real bundled
    data; dungeon_store_with_data opts specific tests back into a store
    that actually matches raw_log_text's synthetic run.
    """

    def test_forces_do_not_populate_without_bundled_dungeon_data(self, client, raw_log_text):
        client.post("/upload", files=_upload_file(raw_log_text))
        report = client.get("/api/runs/1").json()
        assert report["forces"]["required"] is None

    def test_forces_populate_automatically_with_no_route_pasted(
        self, client, raw_log_text, dungeon_store_with_data,
    ):
        resp = client.post("/upload", files=_upload_file(raw_log_text))
        assert resp.status_code == 200
        report = client.get("/api/runs/1").json()
        assert report["forces"]["required"] is not None
        assert report["forces"]["required"] > 0
        # no route pasted -> no adherence comparison, forces alone still work
        assert "comparison" not in report or report["comparison"].get("error")

    def test_pasted_route_adds_adherence_comparison(
        self, client, raw_log_text, route_string, dungeon_store_with_data,
    ):
        resp = client.post(
            "/upload", files=_upload_file(raw_log_text), data={"route": route_string},
        )
        assert resp.status_code == 200
        report = client.get("/api/runs/1").json()
        assert report["forces"]["required"] is not None
        assert "adherence_pct" in report["comparison"]

    def test_bad_route_string_is_a_soft_failure_not_a_rejected_upload(
        self, client, raw_log_text, dungeon_store_with_data,
    ):
        resp = client.post(
            "/upload", files=_upload_file(raw_log_text),
            data={"route": "not a valid mdt export string"},
        )
        assert resp.status_code == 200
        assert "route comparison" in resp.text.lower()
        # the run still got uploaded despite the bad route
        assert client.get("/api/runs").json() != []
        report = client.get("/api/runs/1").json()
        assert report["forces"]["required"] is not None  # bundled data still applied
        assert "comparison" not in report or report["comparison"].get("error")
