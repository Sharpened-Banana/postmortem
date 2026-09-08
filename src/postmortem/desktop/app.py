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

import sys
from pathlib import Path
from typing import Optional

import webview

from .api import DesktopAPI

APP_TITLE = "Postmortem"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 860
MIN_SIZE = (900, 600)
BACKGROUND_COLOR = "#14161b"  # shell/style.css's --bg token

#: Absolute path to the shell's index.html. ``Path(__file__).resolve()``
#: is PyInstaller-safe: once frozen, this module lives inside the bundle
#: at the same relative location the build/postmortem.spec's
#: ``datas`` entry copies shell/ next to, so this resolves correctly
#: both in normal (non-frozen) execution and inside a PyInstaller
#: bundle. Do not swap this for sys._MEIPASS handling -- that's only
#: needed for resources pywebview itself looks up (already handled by
#: pywebview's own get_app_root()/abspath() internals), not for our own
#: shell/ assets.
SHELL_INDEX = Path(__file__).resolve().parent / "shell" / "index.html"


# pywebview hard-requires pythonnet on Windows (it hosts its window via
# .NET WinForms regardless of render engine -- see build/postmortem.spec's
# own note). Which .NET that binds to matters:
#
# - "coreclr" (modern .NET 6+, the "Desktop Runtime") is what works from
#   the frozen build; the installer makes sure it's present.
# - "netfx" (the .NET Framework built into Windows) failed from a frozen
#   build on a real machine with "Failed to resolve
#   Python.Runtime.Loader.Initialize" (2026-09-01), but is a free second
#   try on a machine with no modern .NET at all rather than a dead end.
#
# Without either, pythonnet raises "Failed to create a .NET runtime
# (coreclr)... Can not determine dotnet root" -- which a user saw on a
# fresh Windows install (2026-09-08). That's the case this turns into a
# plain dialog with the download link instead of a traceback.
DOTNET_DOWNLOAD_URL = "https://dotnet.microsoft.com/en-us/download/dotnet/8.0"
DOTNET_MESSAGE = (
    "Postmortem needs the Microsoft .NET Desktop Runtime (version 8, x64) "
    "to show its window, and it isn't installed on this PC.\n\n"
    "Open the download page now? Install the \"Desktop Runtime\" (not the "
    "SDK), then start Postmortem again."
)


# CoreCLR started with no runtime config gets the bare "console" flavour
# of .NET (Microsoft.NETCore.App), which has no System.Windows.Forms in
# it -- pywebview then dies with "Could not load file or assembly
# 'System.Windows.Forms'" (seen 2026-09-08, right after the Desktop
# Runtime was installed). This config asks for the Windows Desktop
# flavour instead; rollForward lets any installed 6+ satisfy it.
CORECLR_RUNTIME_CONFIG = {
    "runtimeOptions": {
        "tfm": "net6.0",
        "framework": {"name": "Microsoft.WindowsDesktop.App", "version": "6.0.0"},
        "rollForward": "LatestMajor",
    },
}


def coreclr_runtime_config_path() -> str:
    """Path of a runtimeconfig.json asking CoreCLR for the Windows Desktop
    runtime, written fresh each start next to the app's own settings."""
    import json

    from ..appdirs import config_dir

    path = config_dir() / "coreclr.runtimeconfig.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(CORECLR_RUNTIME_CONFIG, indent=2), encoding="utf-8")
    return str(path)


def load_dotnet(loader, runtime_config: Optional[str] = None) -> str:
    """Bind pythonnet to the first .NET runtime that loads: CoreCLR with
    the Windows Desktop runtime config, then the built-in .NET
    Framework. Returns the one that worked; raises the *last* failure if
    none do. ``loader`` is ``pythonnet.load`` (injected for tests). Must
    run before anything imports ``clr`` -- pywebview does so lazily
    inside ``webview.start()``."""
    attempts = [
        ("coreclr", {"runtime_config": runtime_config} if runtime_config else {}),
        ("netfx", {}),
    ]
    last: Optional[BaseException] = None
    for name, kwargs in attempts:
        try:
            loader(name, **kwargs)
            return name
        except Exception as exc:  # pythonnet raises plain RuntimeError
            last = exc
    assert last is not None
    raise last


def _load_dotnet_or_explain() -> None:
    import pythonnet

    try:
        try:
            config: Optional[str] = coreclr_runtime_config_path()
        except OSError:
            config = None  # unwritable config dir: still try the bare runtime
        load_dotnet(pythonnet.load, config)
    except Exception:
        import ctypes
        import webbrowser

        # MB_YESNO | MB_ICONWARNING; IDYES == 6. No window exists yet, so
        # a bare Win32 message box is the only UI available.
        answer = ctypes.windll.user32.MessageBoxW(None, DOTNET_MESSAGE, APP_TITLE, 0x04 | 0x30)
        if answer == 6:
            webbrowser.open(DOTNET_DOWNLOAD_URL)
        sys.exit(1)


def main() -> None:
    """Create the desktop window and block until it's closed.

    Usable both as this file's ``if __name__ == "__main__":`` entry and
    as the ``postmortem-desktop`` console-script target (see
    pyproject.toml's ``[project.scripts]``).

    Passing a plain absolute filesystem path (not a ``file://`` URI) as
    ``url`` is intentional: pywebview's own ``is_local_url()`` check
    detects this isn't ``http(s)://``/``file://`` and automatically
    starts its bundled Bottle-based HTTP server to serve index.html (and
    style.css/app.js alongside it) -- no ``http_server=True`` or manual
    URI conversion needed.
    """
    if sys.platform == "win32":
        _load_dotnet_or_explain()

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
