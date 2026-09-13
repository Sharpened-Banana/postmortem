"""OS-appropriate per-user config directory resolution, shared by the
CLI and the (optional) desktop app.

Directory resolution follows each OS's own convention rather than
assuming ``~/.config`` works everywhere (it doesn't on Windows, and isn't
idiomatic on macOS):

- Windows: ``%APPDATA%\\postmortem`` (cache: ``%LOCALAPPDATA%\\postmortem\\cache``)
- macOS:   ``~/Library/Application Support/postmortem``
- Linux/other: ``$XDG_CONFIG_HOME/postmortem``, falling back to
  ``~/.config/postmortem``

Stdlib-only (``os``, ``sys``, ``pathlib``) and dependency-free so the
plain CLI install (no ``desktop`` extra) can use it too -- e.g. for
storing a small upload-token file (see ``upload.py``). The desktop app's
``desktop/config.py`` re-exports ``APP_DIR_NAME``/``config_dir`` from
here rather than defining its own copy.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "postmortem"


def config_dir() -> Path:
    """The OS-appropriate per-user config directory for this app.

    A function (not a module-level constant) so tests can monkeypatch it
    directly rather than touching the real user's home directory.
    """
    if sys.platform == "win32":
        base = os.getenv("APPDATA")
        if base:
            return Path(base) / APP_DIR_NAME
        # No APPDATA (unusual, e.g. some CI/Wine setups) -- fall back
        # rather than crash; still namespaced under our own subfolder.
        return Path.home() / ".config" / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    # Linux and everything else: XDG convention.
    xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / APP_DIR_NAME
    return Path.home() / ".config" / APP_DIR_NAME


def cache_dir() -> Path:
    """The OS-appropriate per-user *cache* directory for this app.

    The same convention as :func:`config_dir`, for data that can be
    thrown away: ``%LOCALAPPDATA%\\postmortem\\cache`` on Windows,
    ``~/Library/Caches/postmortem`` on macOS, ``$XDG_CACHE_HOME`` (or
    ``~/.cache``) elsewhere. ``cache.py`` used a dot-directory under the
    home folder on every platform, which is not where a Windows user
    expects it.
    """
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
        if base:
            return Path(base) / APP_DIR_NAME / "cache"
        return Path.home() / ".cache" / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_DIR_NAME
    xdg = os.getenv("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / APP_DIR_NAME
    return Path.home() / ".cache" / APP_DIR_NAME
