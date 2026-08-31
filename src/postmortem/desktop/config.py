"""Local settings persistence for the pywebview desktop app.

A small JSON file, stored in the OS's standard per-user config
directory, holding the fields a user would otherwise have to retype on
every CLI invocation: ``wow_addon_path`` (for one-click dungeon-data
extraction, see ``extract_dungeon_data`` in api.py), ``raiderio_region``,
``avoidable_data_path``, ``default_output_dir`` and ``history_db_path``.

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
from typing import Any

# APP_DIR_NAME/config_dir moved to postmortem.appdirs so the plain
# CLI (no `desktop` extra installed) can resolve the same per-user config
# directory too -- e.g. for the upload token in upload.py. Re-exported
# here so nothing that already imports them from this module breaks.
from ..appdirs import APP_DIR_NAME, config_dir

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
