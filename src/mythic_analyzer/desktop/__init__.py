"""Desktop app support: the pywebview Python<->JS API bridge (api.py) and
local settings persistence (config.py).

Importing this package (and everything it re-exports below) never
requires pywebview to be installed -- see api.py's module docstring for
where pywebview is (locally, lazily) imported.
"""

from .api import DesktopAPI
from .config import load_settings, save_settings

__all__ = ["DesktopAPI", "load_settings", "save_settings"]
