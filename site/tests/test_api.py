"""Black-box tests for the postmortem_site FastAPI service, via TestClient
(no live server involved).
"""

from __future__ import annotations

import copy

from postmortem.history.store import query_runs as store_query_runs

from postmortem_site import config as site_config
from postmortem_site import db as site_db_module

TOKEN_X = "token-x-0123456789abcdef"
TOKEN_Y = "token-y-fedcba9876543210"


def _upload(client, report, token=TOKEN_X, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("X-Upload-Token", token)
    return client.post("/api/runs", json=report, headers=headers, **kwargs)


class TestRoundTrip:
    def test_full_round_trip(self, client, report):
        resp = _upload(client, report)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        run_id = body["run_id"]
        assert body["url"] == f"/runs/{run_id}"

        feed = client.get("/runs")
        assert feed.status_code == 200
        assert report["run"]["zone"] in feed.text
        # render_index() builds the <a href> table client-side (in JS) from
        # a JSON blob embedded in the page -- TestClient never executes
        # that JS, so assert on the embedded data itself rather than a
        # rendered <a> tag.
        assert f'"html": "/runs/{run_id}"' in feed.text

        detail = client.get(f"/runs/{run_id}")
        assert detail.status_code == 200
        assert report["run"]["zone"] in detail.text

        api_detail = client.get(f"/api/runs/{run_id}")
        assert api_detail.status_code == 200
        assert api_detail.json() == report


class TestValidation:
    def test_non_json_body(self, client):
        resp = client.post(
            "/api/runs",
            content=b"not json{",
            headers={"X-Upload-Token": TOKEN_X, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_json_not_object(self, client):
        resp = client.post(
            "/api/runs", json=[1, 2, 3], headers={"X-Upload-Token": TOKEN_X}
        )
        assert resp.status_code == 400

    def test_missing_run_key(self, client, report):
        payload = copy.deepcopy(report)
        del payload["run"]
        assert _upload(client, payload).status_code == 400

    def test_missing_dungeon_key(self, client, report):
        payload = copy.deepcopy(report)
        del payload["dungeon"]
        assert _upload(client, payload).status_code == 400

    def test_missing_players_key(self, client, report):
        payload = copy.deepcopy(report)
        del payload["players"]
        assert _upload(client, payload).status_code == 400

    def test_zone_null(self, client, report):
        payload = copy.deepcopy(report)
        payload["run"]["zone"] = None
        assert _upload(client, payload).status_code == 400

    def test_zone_missing(self, client, report):
        payload = copy.deepcopy(report)
        del payload["run"]["zone"]
        assert _upload(client, payload).status_code == 400

    def test_start_ts_null(self, client, report):
        payload = copy.deepcopy(report)
        payload["run"]["start_ts"] = None
        assert _upload(client, payload).status_code == 400

    def test_start_ts_missing(self, client, report):
        payload = copy.deepcopy(report)
        del payload["run"]["start_ts"]
        assert _upload(client, payload).status_code == 400


class TestAuth:
    def test_missing_token_header(self, client, report):
        resp = client.post("/api/runs", json=report)
        assert resp.status_code == 401

    def test_blank_token_header(self, client, report):
        resp = client.post(
            "/api/runs", json=report, headers={"X-Upload-Token": "   "}
        )
        assert resp.status_code == 401


class TestBodySize:
    def test_body_too_large(self, client, report, monkeypatch):
        # Shrink the cap rather than constructing a real 5MB+ payload --
        # keeps the test fast while still exercising the real check.
        monkeypatch.setattr(site_config, "MAX_BODY_BYTES", 100)
        resp = _upload(client, report)
        assert resp.status_code == 413


class TestRateLimit:
    def test_second_upload_same_token_rate_limited(self, client, report, monkeypatch):
        # Isolate the per-token guard: disable the per-IP one so this
        # test can't pass/fail for the wrong reason (TestClient's two
        # calls share one fixed host).
        monkeypatch.setattr(site_config, "IP_MIN_INTERVAL_S", 0)
        assert _upload(client, report).status_code == 200

        other = copy.deepcopy(report)
        other["run"]["zone"] = "Some Other Dungeon"
        other["run"]["start_ts"] = report["run"]["start_ts"] + 100000

        resp = _upload(client, other)  # same token, well within 30s
        assert resp.status_code == 429
        assert "retry_after_s" in resp.json()


class TestOwnershipConflict:
    def test_conflict_then_same_token_updates_in_place(
        self, client, report, monkeypatch
    ):
        # These three uploads all come from TestClient's one fixed host;
        # disable both interval guards so this test is purely about the
        # ownership check, not incidentally blocked by rate limiting.
        monkeypatch.setattr(site_config, "UPLOAD_MIN_INTERVAL_S", 0)
        monkeypatch.setattr(site_config, "IP_MIN_INTERVAL_S", 0)

        resp1 = _upload(client, report, token=TOKEN_X)
        assert resp1.status_code == 200
        run_id = resp1.json()["run_id"]

        conflicting = copy.deepcopy(report)
        conflicting["players"] = []  # distinguishable from the original

        resp2 = _upload(client, conflicting, token=TOKEN_Y)
        assert resp2.status_code == 409

        unchanged = client.get(f"/api/runs/{run_id}").json()
        assert unchanged["players"] == report["players"]

        resp3 = _upload(client, conflicting, token=TOKEN_X)  # same token as owner
        assert resp3.status_code == 200
        assert resp3.json()["run_id"] == run_id

        updated = client.get(f"/api/runs/{run_id}").json()
        assert updated["players"] == []

        feed = client.get("/api/runs").json()
        assert len(feed) == 1


class TestMisc:
    def test_nonexistent_run_404(self, client):
        resp = client.get("/runs/999999")
        assert resp.status_code == 404

    def test_root_is_a_real_landing_page(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200
        assert "<html" in resp.text
        assert 'class="site-header"' in resp.text  # shared nav present
        assert 'href="/upload"' in resp.text
        assert 'href="/runs"' in resp.text

    def test_healthz(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_about_page(self, client):
        resp = client.get("/about")
        assert resp.status_code == 200
        assert "X-Upload-Token" in resp.text


class TestFeedShapeContract:
    def test_feed_rows_matches_query_runs_keys(self, client, report, site_db):
        # Pins db.feed_rows()'s row shape against
        # history.store._to_index_row()'s -- what would catch the two
        # drifting out of sync in the future.
        assert _upload(client, report).status_code == 200

        conn = site_db_module.connect(str(site_db))
        try:
            feed = site_db_module.feed_rows(conn, limit=10)
        finally:
            conn.close()
        assert len(feed) == 1

        store_rows = store_query_runs(site_db)
        assert len(store_rows) == 1

        # "html" differs in *value* by design: feed_rows() overrides it
        # to point at this service's own /runs/{id} page, since an
        # uploaded run never has an on-disk html sibling for
        # _to_index_row()'s usual html_name-based derivation to find.
        # Only the key *set* needs to match.
        assert set(feed[0].keys()) == set(store_rows[0].keys())


class TestSecurityHeaders:
    def test_headers_present_on_html_response(self, client):
        resp = client.get("/runs")
        assert resp.status_code == 200
        assert "Content-Security-Policy" in resp.headers
        assert resp.headers["X-Content-Type-Options"] == "nosniff"

    def test_csp_allows_google_fonts(self, client):
        """The landing/about/upload pages load Chakra Petch/IBM Plex Sans
        from Google Fonts -- the site's CSP is default-src 'none' by
        design, so without an explicit allowance those requests would be
        silently blocked by the browser and every page would silently
        fall back to system fonts with no visible error anywhere."""
        csp = client.get("/").headers["Content-Security-Policy"]
        assert "fonts.googleapis.com" in csp
        assert "fonts.gstatic.com" in csp


class TestSiteChrome:
    """The shared nav/footer (app.py's _site_nav/_site_footer/_page/
    _inject_chrome) appears consistently across every page -- including
    /runs and /runs/{id}, whose actual content comes from report/index.py
    and report/html.py (reused verbatim by the CLI and desktop app, so
    those two modules themselves are never modified -- see app.py's
    module note above _CHROME_STYLE)."""

    PAGES_AND_ACTIVE = [
        ("/", "home"),
        ("/runs", "runs"),
        ("/about", "about"),
        ("/upload", "upload"),
    ]

    def test_nav_and_footer_present_on_every_static_page(self, client):
        for path, active in self.PAGES_AND_ACTIVE:
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert 'class="site-header"' in resp.text, path
            assert 'class="site-footer"' in resp.text, path
            # "active" appears either as class="active" (Home/Browse
            # Runs/About) or class="cta active" (the Upload CTA) --
            # either way, some nav link is marked current.
            assert "active" in resp.text, path

    def test_active_nav_link_matches_current_page(self, client):
        resp = client.get("/")
        assert '<a href="/" class="active">' in resp.text

        resp = client.get("/runs")
        assert '<a href="/runs" class="active">' in resp.text

        resp = client.get("/about")
        assert '<a href="/about" class="active">' in resp.text

        resp = client.get("/upload")
        assert 'class="cta active"' in resp.text

    def test_run_detail_page_has_chrome_and_keeps_its_own_report(self, client, report):
        resp = _upload(client, report)
        run_id = resp.json()["run_id"]

        detail = client.get(f"/runs/{run_id}")
        assert detail.status_code == 200
        assert 'class="site-header"' in detail.text
        assert 'class="site-footer"' in detail.text
        # the underlying report/html.py content is still there, unmodified
        assert report["run"]["zone"] in detail.text
        assert '"report-data"' in detail.text  # report/html.py's embedded JSON

    def test_every_page_loads_the_shared_fonts(self, client):
        for path, _ in self.PAGES_AND_ACTIVE:
            resp = client.get(path)
            assert "fonts.googleapis.com" in resp.text, path

    def test_landing_page_shows_empty_state_with_no_runs(self, client):
        resp = client.get("/")
        assert "be the first" in resp.text.lower()

    def test_landing_page_shows_recent_run_and_stats(self, client, report):
        resp = _upload(client, report)
        run_id = resp.json()["run_id"]

        landing = client.get("/")
        assert report["run"]["zone"] in landing.text
        assert f'href="/runs/{run_id}"' in landing.text
        # stat-strip total ("1" run tracked) actually reflects real data
        assert "<b>1</b>" in landing.text
