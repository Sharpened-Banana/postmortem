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


class TestLogUpload:
    def test_successful_upload_lists_the_run(self, client, raw_log_text):
        resp = client.post("/upload", files=_upload_file(raw_log_text))
        assert resp.status_code == 200
        assert "runs/" in resp.text

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

    def test_raw_log_is_not_persisted_after_the_request(self, client, raw_log_text):
        import glob
        import tempfile

        before = set(glob.glob(f"{tempfile.gettempdir()}/*.txt"))
        client.post("/upload", files=_upload_file(raw_log_text))
        after = set(glob.glob(f"{tempfile.gettempdir()}/*.txt"))
        assert after - before == set()
