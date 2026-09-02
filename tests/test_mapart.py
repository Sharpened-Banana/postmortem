"""postmortem.mapart -- dungeon map art from the user's MDT install,
embedded into the local report and stripped before upload."""

from __future__ import annotations

import io
import json
import urllib.request

import pytest

from postmortem import mapart
from postmortem.report.html import render_html

PIL = pytest.importorskip("PIL", reason="pillow is a desktop extra")
from PIL import Image  # noqa: E402


def _fake_mdt(tmp_path, sublevel=1, tiles=4, tile_px=4, colors=None):
    """A MythicDungeonTools-shaped install with one floor of tiny tiles."""
    mdt = tmp_path / "World of Warcraft" / "_retail_" / "Interface" / "AddOns" / "MythicDungeonTools"
    folder = mdt / "Midnight" / "Textures" / "AltarOfFangs"
    folder.mkdir(parents=True)
    for i in range(1, tiles + 1):
        c = (colors or {}).get(i, (i * 40 % 256, 0, 0))
        Image.new("RGB", (tile_px, tile_px), c).save(folder / f"{sublevel}_{i}.png")
    (tmp_path / "World of Warcraft" / "_retail_" / "Logs").mkdir(parents=True)
    return mdt, folder


class TestLocate:
    def test_mdt_dir_from_log_path(self, tmp_path):
        mdt, _ = _fake_mdt(tmp_path)
        log = tmp_path / "World of Warcraft" / "_retail_" / "Logs" / "WoWCombatLog-x.txt"
        assert mapart.mdt_dir_from_log_path(log) == mdt
        assert mapart.mdt_dir_from_log_path(tmp_path / "elsewhere" / "log.txt") is None

    def test_texture_dir_resolves_the_in_game_path(self, tmp_path):
        mdt, folder = _fake_mdt(tmp_path)
        tex = r"Interface\AddOns\MythicDungeonTools\Midnight\Textures\AltarOfFangs"
        assert mapart.texture_dir(mdt, tex) == folder
        assert mapart.texture_dir(mdt, r"Interface\AddOns\MythicDungeonTools\Nope\X") is None
        assert mapart.texture_dir(mdt, "") is None


class TestStitch:
    def test_tiles_are_laid_out_row_major(self, tmp_path):
        _, folder = _fake_mdt(tmp_path, tiles=4, tile_px=4, colors={
            1: (255, 0, 0), 2: (0, 255, 0), 3: (0, 0, 255), 4: (255, 255, 0)})
        img = mapart.stitch_floor(folder, 1, tiles_per_row=2, tile_px=4)
        assert img.size == (8, 8)
        assert img.getpixel((0, 0)) == (255, 0, 0)     # tile 1: top-left
        assert img.getpixel((7, 0)) == (0, 255, 0)     # tile 2: top-right
        assert img.getpixel((0, 7)) == (0, 0, 255)     # tile 3: second row
        assert img.getpixel((7, 7)) == (255, 255, 0)   # tile 4

    def test_missing_tiles_mean_no_image(self, tmp_path):
        _, folder = _fake_mdt(tmp_path)
        assert mapart.stitch_floor(folder, 2) is None   # no floor-2 tiles

    def test_data_uri_is_a_jpeg(self, tmp_path):
        _, folder = _fake_mdt(tmp_path)
        uri = mapart.image_data_uri(mapart.stitch_floor(folder, 1, tiles_per_row=2, tile_px=4))
        assert uri.startswith("data:image/jpeg;base64,")


class _Store:
    def __init__(self, data):
        self._data = data

    def by_challenge_map_id(self, map_id):
        return self._data if map_id == 588 else None


class _Data:
    map_textures = {"1": r"Interface\AddOns\MythicDungeonTools\Midnight\Textures\AltarOfFangs"}


class TestAttachAndStrip:
    def _report(self):
        return {"run": {"challenge_map_id": 588}, "map": {"canvas": {"width": 840, "height": 555},
                "bounds": {"min_x": 0, "max_x": 840, "min_y": -555, "max_y": 0}, "enemies": [
                    {"name": "Mob", "x": 100, "y": -200, "is_boss": False, "plan_pull": 1,
                     "deviated": False, "sublevel": 1}], "pois": []}}

    def test_attaches_a_background_per_floor(self, tmp_path):
        mdt, _ = _fake_mdt(tmp_path)
        report = self._report()
        assert mapart.attach_map_backgrounds(report, mdt, _Store(_Data())) == 1
        bg = report["map"]["backgrounds"]["1"]
        assert bg["data_uri"].startswith("data:image/jpeg;base64,")
        assert (bg["width"], bg["height"]) == (840, 555)

    def test_no_ops_cleanly(self, tmp_path):
        mdt, _ = _fake_mdt(tmp_path)
        assert mapart.attach_map_backgrounds(self._report(), None, _Store(_Data())) == 0
        assert mapart.attach_map_backgrounds(self._report(), mdt, None) == 0
        other = self._report(); other["run"]["challenge_map_id"] = 1
        assert mapart.attach_map_backgrounds(other, mdt, _Store(_Data())) == 0
        assert mapart.attach_map_backgrounds({"run": {}}, mdt, _Store(_Data())) == 0

    def test_strip_returns_a_copy_without_art_and_leaves_the_original(self, tmp_path):
        mdt, _ = _fake_mdt(tmp_path)
        report = self._report()
        mapart.attach_map_backgrounds(report, mdt, _Store(_Data()))
        stripped = mapart.strip_backgrounds(report)
        assert "backgrounds" not in stripped["map"]
        assert "backgrounds" in report["map"]              # original untouched
        assert stripped["map"]["enemies"] == report["map"]["enemies"]
        plain = self._report()
        assert mapart.strip_backgrounds(plain) is plain    # nothing to strip


class TestRendering:
    def test_background_image_and_flipped_frame(self, tmp_path):
        mdt, _ = _fake_mdt(tmp_path)
        report = TestAttachAndStrip()._report()
        report.update({"dungeon": {"name": "Altar of Fangs"}, "players": [], "pulls": [],
                       "deaths": [], "forces": {"timeline": []}})
        mapart.attach_map_backgrounds(report, mdt, _Store(_Data()))
        html = render_html(report)
        # the map JS is embedded verbatim; assert on its source
        assert 'preserveAspectRatio="none"' in html
        assert 'transform="scale(1,-1)"' in html
        assert "map art from your MDT install" in html
        assert "(-b.max_y).toFixed(1)" in html   # viewBox expressed in the flipped frame

    def test_upload_never_ships_the_art(self, tmp_path, monkeypatch):
        from postmortem import upload
        mdt, _ = _fake_mdt(tmp_path)
        report = TestAttachAndStrip()._report()
        mapart.attach_map_backgrounds(report, mdt, _Store(_Data()))
        sent = {}

        class _Resp(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def getcode(self): return 200

        def fake_urlopen(request, timeout=None):
            sent["body"] = json.loads(request.data.decode("utf-8"))
            return _Resp(b'{"ok": true, "run_id": 1, "url": "/runs/1"}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        upload.upload_report(report, "https://example.test")
        assert "backgrounds" not in sent["body"]["map"]
        assert "backgrounds" in report["map"]   # local copy keeps it
