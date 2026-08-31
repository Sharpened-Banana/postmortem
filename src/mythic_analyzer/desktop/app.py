"""Window-creation entry point for the pywebview desktop app.

This module's only job is wiring the already-built pieces together --
``DesktopAPI`` (api.py) as the JS bridge, and the static shell
(shell/index.html) as the UI -- into a real ``pywebview`` window. No
business logic lives here; every actual operation (analyze, list_runs,
settings, ...) is a method on ``DesktopAPI`` already.

pywebview is imported at module scope here (unlike api.py, which keeps
it a lazy/local import) because this module's entire purpose requires
it -- there's no reduced-dependency code path to protect.

Window sizing: 1280x860 default, (900, 600) minimum. The shell's own
CSS (see shell/style.css) lays its Home/History screens out with
``.two-col``/``.card-grid`` grids that assume a reasonably wide
viewport but sets no other hard minimum, so 900x600 is comfortably
above where those grids would start looking cramped.

``background_color="#14161b"`` matches the shell's own ``--bg`` CSS
custom property (verified in shell/style.css) so the window shows its
own dark background instead of a white flash while index.html's CSS is
still loading/painting.
"""

from __future__ import annotations

from pathlib import Path

import webview

from .api import DesktopAPI

APP_TITLE = "Mythic Analyzer"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 860
MIN_SIZE = (900, 600)
BACKGROUND_COLOR = "#14161b"  # shell/style.css's --bg token

#: Absolute path to the shell's index.html. ``Path(__file__).resolve()``
#: is PyInstaller-safe: once frozen, this module lives inside the bundle
#: at the same relative location the build/mythic-analyzer.spec's
#: ``datas`` entry copies shell/ next to, so this resolves correctly
#: both in normal (non-frozen) execution and inside a PyInstaller
#: bundle. Do not swap this for sys._MEIPASS handling -- that's only
#: needed for resources pywebview itself looks up (already handled by
#: pywebview's own get_app_root()/abspath() internals), not for our own
#: shell/ assets.
SHELL_INDEX = Path(__file__).resolve().parent / "shell" / "index.html"


def main() -> None:
    """Create the desktop window and block until it's closed.

    Usable both as this file's ``if __name__ == "__main__":`` entry and
    as the ``mythic-analyzer-desktop`` console-script target (see
    pyproject.toml's ``[project.scripts]``).

    Passing a plain absolute filesystem path (not a ``file://`` URI) as
    ``url`` is intentional: pywebview's own ``is_local_url()`` check
    detects this isn't ``http(s)://``/``file://`` and automatically
    starts its bundled Bottle-based HTTP server to serve index.html (and
    style.css/app.js alongside it) -- no ``http_server=True`` or manual
    URI conversion needed.
    """
    webview.create_window(
        APP_TITLE,
        url=str(SHELL_INDEX),
        js_api=DesktopAPI(),
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        min_size=MIN_SIZE,
        background_color=BACKGROUND_COLOR,
    )
    webview.start()


if __name__ == "__main__":
    main()
