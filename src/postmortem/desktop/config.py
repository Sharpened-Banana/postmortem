"""Local settings persistence for the pywebview desktop app.

A small JSON file, stored in the OS's standard per-user config
directory, holding the fields a user would otherwise have to retype on
every CLI invocation: ``wow_addon_path`` (for one-click dungeon-data
extraction, see ``extract_dungeon_data`` in api.py), ``raiderio_region``,
``avoidable_data_path``, ``default_output_dir``, ``history_db_path`` and
``wow_log_path`` (the live ``WoWCombatLog.txt`` to watch -- see
``start_watch`` in api.py).

Directory resolution follows each OS's own convention rather than
assuming ``~/.config`` works everywhere (it doesn't on Windows, and isn't
idiomatic on macOS) -- this matters once the app is packaged for both
macOS and Windows:

- Windows: ``%APPDATA%\\postmortem``
- macOS:   ``~/Library/Application Support/postmortem``
- Linux/other: ``$XDG_CONFIG_HOME/postmortem``, falling back to
  ``~/.config/postmortem``

Tolerant of a missing or corrupt settings file, matching this project's
established "don't crash on our own possibly-missing/corrupt local
state" pattern (see ``cache.py``'s ``_load_cache``): ``load_settings()``
just returns defaults rather than raising.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

# APP_DIR_NAME/config_dir moved to postmortem.appdirs so the plain
# CLI (no `desktop` extra installed) can resolve the same per-user config
# directory too -- e.g. for the upload token in upload.py. Re-exported
# here so nothing that already imports them from this module breaks.
from ..appdirs import APP_DIR_NAME, config_dir
from ..bundled import bundled_dungeon_data_path, bundled_interrupt_data_path

SETTINGS_FILENAME = "desktop_settings.json"

#: Every field a fresh install (or a corrupt/missing settings file)
#: falls back to. ``load_settings()`` always returns a dict containing at
#: least these keys.
DEFAULT_SETTINGS: dict[str, Any] = {
    "wow_addon_path": None,
    "raiderio_region": None,
    "avoidable_data_path": None,
    "default_output_dir": None,
    "history_db_path": None,
    "site_url": None,
    "wow_log_path": None,
    # When True, the app starts Watch Live automatically on launch (using
    # the saved wow_log_path + site_url), so the whole "finish a key -> it's
    # analyzed and uploaded" loop needs zero clicks per session. Off by
    # default: opting in is a deliberate choice, since it begins tailing a
    # file and uploading runs the moment the app opens.
    "watch_auto_start": False,
    # Per-dungeon default MDT routes, applied automatically to any run
    # (Watch Live or one-off analysis) that doesn't get an explicit route
    # -- so route adherence shows up on every key without pasting a
    # string each session. A list of
    #   {"dungeon_idx": int, "dungeon_name": str|None,
    #    "challenge_map_id": int|None, "route": "<MDT export string>"}
    # -- the MDT string itself carries dungeon_idx, so a pasted route
    # self-identifies; the other two are looked up from dungeon data at
    # save time so matching a run needs no data file later. See
    # resolve_default_route.
    "default_routes": [],
}


def settings_path() -> Path:
    """Full path to the settings JSON file."""
    return config_dir() / SETTINGS_FILENAME


def load_settings() -> dict[str, Any]:
    """Load persisted settings, tolerant of a missing/corrupt file.

    Returns a dict containing every key in ``DEFAULT_SETTINGS`` (filled
    from the saved file where present, defaults otherwise). A missing
    file, an unreadable file, invalid JSON, or a JSON value that isn't an
    object at the top level all just yield the defaults -- this is our
    own local state, not user-typed configuration, so the bar is "don't
    crash and don't lose the ability to keep working" (see cache.py's
    ``_load_cache``), not "raise a clear error".
    """
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return settings
    if isinstance(payload, dict):
        settings.update(payload)
    return settings


def resolve_output_dir(settings: dict[str, Any], subdir: str) -> Path:
    """The directory local reports of one kind get written into: the
    ``default_output_dir`` setting when set, otherwise a per-purpose
    subfolder under this app's own config directory. ``start_watch()``
    (api.py) already had this exact "works with zero setup" fallback for
    Watch Live's recorded-run output; this generalizes it (``subdir``
    picks the purpose-specific subfolder, e.g. ``"watch-runs"`` vs.
    ``"analyzed-runs"``) so a one-off ``analyze()`` run gets the same
    "always saved somewhere, no configuration required" default instead
    of the report only ever existing in memory for as long as the report
    screen stays open.
    """
    configured = settings.get("default_output_dir")
    if configured:
        return Path(configured)
    return config_dir() / subdir


def resolve_watch_log_path(folder: str | Path) -> Path:
    """The combat log to watch inside a WoW ``Logs`` folder: the most
    recently modified ``WoWCombatLog*.txt`` file, or ``folder /
    "WoWCombatLog.txt"`` when none exist yet.

    WoW does NOT reliably reuse one stable ``WoWCombatLog.txt`` filename
    for "the log this session is actively writing to" -- some installs
    always append a session timestamp instead (confirmed real 2026-09-01:
    a real user's Logs folder held only ``WoWCombatLog-<timestamp>.txt``
    archives, seven sessions running, never once a plain-named file), so
    hardcoding that plain name (as both the Watch tab's and Settings'
    "choose your WoW Logs folder" pickers used to) silently pointed Watch
    Live at a file that would never exist on those installs. The file
    actually being written to right now -- whatever it's named -- is
    always the one with the newest mtime, since only an open, growing log
    keeps getting touched; an archived prior session's file is frozen the
    moment that session ended. Falling back to the plain name when no log
    exists yet at all preserves ``Recorder.watch()``'s existing "wait for
    the first key of a fresh session" behavior.
    """
    folder = Path(folder)
    candidates = sorted(
        folder.glob("WoWCombatLog*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    return folder / "WoWCombatLog.txt"


def resolve_history_db_path(settings: dict[str, Any]) -> Path:
    """The local run-history database path: the ``history_db_path``
    setting when set, otherwise a default file under this app's own
    config directory. Same zero-config philosophy as
    ``resolve_output_dir`` -- every analyzed/watched run gets ingested
    here so the History screen has something to show without the user
    ever having to configure a database path themselves.
    """
    configured = settings.get("history_db_path")
    if configured:
        return Path(configured)
    return config_dir() / "history.db"


AVOIDABLE_FILENAME = "avoidable_spells.json"


def resolve_avoidable_data_path(settings: dict[str, Any]) -> Optional[Path]:
    """The avoidable-damage spell file to load: the ``avoidable_data_path``
    setting when set, otherwise ``<config dir>/avoidable_spells.json`` if
    one has been dropped there, otherwise None (no tagging -- same as
    before this existed).

    Avoidable damage is only ever as good as the spell list behind it,
    and this project deliberately doesn't ship one (see
    analysis/avoidable.py). Until now, though, the only way to use a list
    at all was to point Settings at it -- so a real user with the field
    left blank saw "0 avoidable" on every run and reasonably read that as
    the feature being missing (2026-09-02). Picking up a file from the
    app's own data folder makes "drop the list in, it just works" the
    zero-configuration path, matching resolve_history_db_path.
    """
    configured = settings.get("avoidable_data_path")
    if configured:
        return Path(configured)
    default = config_dir() / AVOIDABLE_FILENAME
    return default if default.is_file() else None


LEARNED_INTERRUPTS_FILENAME = "learned_interrupts.json"


def resolve_learned_interrupts_path(settings: dict[str, Any]) -> Path:
    """Where this account's own learned interruptibility evidence lives
    (see analysis/interrupt_learning.py). Always a real path -- unlike
    the other resolvers this one never returns None, because the file is
    written as well as read: it accumulates a little more every time a
    run is analyzed, and a missing file just means nothing learned yet.
    """
    configured = settings.get("learned_interrupts_path")
    if configured:
        return Path(configured)
    return config_dir() / LEARNED_INTERRUPTS_FILENAME


STEALABLE_FILENAME = "stealable_spells.json"


def resolve_stealable_data_path(settings: dict[str, Any]) -> Optional[Path]:
    """The stealable-buff spell file to load: the ``stealable_data_path``
    setting when set, otherwise ``<config dir>/stealable_spells.json`` if
    one has been dropped there, otherwise None (no tagging).

    Same posture as ``resolve_avoidable_data_path`` -- deliberately no
    packaged fallback (contrast ``resolve_interrupt_data_path``): there's
    no community-maintained database this project can convert into a
    bundled copy the way ``mplus-interrupts`` covers interrupts (see
    ``analysis/stealable.py``'s module docstring for why), so this is a
    real user-supplied file or nothing.
    """
    configured = settings.get("stealable_data_path")
    if configured:
        return Path(configured)
    default = config_dir() / STEALABLE_FILENAME
    return default if default.is_file() else None


def resolve_dungeon_data_path(settings: dict[str, Any]) -> Optional[Path]:
    """The MDT dungeon/enemy data to analyze with: an explicit
    ``dungeon_data_path`` setting when set, else a ``dungeon_data.json``
    dropped into the app's own data folder, else the copy shipped inside
    the package (see postmortem/bundled.py), else None.

    Until 2026-09-02 the desktop had no fallback at all -- a Watch Live
    run analyzed with no dungeon data unless the user had run
    extract-data themselves, so forces progress and route adherence were
    simply absent by default even though the public site had been
    bundling this exact file all along.
    """
    configured = settings.get("dungeon_data_path")
    if configured:
        return Path(configured)
    local = config_dir() / "dungeon_data.json"
    if local.is_file():
        return local
    bundled = bundled_dungeon_data_path()
    return bundled if bundled.is_file() else None


def resolve_interrupt_data_path(settings: dict[str, Any]) -> Optional[Path]:
    """The spell-interruptibility data to analyze with: an explicit
    ``interrupt_data_path`` setting when set, else an ``interrupt_data.json``
    dropped into the app's own data folder, else the copy shipped inside
    the package (see postmortem/bundled.py), else None. Same resolution
    order as ``resolve_dungeon_data_path`` -- this is package-maintained
    data too (see bundled.py's own comment on why), not a user-supplied
    file the way avoidable-damage tagging is.

    Until 2026-09-04 this had no resolution at all, in the CLI or the
    desktop app -- ``--interrupt-data`` had to be passed explicitly on
    every CLI invocation, and the desktop app (including Watch Live)
    never loaded or passed this data at all, so kick-efficiency reporting
    silently ran on the plain "kicked at least once" heuristic everywhere
    except a bare CLI call with the flag set. Fixed alongside replacing
    the addon's now-permanently-dead live capture with a bundled,
    community-sourced database (see interruptibility.py's module
    docstring) that's actually worth resolving automatically.
    """
    configured = settings.get("interrupt_data_path")
    if configured:
        return Path(configured)
    local = config_dir() / "interrupt_data.json"
    if local.is_file():
        return local
    bundled = bundled_interrupt_data_path()
    return bundled if bundled.is_file() else None


def resolve_default_route(
    settings: dict[str, Any],
    *,
    challenge_map_id: Optional[int],
    zone_name: Optional[str],
    dungeon_idx: Optional[int] = None,
) -> Optional[str]:
    """The saved default MDT route string for a run, or None.

    Matched in order of how certain the signal is: the run's
    challenge-map id (what CHALLENGE_MODE_START logs -- exact), then MDT
    dungeon index (when a caller already resolved one via dungeon data),
    then the zone name (case-insensitive -- a last resort for entries
    saved before any dungeon data was available to fill the ids in).
    """
    routes = settings.get("default_routes") or []
    if not isinstance(routes, list):
        return None

    def _pick(pred) -> Optional[str]:
        for entry in routes:
            if isinstance(entry, dict) and entry.get("route") and pred(entry):
                return str(entry["route"])
        return None

    if challenge_map_id is not None:
        hit = _pick(lambda e: e.get("challenge_map_id") == challenge_map_id)
        if hit:
            return hit
    if dungeon_idx is not None:
        hit = _pick(lambda e: e.get("dungeon_idx") == dungeon_idx)
        if hit:
            return hit
    if zone_name:
        want = zone_name.strip().lower()
        hit = _pick(lambda e: (e.get("dungeon_name") or "").strip().lower() == want)
        if hit:
            return hit
    return None


def save_settings(settings: dict[str, Any]) -> None:
    """Persist ``settings`` as JSON, creating the config directory if
    needed. Merged onto ``DEFAULT_SETTINGS`` first so a partial dict
    (e.g. just one changed field) still leaves a complete, self-
    consistent file behind -- callers don't need to round-trip
    ``load_settings()`` themselves before saving.
    """
    merged = dict(DEFAULT_SETTINGS)
    merged.update(settings or {})
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=1)
