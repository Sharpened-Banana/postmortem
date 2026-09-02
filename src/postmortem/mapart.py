"""Dungeon map art for the route map, from the user's own MDT install.

MythicDungeonTools ships each dungeon floor's map as plain PNG tiles
inside the addon folder (e.g. ``MythicDungeonTools/Midnight/Textures/
AltarOfFangs/1_1.png .. 1_150.png``: 128x128 each, ``<floor>_<n>``,
row-major, 15 per row -> 1920x1280). Our extracted dungeon data already
records that folder per floor (``DungeonData.map_textures``), and MDT's
enemy/POI coordinates are in the same 840x555 planning canvas the tiles
are laid out for -- so a stitched floor image scaled onto the canvas
lines up with every planned enemy dot with no calibration at all
(confirmed 2026-09-02 by overlaying all 128 Altar of Fangs clones on the
stitched map: every one sits in its room).

Everything here is best-effort and optional:

- Pillow (a ``desktop`` extra, not a core dependency -- the core stays
  stdlib-only) is imported lazily; without it, or without an MDT
  install, or with a floor whose tiles are missing, the report simply
  has no background, exactly as before. Nothing raises.
- The art is Blizzard's, redistributed by MDT for in-game use. Reading
  it from the user's *own* installed addon into their *own* local report
  is the same "from your own addon" boundary everything else in this
  project respects (``extract-data``, the interrupt database, the
  results writeback). It is deliberately NOT sent along with a report
  uploaded to the public site -- see ``strip_backgrounds`` and its
  callers -- so the site never republishes it.
"""

from __future__ import annotations

import base64
import copy
import io
from pathlib import Path
from typing import Any, Optional

TILE_PX = 128
TILES_PER_ROW = 15
#: MDT's planning canvas -- what every enemy/POI coordinate is in.
CANVAS_W, CANVAS_H = 840, 555
#: Embedded size: ~180KB as JPEG at this width, plenty for a report.
EMBED_WIDTH = 1200
EMBED_QUALITY = 82

MDT_ADDON_DIRNAME = "MythicDungeonTools"


def mdt_dir_from_log_path(log_path: str | Path) -> Optional[Path]:
    """The installed MythicDungeonTools folder, derived from the combat
    log path the same way ``addon_results.addon_dir_from_log_path``
    finds our own addon: ``<flavor>/Logs/<log>`` -> ``<flavor>/Interface/
    AddOns/MythicDungeonTools``. None when it isn't there."""
    logs_dir = Path(log_path).parent
    if logs_dir.name != "Logs":
        return None
    mdt = logs_dir.parent / "Interface" / "AddOns" / MDT_ADDON_DIRNAME
    return mdt if mdt.is_dir() else None


def texture_dir(mdt_dir: str | Path, map_texture: str) -> Optional[Path]:
    """Resolve one of ``DungeonData.map_textures``' values -- an in-game
    path like ``Interface\\AddOns\\MythicDungeonTools\\Midnight\\Textures\\
    AltarOfFangs`` -- to the real folder under the installed addon."""
    if not map_texture:
        return None
    parts = map_texture.replace("\\", "/").split("/")
    try:
        i = parts.index(MDT_ADDON_DIRNAME)
    except ValueError:
        return None
    folder = Path(mdt_dir).joinpath(*parts[i + 1:])
    return folder if folder.is_dir() else None


def stitch_floor(folder: str | Path, sublevel: int, *, tiles_per_row: int = TILES_PER_ROW,
                 tile_px: int = TILE_PX):
    """Assemble ``<sublevel>_<n>.png`` tiles into one PIL image, or None
    if the tiles aren't there. Requires Pillow (returns None without it)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    folder = Path(folder)
    tiles = {}
    for p in folder.glob(f"{sublevel}_*.png"):
        try:
            tiles[int(p.stem.split("_", 1)[1])] = p
        except ValueError:
            continue
    if not tiles:
        return None
    n = max(tiles)
    rows = -(-n // tiles_per_row)
    img = Image.new("RGB", (tiles_per_row * tile_px, rows * tile_px), (0, 0, 0))
    for i in range(1, n + 1):
        p = tiles.get(i)
        if p is None:
            continue
        with Image.open(p) as t:
            r, c = divmod(i - 1, tiles_per_row)
            # MDT's tiles are palette PNGs with a transparency byte; going
            # through RGBA first is what Pillow asks for (converting P->RGB
            # directly warns on every tile).
            img.paste(t.convert("RGBA").convert("RGB"), (c * tile_px, r * tile_px))
    return img


def image_data_uri(img, *, width: int = EMBED_WIDTH, quality: int = EMBED_QUALITY) -> str:
    """Downscale to ``width`` and encode as a JPEG data URI."""
    from PIL import Image  # caller already proved Pillow is importable

    if img.width > width:
        img = img.resize((width, round(width * img.height / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def attach_map_backgrounds(report: dict[str, Any], mdt_dir: Optional[str | Path], store) -> int:
    """Add ``report["map"]["backgrounds"] = {"<sublevel>": {"data_uri",
    "width", "height"}}`` for every floor whose MDT tiles can be found.
    Returns how many floors got a background. Never raises; 0 means the
    report is left exactly as it was."""
    try:
        if not mdt_dir or store is None:
            return 0
        map_block = report.get("map")
        if not isinstance(map_block, dict):
            return 0
        data = store.by_challenge_map_id(report.get("run", {}).get("challenge_map_id"))
        if data is None:
            return 0
        added = 0
        backgrounds: dict[str, dict[str, Any]] = {}
        for sublevel, tex in (data.map_textures or {}).items():
            folder = texture_dir(mdt_dir, tex)
            if folder is None:
                continue
            img = stitch_floor(folder, int(sublevel))
            if img is None:
                continue
            backgrounds[str(sublevel)] = {
                "data_uri": image_data_uri(img),
                "width": CANVAS_W, "height": CANVAS_H,
            }
            added += 1
        if backgrounds:
            map_block["backgrounds"] = backgrounds
        return added
    except Exception:
        return 0


def strip_backgrounds(report: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``report`` without any embedded map art -- what gets
    uploaded to the public site. The art stays in the local report."""
    if not isinstance(report.get("map"), dict) or "backgrounds" not in report["map"]:
        return report
    out = copy.copy(report)
    out["map"] = {k: v for k, v in report["map"].items() if k != "backgrounds"}
    return out
