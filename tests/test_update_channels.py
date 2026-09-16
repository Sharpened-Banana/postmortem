"""Beta vs stable release channels (docs/RELEASE_CHANNELS.md): tag
ordering, and which releases each channel is offered."""

from __future__ import annotations

import pytest

from postmortem.desktop import updater
from postmortem.desktop.updater import build_key, check_for_update, tag_channel


class TestTagOrdering:
    def test_stable_outranks_every_beta_of_the_same_number(self):
        assert build_key("beta-desktop-49.1") < build_key("beta-desktop-49.2")
        assert build_key("beta-desktop-49.2") < build_key("alpha-desktop-49")
        assert build_key("alpha-desktop-48") < build_key("beta-desktop-49.1")
        assert build_key("alpha-desktop-49") < build_key("beta-desktop-50.1")

    @pytest.mark.parametrize("bad", ["", "dev", "addon-v0.3.4", "alpha-desktop-49.1",
                                     "beta-desktop-49", "beta-desktop-x.1"])
    def test_non_tags(self, bad):
        assert build_key(bad) is None and tag_channel(bad) is None

    def test_channels(self):
        assert tag_channel("alpha-desktop-49") == "stable"
        assert tag_channel("beta-desktop-49.3") == "beta"


def _release(tag, prerelease=False, draft=False):
    return {
        "tag_name": tag, "prerelease": prerelease, "draft": draft, "body": f"notes {tag}",
        "assets": [
            {"name": "Postmortem-macos.zip", "browser_download_url": f"https://github.com/x/{tag}/mac"},
            {"name": "Postmortem-windows.zip", "browser_download_url": f"https://github.com/x/{tag}/win"},
        ],
    }


LISTING = [
    _release("addon-v0.3.4"),
    _release("beta-desktop-49.2", prerelease=True),
    _release("beta-desktop-49.1", prerelease=True),
    _release("alpha-desktop-48"),
    _release("beta-desktop-50.1", prerelease=True, draft=True),  # a draft: never
    _release("alpha-desktop-47"),
]


class TestChannelSelection:
    @pytest.fixture(autouse=True)
    def _platform(self, monkeypatch):
        monkeypatch.setattr(updater, "_asset_name_for_platform", lambda: "Postmortem-macos.zip")
        monkeypatch.setattr(updater, "_expected_digest", lambda payload, name: None)

    def test_stable_app_on_stable_channel_sees_only_stable(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "alpha-desktop-47")
        r = check_for_update(lambda url: LISTING, channel="stable")
        assert r["tag"] == "alpha-desktop-48"

    def test_stable_app_on_beta_channel_is_offered_the_newest_beta(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "alpha-desktop-48")
        assert check_for_update(lambda url: LISTING, channel="stable") is None
        r = check_for_update(lambda url: LISTING, channel="beta")
        assert r["tag"] == "beta-desktop-49.2" and r["download_url"].endswith("/mac")

    def test_beta_app_is_offered_the_stable_it_led_to_and_never_older(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "beta-desktop-49.2")
        assert check_for_update(lambda url: LISTING, channel="beta") is None
        assert check_for_update(lambda url: LISTING, channel="stable") is None  # 48 < 49.2
        with_49 = [_release("alpha-desktop-49")] + LISTING
        assert check_for_update(lambda url: with_49, channel="beta")["tag"] == "alpha-desktop-49"
        assert check_for_update(lambda url: with_49, channel="stable")["tag"] == "alpha-desktop-49"

    def test_unknown_channel_behaves_as_stable(self, monkeypatch):
        monkeypatch.setattr(updater, "VERSION", "alpha-desktop-48")
        assert check_for_update(lambda url: LISTING, channel="nightly") is None

    def test_the_shipped_stable_updater_ignores_beta_tags(self):
        """Apps already installed (45-48) run the old regex; a beta
        pre-release in the listing must be invisible to them."""
        import subprocess, types
        src = subprocess.check_output(
            ["git", "show", "alpha-desktop-48:src/postmortem/desktop/updater.py"], text=True)
        old = types.ModuleType("updater48"); old.__package__ = "postmortem.desktop"
        exec(compile(src, "updater48.py", "exec"), old.__dict__)
        old.VERSION = "alpha-desktop-48"
        old._asset_name_for_platform = lambda: "Postmortem-macos.zip"
        assert old.check_for_update(lambda url: LISTING) is None
        assert old.check_for_update(lambda url: [_release("alpha-desktop-49")] + LISTING)["tag"] == "alpha-desktop-49"


class TestApiChannel:
    def test_check_for_update_reads_the_setting(self, monkeypatch, tmp_path):
        from postmortem.desktop import config as desktop_config
        from postmortem.desktop.api import DesktopAPI
        monkeypatch.setattr(desktop_config, "config_dir", lambda: tmp_path)
        seen = {}
        monkeypatch.setattr(updater, "check_for_update",
                            lambda fetcher=None, channel="stable": seen.setdefault("channel", channel) and None)
        api = DesktopAPI()
        assert api.check_for_update()["channel"] == "stable"
        s = desktop_config.load_settings(); s["update_channel"] = "beta"; desktop_config.save_settings(s)
        seen.clear()
        assert api.check_for_update()["channel"] == "beta" and seen["channel"] == "beta"
