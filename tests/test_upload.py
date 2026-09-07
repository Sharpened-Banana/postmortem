"""Client-side upload plumbing (postmortem.upload): the local
upload-token file and POSTing a report to a public postmortem site.
"""

from __future__ import annotations

import hashlib
import io
import json
import ssl
import urllib.error

import pytest

import postmortem.upload as upload
from postmortem.cli import main


@pytest.fixture()
def isolated_config_dir(tmp_path, monkeypatch):
    """A config dir under tmp_path, with appdirs.config_dir() patched to
    return it -- never touches the real user's home directory."""
    fake_dir = tmp_path / "postmortem-config"
    monkeypatch.setattr(upload.appdirs, "config_dir", lambda: fake_dir)
    return fake_dir


class TestTokenPersistence:
    def test_first_call_creates_file_and_returns_token(self, isolated_config_dir):
        assert not isolated_config_dir.exists()
        token = upload.load_or_create_token()
        assert isinstance(token, str) and token
        token_file = isolated_config_dir / upload.TOKEN_FILENAME
        assert token_file.exists()
        assert json.loads(token_file.read_text())["token"] == token

    def test_second_call_returns_same_token(self, isolated_config_dir):
        first = upload.load_or_create_token()
        second = upload.load_or_create_token()
        assert first == second

    def test_corrupt_token_file_falls_back_to_a_fresh_token(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        (isolated_config_dir / upload.TOKEN_FILENAME).write_text(
            "{not valid json", encoding="utf-8",
        )
        token = upload.load_or_create_token()
        assert isinstance(token, str) and token

    def test_empty_token_field_falls_back_to_a_fresh_token(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        (isolated_config_dir / upload.TOKEN_FILENAME).write_text(
            json.dumps({"token": ""}), encoding="utf-8",
        )
        token = upload.load_or_create_token()
        assert isinstance(token, str) and token

    def test_missing_token_field_falls_back_to_a_fresh_token(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        (isolated_config_dir / upload.TOKEN_FILENAME).write_text(
            json.dumps({"nope": "not-a-token"}), encoding="utf-8",
        )
        token = upload.load_or_create_token()
        assert isinstance(token, str) and token

    def test_non_object_json_falls_back_to_a_fresh_token(self, isolated_config_dir):
        isolated_config_dir.mkdir(parents=True)
        (isolated_config_dir / upload.TOKEN_FILENAME).write_text(
            "[1, 2, 3]", encoding="utf-8",
        )
        token = upload.load_or_create_token()
        assert isinstance(token, str) and token


class _FakeHTTPResponse:
    """Minimal stand-in for the object ``urlopen`` yields as a context
    manager: readable bytes plus context-manager support."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class TestUploadReport:
    def test_success_returns_parsed_response(self, monkeypatch):
        response_body = {"ok": True, "run_id": 42, "url": "/runs/42"}

        def fake_urlopen(request, timeout=None, context=None):
            assert request.get_header("X-upload-token") == "tok-123"
            assert request.get_header("Content-type") == "application/json"
            return _FakeHTTPResponse(json.dumps(response_body).encode("utf-8"))

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.upload_report({"run": {}}, "https://example.com",
                                       token="tok-123")
        assert result == response_body

    def test_strips_trailing_slash_and_appends_api_runs(self, monkeypatch):
        seen = {}

        def fake_urlopen(request, timeout=None, context=None):
            seen["url"] = request.full_url
            return _FakeHTTPResponse(json.dumps({"ok": True, "url": "/runs/1"}).encode())

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        upload.upload_report({}, "https://example.com/", token="tok")
        assert seen["url"] == "https://example.com/api/runs"

    def test_default_token_loads_or_creates_one(self, monkeypatch, isolated_config_dir):
        seen = {}

        def fake_urlopen(request, timeout=None, context=None):
            seen["token"] = request.get_header("X-upload-token")
            return _FakeHTTPResponse(json.dumps({"ok": True, "url": "/runs/1"}).encode())

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.upload_report({}, "https://example.com")
        assert result["ok"] is True
        assert seen["token"] == upload.load_or_create_token()

    def test_http_error_with_json_body_returns_parsed_body(self, monkeypatch):
        error_body = {"ok": False, "error": "duplicate run"}

        def fake_urlopen(request, timeout=None, context=None):
            raise urllib.error.HTTPError(
                request.full_url, 409, "Conflict", hdrs=None,
                fp=io.BytesIO(json.dumps(error_body).encode("utf-8")),
            )

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.upload_report({}, "https://example.com", token="tok")
        assert result == error_body

    def test_http_error_with_non_json_body_falls_back(self, monkeypatch):
        def fake_urlopen(request, timeout=None, context=None):
            raise urllib.error.HTTPError(
                request.full_url, 500, "Internal Server Error", hdrs=None,
                fp=io.BytesIO(b"<html>oops</html>"),
            )

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.upload_report({}, "https://example.com", token="tok")
        assert result == {"ok": False, "error": "HTTP 500: Internal Server Error"}

    def test_url_error_returns_fallback_dict(self, monkeypatch):
        def fake_urlopen(request, timeout=None, context=None):
            raise urllib.error.URLError("nodename nor servname provided")

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.upload_report({}, "https://example.com", token="tok")
        assert result == {"ok": False,
                           "error": "nodename nor servname provided"}

    def test_passes_a_certifi_backed_ssl_context_to_urlopen(self, monkeypatch):
        # Real bug (2026-09-03): "Upload to site" hit
        # CERTIFICATE_VERIFY_FAILED on a packaged desktop build because
        # urlopen()'s default context couldn't find a CA trust store.
        seen = {}

        def fake_urlopen(request, timeout=None, context=None):
            seen["context"] = context
            return _FakeHTTPResponse(json.dumps({"ok": True, "url": "/runs/1"}).encode())

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        upload.upload_report({}, "https://example.com", token="tok")
        assert isinstance(seen["context"], ssl.SSLContext)

    def test_never_raises_on_any_of_the_above(self, monkeypatch):
        # Belt-and-suspenders: every branch above already asserts a
        # returned dict rather than a raised exception, but drive one
        # more failure mode (bad JSON in an otherwise-2xx response)
        # through the same "must return, never raise" contract.
        def fake_urlopen(request, timeout=None, context=None):
            return _FakeHTTPResponse(b"not json")

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.upload_report({}, "https://example.com", token="tok")
        assert result.get("ok") is False


class TestSiteBaseUrl:
    """Any URL a user is likely to paste as the "site URL" must resolve
    to the site's origin. Real bug (2026-09-02): Settings held the
    ``/upload`` *page* URL and every Watch Live upload silently 404'd at
    ``<site>/upload/api/runs`` -- a timed key never reached the site."""

    @pytest.mark.parametrize("pasted", [
        "https://postmortem-mplus.fly.dev",
        "https://postmortem-mplus.fly.dev/",
        "https://postmortem-mplus.fly.dev/upload",
        "https://postmortem-mplus.fly.dev/upload/",
        "https://postmortem-mplus.fly.dev/UPLOAD",
        "https://postmortem-mplus.fly.dev/runs",
        "https://postmortem-mplus.fly.dev/api/runs",
        "https://postmortem-mplus.fly.dev/about",
        "  https://postmortem-mplus.fly.dev/upload  ",
    ])
    def test_page_urls_normalize_to_the_origin(self, pasted):
        assert upload.site_base_url(pasted) == "https://postmortem-mplus.fly.dev"

    def test_a_nested_deployment_keeps_its_prefix(self):
        assert upload.site_base_url("https://host/postmortem/upload") == "https://host/postmortem"
        assert upload.site_base_url("https://host/postmortem") == "https://host/postmortem"

    def test_upload_from_the_upload_page_url_hits_api_runs(self, monkeypatch):
        seen = {}

        def fake_urlopen(request, timeout=None, context=None):
            seen["url"] = request.full_url
            return _FakeHTTPResponse(json.dumps({"ok": True, "url": "/runs/16"}).encode())

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.upload_report({"run": {}}, "https://postmortem-mplus.fly.dev/upload",
                                       token="tok")
        assert seen["url"] == "https://postmortem-mplus.fly.dev/api/runs"
        assert result["ok"] is True


class TestCLIUploadFlag:
    """cmd_analyze's --upload wiring: uploading is a best-effort bonus
    step that never changes the command's exit code or suppresses its
    normal output, mirroring how --raiderio enrichment failures are
    already handled."""

    def test_upload_failure_does_not_break_analysis(self, log_file, monkeypatch, capsys):
        monkeypatch.setattr(
            "postmortem.upload.upload_report",
            lambda report, url, **kwargs: {"ok": False, "error": "offline"},
        )
        exit_code = main(["analyze", str(log_file), "--upload", "https://example.com"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "MYTHIC+ POST-MORTEM" in captured.out
        assert "upload failed: offline" in captured.err

    def test_upload_success_prints_url(self, log_file, monkeypatch, capsys):
        monkeypatch.setattr(
            "postmortem.upload.upload_report",
            lambda report, url, **kwargs: {"ok": True, "run_id": 7, "url": "/runs/7"},
        )
        exit_code = main(["analyze", str(log_file), "--upload", "https://example.com"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "uploaded: https://example.com/runs/7" in out

    def test_upload_token_flag_is_passed_through(self, log_file, monkeypatch):
        seen = {}

        def fake_upload_report(report, url, **kwargs):
            seen.update(kwargs)
            return {"ok": True, "run_id": 1, "url": "/runs/1"}

        monkeypatch.setattr("postmortem.upload.upload_report", fake_upload_report)
        main(["analyze", str(log_file), "--upload", "https://example.com",
              "--upload-token", "shared-team-token"])
        assert seen["token"] == "shared-team-token"

    def test_no_upload_flag_never_touches_upload_module(self, log_file, monkeypatch):
        # Sanity check for the "local import keeps upload.py off the hot
        # path" claim: analyze without --upload must not even try to call
        # upload_report.
        def boom(*args, **kwargs):
            raise AssertionError("upload_report should not be called")

        monkeypatch.setattr("postmortem.upload.upload_report", boom)
        assert main(["analyze", str(log_file)]) == 0


class TestStartDeviceLink:
    """start_device_link (phase 3): begins a device-code link to a
    Postmortem account -- posts this install's token *hash*, never the
    raw token, and never raises."""

    def test_success_returns_the_sites_response(self, monkeypatch):
        response_body = {"ok": True, "code": "ABCD-1234", "poll_token": "poll-1",
                          "verify_url": "https://example.com/link?code=ABCD-1234",
                          "expires_in": 600}

        def fake_urlopen(request, timeout=None, context=None):
            assert request.get_header("Content-type") == "application/json"
            body = json.loads(request.data.decode("utf-8"))
            assert body["token_hash"] == hashlib.sha256(b"tok-123").hexdigest()
            assert body["label"] == "my label"
            return _FakeHTTPResponse(json.dumps(response_body).encode("utf-8"))

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.start_device_link(
            "https://example.com", token="tok-123", label="my label",
        )
        assert result == response_body

    def test_default_token_loads_or_creates_one(self, monkeypatch, isolated_config_dir):
        seen = {}

        def fake_urlopen(request, timeout=None, context=None):
            seen["hash"] = json.loads(request.data.decode("utf-8"))["token_hash"]
            return _FakeHTTPResponse(json.dumps({"ok": True}).encode())

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        upload.start_device_link("https://example.com")
        token = upload.load_or_create_token()
        assert seen["hash"] == hashlib.sha256(token.encode()).hexdigest()

    def test_accounts_disabled_404_comes_back_as_the_sites_error_body(self, monkeypatch):
        error_body = {"error": "accounts are not enabled on this site"}

        def fake_urlopen(request, timeout=None, context=None):
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", hdrs=None,
                fp=io.BytesIO(json.dumps(error_body).encode("utf-8")),
            )

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.start_device_link("https://example.com", token="tok")
        assert result == error_body

    def test_network_failure_never_raises(self, monkeypatch):
        def fake_urlopen(request, timeout=None, context=None):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.start_device_link("https://example.com", token="tok")
        assert result == {"ok": False, "error": "no route to host"}


class TestPollDeviceLink:
    def test_pending_and_approved_pass_through(self, monkeypatch):
        def fake_urlopen(request, timeout=None, context=None):
            assert "poll_token=poll-1" in request.full_url
            return _FakeHTTPResponse(json.dumps(
                {"status": "approved", "display_name": "Zebra#1234"}
            ).encode())

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.poll_device_link("https://example.com", "poll-1")
        assert result == {"status": "approved", "display_name": "Zebra#1234"}

    def test_expired_404_body_passes_through(self, monkeypatch):
        def fake_urlopen(request, timeout=None, context=None):
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", hdrs=None,
                fp=io.BytesIO(json.dumps({"status": "expired"}).encode()),
            )

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.poll_device_link("https://example.com", "poll-1")
        assert result == {"status": "expired"}

    def test_network_failure_is_a_status_error_not_an_exception(self, monkeypatch):
        def fake_urlopen(request, timeout=None, context=None):
            raise urllib.error.URLError("timed out")

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.poll_device_link("https://example.com", "poll-1")
        assert result == {"status": "error", "error": "timed out"}


class TestWhoami:
    def test_linked_account_passes_through(self, monkeypatch):
        def fake_urlopen(request, timeout=None, context=None):
            assert request.get_header("X-upload-token") == "tok-123"
            return _FakeHTTPResponse(
                json.dumps({"linked": True, "display_name": "Zebra#1234"}).encode()
            )

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.whoami("https://example.com", token="tok-123")
        assert result == {"linked": True, "display_name": "Zebra#1234"}

    def test_not_linked(self, monkeypatch):
        def fake_urlopen(request, timeout=None, context=None):
            return _FakeHTTPResponse(json.dumps({"linked": False}).encode())

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        assert upload.whoami("https://example.com", token="tok") == {"linked": False}

    def test_network_failure_is_treated_as_not_linked(self, monkeypatch):
        def fake_urlopen(request, timeout=None, context=None):
            raise urllib.error.URLError("nope")

        monkeypatch.setattr(upload.urllib.request, "urlopen", fake_urlopen)
        result = upload.whoami("https://example.com", token="tok")
        assert result["linked"] is False
        assert "nope" in result["error"]
