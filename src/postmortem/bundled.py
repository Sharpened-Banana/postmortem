"""Locations of data files shipped inside the ``postmortem`` package.

``data/dungeon_data.json`` is the MDT dungeon/enemy extract (see
``postmortem extract-data``) that both the public site and the desktop
app fall back to when nothing else is configured -- one copy, one place,
so the two can't drift apart. Until 2026-09-02 only the site bundled
it (as its own private file), which meant a desktop Watch Live run had
*no* dungeon data unless the user had extracted their own: no forces
progress, no route adherence, and nothing to match a saved default
route against, since an MDT route knows its dungeon by MDT index while
the combat log names it by challenge-map id -- the bridge between those
lives in exactly this file.

``data/interrupt_data.json`` is the community-sourced spell
interruptibility database (see ``scripts/build_interrupt_data.py``) that
replaces the addon's own live capture, which is permanently dead: Patch
12.0.0 made ``UnitCastingInfo``/``UnitChannelInfo``'s ``notInterruptible``
return a "secret" value addon code cannot read into a real boolean (see
``addon/Postmortem/InterruptDatabase.lua``'s own comment on this --
confirmed 2026-09-04, deliberate and permanent per Blizzard's own
"Secret Values" system, not a bug to wait out). Bundled the same way as
dungeon_data.json -- refreshed manually each season, not fetched live --
so this, too, is zero-config for every consumer (CLI, desktop app,
Watch Live, the public site upload path) instead of requiring the user
to go find and configure a file themselves.

Kept as a plain path helper (not importlib.resources) so it also
resolves inside a PyInstaller bundle, where build/postmortem.spec copies
``data/`` to the same ``postmortem/data`` relative location this
module's own ``__file__`` sits beside.
"""

from __future__ import annotations

from pathlib import Path

DUNGEON_DATA_FILENAME = "dungeon_data.json"
INTERRUPT_DATA_FILENAME = "interrupt_data.json"


def bundled_dungeon_data_path() -> Path:
    """Path to the packaged MDT dungeon/enemy data (may not exist in a
    source checkout that never had it copied in -- callers treat a
    missing file as "no data", never as an error)."""
    return Path(__file__).resolve().parent / "data" / DUNGEON_DATA_FILENAME


def bundled_interrupt_data_path() -> Path:
    """Path to the packaged spell-interruptibility database (may not
    exist in a source checkout that never had it copied in -- callers
    treat a missing file as "no data", never as an error)."""
    return Path(__file__).resolve().parent / "data" / INTERRUPT_DATA_FILENAME
