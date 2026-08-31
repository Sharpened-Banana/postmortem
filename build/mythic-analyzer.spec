# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Mythic Analyzer desktop app.
Run from the repo root (or anywhere -- paths below are resolved off
``SPECPATH``, which PyInstaller injects into this file's exec globals as
the directory containing this .spec file): ``pyinstaller
build/mythic-analyzer.spec``.

Assumes ``mythic_analyzer`` (with the ``desktop`` extra) and
``pyinstaller`` are already ``pip install``-ed into the current
environment -- see .github/workflows/release-desktop.yml, which does
exactly that before invoking this spec. That real install is what lets
build/entry.py just ``import mythic_analyzer.desktop.app`` normally,
sidestepping the repo's src/-layout entirely (same reason the
``mythic-analyzer`` and ``mythic-analyzer-desktop`` console scripts
don't need any sys.path handling either).

This single spec file targets both macOS and Windows CI runners (see the
build matrix in release-desktop.yml) -- PyInstaller specs are just
Python, so the mac-only BUNDLE() step below is gated on ``sys.platform``
rather than needing two separate spec files.

pywebview's own PyInstaller hook (``webview/__pyinstaller/hook-webview.py``,
auto-discovered via its registered ``pyinstaller40`` entry point) already
handles pywebview's own bundled JS bridge files and, on Windows, its
WebView2 loader DLLs -- so no manual ``hiddenimports``/``datas`` entries
are needed for pywebview itself here, only for this project's own
shell/ assets below.
"""

import os
import sys

SPEC_DIR = os.path.dirname(os.path.abspath(SPECPATH))
REPO_ROOT = os.path.dirname(SPEC_DIR)

entry_script = os.path.join(SPEC_DIR, "entry.py")

# Destination side ('mythic_analyzer/desktop/shell') matters more than the
# source side: once frozen, app.py's own
# `Path(__file__).resolve().parent / "shell"` lookup resolves against
# PyInstaller's synthetic __file__ for bundled pure-Python modules, which
# preserves the dotted-package-derived directory structure
# (mythic_analyzer/desktop/app.py) under sys._MEIPASS -- so the shell/
# data files need to land at that same 'mythic_analyzer/desktop/shell'
# relative path for app.py to find them at runtime.
shell_src = os.path.join(REPO_ROOT, "src", "mythic_analyzer", "desktop", "shell")
shell_dst = os.path.join("mythic_analyzer", "desktop", "shell")

a = Analysis(
    [entry_script],
    pathex=[],
    binaries=[],
    datas=[(shell_src, shell_dst)],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MythicAnalyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Windowed (no console): this is a GUI app, not a CLI tool -- a
    # console=True build would pop a terminal window alongside the app
    # window on Windows and would break the macOS .app bundle.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MythicAnalyzer",
)

# macOS only: wrap the onedir COLLECT() output in a real .app bundle.
# A bare Unix executable has no dock icon and awkward Finder
# double-click behavior -- BUNDLE() is the standard PyInstaller way to
# get a proper double-clickable macOS app. No custom icon exists in this
# repo yet (confirmed -- no .icns/.ico anywhere), so PyInstaller's
# default icon is used; that's an accepted gap for this pass, not an
# oversight. Not code-signed/notarized either (no paid Apple Developer
# account available) -- see release-desktop.yml's top comment for the
# resulting Gatekeeper-warning caveat.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="MythicAnalyzer.app",
        icon=None,
        bundle_identifier="com.mythicanalyzer.desktop",
    )
