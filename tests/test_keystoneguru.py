"""postmortem.keystoneguru -- profile route listing + MDT export, with
urllib monkeypatched so nothing here touches the network."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from postmortem import keystoneguru as kg


class TestParseProfileId:
    @pytest.mark.parametrize("text", [
        "https://keystone.guru/profile/64246",
        "https://keystone.guru/profile/64246/",
        "keystone.guru/index.php/profile/64246",
        "/profile/64246",
        "  64246  ",
    ])
    def test_accepts_urls_and_bare_ids(self, text):
        assert kg.parse_profile_id(text) == 64246

    @pytest.mark.parametrize("text", ["", "   ", "https://keystone.guru/routes/retail", "not a url"])
    def test_rejects_everything_else(self, text):
        with pytest.raises(kg.KeystoneGuruError):
            kg.parse_profile_id(text)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(routes_by_url):
    """urlopen stand-in: answers from a {url-substring: payload} table."""
    calls = []

    def urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        calls.append(url)
        # the site's own XHR header must be sent, or it answers HTML
        assert request.get_header("X-requested-with") == "XMLHttpRequest"
        for needle, payload in routes_by_url.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return _FakeResponse(json.dumps(payload).encode("utf-8"))
        raise AssertionError(f"unexpected url {url}")

    urlopen.calls = calls
    return urlopen


def _row(key, title, slug, published=True, mdt=True):
    return {
        "public_key": key, "title": title, "published": published,
        "dungeon": {"slug": slug, "name": f"dungeons.x.{slug}.name", "mdt_supported": mdt},
    }


class TestListPublicRoutes:
    def test_lists_and_pages_until_the_total_is_reached(self, monkeypatch):
        page1 = {"data": [_row("aaa", "Route A", "altar-of-fangs"),
                          _row("bbb", "Route B", "murder-row")], "recordsTotal": 3, "recordsFiltered": 3}
        page2 = {"data": [_row("ccc", "Route C", "the-blinding-vale", mdt=False)],
                 "recordsTotal": 3, "recordsFiltered": 3}

        def urlopen(request, timeout=None):
            url = request.full_url
            assert "user_id=64246" in url and "columns%5B0%5D%5Bdata%5D=title" in url
            return _FakeResponse(json.dumps(page2 if "start=2" in url else page1).encode())

        monkeypatch.setattr(kg, "_PAGE_SIZE", 2)
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        routes = kg.list_public_routes(64246)
        assert [r["public_key"] for r in routes] == ["aaa", "bbb", "ccc"]
        assert routes[0]["dungeon_name"] == "Altar Of Fangs"
        assert routes[2]["mdt_supported"] is False

    def test_unexpected_shape_is_a_clean_error(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen({"/ajax/routes": {"message": "nope"}}))
        with pytest.raises(kg.KeystoneGuruError):
            kg.list_public_routes(1)


class TestFetchMdtString:
    def test_returns_the_export_string(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen",
                            _fake_urlopen({"/ajax/0RhVlYt/mdtExport": {"mdt_string": " !~MDT2~abc ", "warnings": []}}))
        assert kg.fetch_mdt_string("0RhVlYt") == "!~MDT2~abc"

    def test_key_is_sanitized_and_missing_string_is_an_error(self, monkeypatch):
        # "0RhVlYt/../x" is stripped to the alphanumerics "0RhVlYtx" -- no
        # path traversal can reach the request URL
        urlopen = _fake_urlopen({"/ajax/0RhVlYtx/mdtExport": {"warnings": []}})
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        with pytest.raises(kg.KeystoneGuruError):
            kg.fetch_mdt_string("0RhVlYt/../x")
        assert urlopen.calls == ["https://keystone.guru/ajax/0RhVlYtx/mdtExport?useCache=1"]

    def test_http_and_network_errors_are_mapped(self, monkeypatch):
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen({"/mdtExport": err}))
        with pytest.raises(kg.KeystoneGuruError, match="HTTP 404"):
            kg.fetch_mdt_string("abc")
        monkeypatch.setattr(urllib.request, "urlopen",
                            _fake_urlopen({"/mdtExport": urllib.error.URLError("offline")}))
        with pytest.raises(kg.KeystoneGuruError, match="could not reach"):
            kg.fetch_mdt_string("abc")
