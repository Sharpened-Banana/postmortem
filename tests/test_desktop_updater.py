"""Tests for postmortem.desktop.updater -- the auto-update logic that
checks GitHub Releases for a newer alpha-desktop-N build, downloads it,
and hands off to a detached helper script to swap it into place.

apply_update_and_relaunch()'s actual "wait for this process to exit,
then swap directories and relaunch" behavior can't be exercised for
real here (it needs a genuinely separate, genuinely frozen process) --
these tests cover everything short of that: version comparison, asset
selection, URL trust checks, zip extraction/validation (including path-
traversal rejection), and that the hand-off itself (script content,
subprocess.Popen args) is well-formed, with subprocess.Popen mocked out
so nothing is actually spawned.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

from postmortem.desktop import updater


# -- SSL context (frozen-build cert bundle) ----------------------------


class TestSSLContext:
    """Real report (2026-09-01): the update banner never appeared on a
    real installed macOS build, with no visible error anywhere. Root
    cause, confirmed with a real PyInstaller build: a frozen app has no
    CA certificate bundle of its own, so urllib.request.urlopen() over
    https:// raised ssl.SSLCertVerificationError (wrapped as a
    urllib.error.URLError) on every single call -- caught and silently
    turned into None by _default_fetcher's own except clause (by
    design, so a real network hiccup can't crash the update check),
    which made the whole feature look like it was just quietly doing
    nothing. certifi ships a real CA bundle file PyInstaller's own
    built-in hook bundles automatically the moment certifi is imported
    anywhere in the app -- confirmed with a real local PyInstaller
    build that a plain https:// GitHub API call fails without this and
    succeeds with it.
    """

    def test_module_level_ssl_context_is_set_up_from_certifi(self):
        # certifi is a real desktop-extra dependency (pyproject.toml) --
        # this is really just confirming the module-level try/except
        # around `import certifi` actually resolved to the "have it"
        # branch in this environment, not the ImportError fallback.
        assert updater._SSL_CONTEXT is not None

    def test_default_fetcher_passes_the_ssl_context_to_urlopen(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["context"] = context
            raise updater.urllib.error.URLError("stop here, this test only cares about the call args")

        monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)
        assert updater._default_fetcher("https://api.github.com/x") is None
        assert captured["context"] is updater._SSL_CONTEXT

    def test_download_update_passes_the_ssl_context_to_urlopen(self, monkeypatch, tmp_path):
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["context"] = context
            raise updater.urllib.error.URLError("stop here, this test only cares about the call args")

        monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(updater.urllib.error.URLError):
            updater.download_update(
                "https://github.com/x/y/releases/download/t/a.zip", tmp_path / "out.zip",
            )
        assert captured["context"] is updater._SSL_CONTEXT


# -- check_for_update ---------------------------------------------------


class TestCheckForUpdate:
    def test_dev_build_never_reports_an_update(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "dev")
        fetcher = lambda url: {"tag_name": "alpha-desktop-999", "assets": []}
        assert updater.check_for_update(fetcher=fetcher) is None

    def test_no_network_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "alpha-desktop-5")
        assert updater.check_for_update(fetcher=lambda url: None) is None

    def test_same_or_older_latest_release_reports_nothing(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "alpha-desktop-5")
        fetcher = lambda url: {"tag_name": "alpha-desktop-5", "assets": []}
        assert updater.check_for_update(fetcher=fetcher) is None
        fetcher = lambda url: {"tag_name": "alpha-desktop-3", "assets": []}
        assert updater.check_for_update(fetcher=fetcher) is None

    def test_newer_release_with_a_matching_asset_is_reported(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "alpha-desktop-5")
        monkeypatch.setattr(sys, "platform", "darwin")
        fetcher = lambda url: {
            "tag_name": "alpha-desktop-8",
            "body": "some release notes",
            "assets": [
                {"name": "Postmortem-macos.zip", "browser_download_url": "https://github.com/x/y/releases/download/alpha-desktop-8/Postmortem-macos.zip"},
                {"name": "Postmortem-windows.zip", "browser_download_url": "https://github.com/x/y/releases/download/alpha-desktop-8/Postmortem-windows.zip"},
            ],
        }
        result = updater.check_for_update(fetcher=fetcher)
        assert result == {
            "tag": "alpha-desktop-8",
            "download_url": "https://github.com/x/y/releases/download/alpha-desktop-8/Postmortem-macos.zip",
            "notes": "some release notes",
        }

    def test_newer_release_without_this_platforms_asset_reports_nothing(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "alpha-desktop-5")
        monkeypatch.setattr(sys, "platform", "darwin")
        fetcher = lambda url: {
            "tag_name": "alpha-desktop-8",
            "assets": [{"name": "Postmortem-windows.zip", "browser_download_url": "https://x/y.zip"}],
        }
        assert updater.check_for_update(fetcher=fetcher) is None

    def test_malformed_tag_reports_nothing(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "alpha-desktop-5")
        fetcher = lambda url: {"tag_name": "v2.0.0", "assets": []}
        assert updater.check_for_update(fetcher=fetcher) is None

    def test_unsupported_platform_reports_nothing(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "alpha-desktop-5")
        monkeypatch.setattr(sys, "platform", "linux")
        fetcher = lambda url: {"tag_name": "alpha-desktop-8", "assets": []}
        assert updater.check_for_update(fetcher=fetcher) is None


# -- trusted-URL check ----------------------------------------------------


class TestTrustedDownloadUrl:
    @pytest.mark.parametrize("url", [
        "https://github.com/Sharpened-Banana/postmortem/releases/download/alpha-desktop-8/Postmortem-macos.zip",
        "https://objects.githubusercontent.com/foo/bar",
    ])
    def test_trusted_hosts_pass(self, url):
        assert updater._is_trusted_download_url(url) is True

    @pytest.mark.parametrize("url", [
        "http://github.com/x/y.zip",  # not https
        "https://evil.example.com/github.com/x.zip",
        "https://github.com.evil.example.com/x.zip",
        "not-a-url-at-all",
        "",
    ])
    def test_untrusted_urls_are_rejected(self, url):
        assert updater._is_trusted_download_url(url) is False

    def test_download_update_refuses_an_untrusted_url_without_any_network_call(self, tmp_path):
        with pytest.raises(ValueError):
            updater.download_update("https://evil.example.com/x.zip", tmp_path / "out.zip")


# -- extraction / validation ------------------------------------------------


def _build_zip(tmp_path, entries: dict[str, bytes]) -> Path:
    zip_path = tmp_path / "update.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return zip_path


class TestExtractUpdate:
    def test_macos_bundle_extracts_and_validates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        zip_path = _build_zip(tmp_path, {
            "Postmortem.app/Contents/MacOS/Postmortem": b"fake binary",
            "Postmortem.app/Contents/Info.plist": b"<plist/>",
        })
        staging = tmp_path / "staging"
        staging.mkdir()
        result = updater.extract_update(zip_path, staging)
        assert result == staging / "Postmortem.app"
        assert (result / "Contents" / "MacOS" / "Postmortem").exists()

    def test_windows_install_extracts_and_validates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        zip_path = _build_zip(tmp_path, {
            "Postmortem.exe": b"fake exe",
            "_internal/base_library.zip": b"fake",
        })
        staging = tmp_path / "staging"
        staging.mkdir()
        result = updater.extract_update(zip_path, staging)
        assert result == staging
        assert (result / "Postmortem.exe").exists()

    def test_incomplete_macos_zip_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        zip_path = _build_zip(tmp_path, {"Postmortem.app/Contents/Info.plist": b"<plist/>"})
        staging = tmp_path / "staging"
        staging.mkdir()
        with pytest.raises(ValueError):
            updater.extract_update(zip_path, staging)

    def test_incomplete_windows_zip_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        zip_path = _build_zip(tmp_path, {"_internal/base_library.zip": b"fake"})
        staging = tmp_path / "staging"
        staging.mkdir()
        with pytest.raises(ValueError):
            updater.extract_update(zip_path, staging)

    def test_path_traversal_entries_are_rejected(self, tmp_path):
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../evil.txt", b"pwned")
        staging = tmp_path / "staging"
        staging.mkdir()
        with pytest.raises(ValueError):
            updater._safe_extract(zip_path, staging)


# -- permissions and symlinks (the real 2026-09-01 self-update failure) -----


def _zip_info(name: str, unix_mode: int) -> zipfile.ZipInfo:
    """A ZipInfo with its Unix mode bits set in external_attr's upper 16
    bits -- exactly the encoding ditto/unzip/zip all honor, including
    the S_IFLNK file-type bit for a symlink entry."""
    info = zipfile.ZipInfo(name)
    info.external_attr = unix_mode << 16
    return info


class TestSafeExtractPreservesPermissionsAndSymlinks:
    """Real, reproduced failure (2026-09-01): a self-applied update
    downloaded, validated, and swapped in cleanly, but the result was
    completely unlaunchable (macOS: "Launchd job spawn failed").
    Root-caused by diffing the broken build against a known-good one
    extracted the normal way (ditto/unzip, not this module): plain
    ZipFile.extractall() drops the executable bit entirely, and doesn't
    reconstruct symlinks at all -- each one came out as an ordinary
    file whose *content* is the literal target path text. This
    project's own build has real symlinks in it (build/postmortem.spec's
    own datas entry becomes one), and macOS's Python.framework layout is
    symlink-heavy internally, so this wasn't a cosmetic gap -- it broke
    the bundle's own structure, not just permissions.
    """

    def test_the_executable_bit_survives_extraction(self, tmp_path):
        zip_path = tmp_path / "update.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(_zip_info("Postmortem.app/Contents/MacOS/Postmortem", 0o755), b"fake binary")
        staging = tmp_path / "staging"
        staging.mkdir()
        updater._safe_extract(zip_path, staging)

        exe = staging / "Postmortem.app/Contents/MacOS/Postmortem"
        assert exe.stat().st_mode & 0o111  # any execute bit set

    def test_a_symlink_entry_becomes_a_real_symlink_not_a_text_file(self, tmp_path):
        import stat as _stat

        zip_path = tmp_path / "update.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                _zip_info("Postmortem.app/Contents/Frameworks/postmortem", _stat.S_IFLNK | 0o755),
                "../Resources/postmortem",
            )
            zf.writestr("Postmortem.app/Contents/Resources/postmortem/marker.txt", b"real content")
        staging = tmp_path / "staging"
        staging.mkdir()
        updater._safe_extract(zip_path, staging)

        link = staging / "Postmortem.app/Contents/Frameworks/postmortem"
        assert link.is_symlink()
        assert link.readlink() == Path("../Resources/postmortem")
        # and it actually resolves to the real content through the symlink
        assert (link / "marker.txt").read_text() == "real content"

    def test_directories_get_their_own_permissions_without_blocking_extraction(self, tmp_path):
        # A restrictive directory mode applied too early (before its
        # contents are written) would break extraction outright --
        # confirms the ordering (dirs -> files/symlinks -> dir modes
        # last) actually holds.
        zip_path = tmp_path / "update.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(_zip_info("Postmortem.app/Contents/MacOS/", 0o755), "")
            zf.writestr(_zip_info("Postmortem.app/Contents/MacOS/Postmortem", 0o755), b"fake binary")
        staging = tmp_path / "staging"
        staging.mkdir()
        updater._safe_extract(zip_path, staging)

        exe = staging / "Postmortem.app/Contents/MacOS/Postmortem"
        assert exe.read_bytes() == b"fake binary"
        assert exe.stat().st_mode & 0o111

    def test_windows_style_zip_with_no_unix_attrs_still_extracts_cleanly(self, tmp_path):
        # Compress-Archive (the real Windows asset's packaging tool)
        # never sets these bits at all -- every mode/symlink branch
        # above must be a safe no-op, not a crash, for that zip.
        zip_path = _build_zip(tmp_path, {"Postmortem.exe": b"fake exe", "_internal/base_library.zip": b"fake"})
        staging = tmp_path / "staging"
        staging.mkdir()
        updater._safe_extract(zip_path, staging)
        assert (staging / "Postmortem.exe").read_bytes() == b"fake exe"


# -- download_update (network mocked) ----------------------------------------


class _FakeResponse:
    def __init__(self, data: bytes, content_length=None):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(content_length)} if content_length is not None else {}

    def read(self, n):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDownloadUpdate:
    def test_streams_to_disk_and_reports_progress(self, tmp_path, monkeypatch):
        payload = b"x" * 1000
        monkeypatch.setattr(
            updater.urllib.request, "urlopen",
            lambda req, timeout=None, context=None: _FakeResponse(payload, content_length=len(payload)),
        )
        progress = []
        dest = tmp_path / "out.zip"
        updater.download_update(
            "https://github.com/x/y/releases/download/t/a.zip", dest,
            on_progress=progress.append,
        )
        assert dest.read_bytes() == payload
        assert progress[-1]["written"] == len(payload)
        assert progress[-1]["total"] == len(payload)

    def test_missing_content_length_still_works(self, tmp_path, monkeypatch):
        payload = b"y" * 500
        monkeypatch.setattr(
            updater.urllib.request, "urlopen",
            lambda req, timeout=None, context=None: _FakeResponse(payload),
        )
        dest = tmp_path / "out.zip"
        updater.download_update("https://github.com/x/y/releases/download/t/a.zip", dest)
        assert dest.read_bytes() == payload


# -- apply_update_and_relaunch (subprocess mocked) ---------------------------


class TestApplyUpdateAndRelaunch:
    def test_refuses_outside_a_frozen_build(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        with pytest.raises(RuntimeError):
            updater.apply_update_and_relaunch(tmp_path / "new")

    def test_writes_a_relaunch_script_and_spawns_it_detached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "platform", "darwin")
        fake_old_root = tmp_path / "Postmortem.app"
        fake_old_root.mkdir()
        monkeypatch.setattr(updater, "_current_install_root", lambda: fake_old_root)

        captured = {}

        def fake_popen(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return object()

        monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

        new_install = tmp_path / "extracted" / "Postmortem.app"
        new_install.mkdir(parents=True)
        updater.apply_update_and_relaunch(new_install, pid=12345)

        args = captured["args"]
        assert args[0] == "/bin/sh"
        script_path = updater.Path(args[1])
        assert script_path.exists()
        script_text = script_path.read_text()
        assert "kill -0" in script_text  # waits for the PID to exit
        assert "mv" in script_text and "open -n" in script_text
        assert str(12345) in args
        assert str(fake_old_root) in args
        assert str(new_install) in args

    def test_windows_uses_powershell_and_wait_process(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        fake_old_root = tmp_path / "Postmortem"
        fake_old_root.mkdir()
        monkeypatch.setattr(updater, "_current_install_root", lambda: fake_old_root)

        captured = {}
        monkeypatch.setattr(
            updater.subprocess, "Popen",
            lambda args, **kwargs: captured.update(args=args, kwargs=kwargs) or object(),
        )

        new_install = tmp_path / "extracted"
        new_install.mkdir()
        updater.apply_update_and_relaunch(new_install, pid=999)

        args = captured["args"]
        assert args[0] == "powershell"
        assert "-File" in args
        script_path = updater.Path(args[args.index("-File") + 1])
        assert "Wait-Process" in script_path.read_text()
        assert captured["kwargs"]["creationflags"] != 0


# -- perform_update (end-to-end with network + subprocess mocked) -----------


class TestPerformUpdate:
    def test_downloads_extracts_and_cleans_up_the_zip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")

        def build_fake_zip_bytes():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("Postmortem.app/Contents/MacOS/Postmortem", b"fake")
            return buf.getvalue()

        payload = build_fake_zip_bytes()
        monkeypatch.setattr(
            updater.urllib.request, "urlopen",
            lambda req, timeout=None, context=None: _FakeResponse(payload, content_length=len(payload)),
        )

        work_dir = tmp_path / "work"
        result = updater.perform_update(
            "https://github.com/x/y/releases/download/t/Postmortem-macos.zip", work_dir,
        )
        assert result == work_dir / "extracted" / "Postmortem.app"
        assert (result / "Contents" / "MacOS" / "Postmortem").exists()
        assert not (work_dir / "update.zip").exists()  # cleaned up after extraction
