# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Postmortem desktop app.
Run from the repo root (or anywhere -- paths below are resolved off
``SPECPATH``, which PyInstaller injects into this file's exec globals as
the directory containing this .spec file): ``pyinstaller
build/postmortem.spec``.

Assumes ``postmortem`` (with the ``desktop`` extra) and
``pyinstaller`` are already ``pip install``-ed into the current
environment -- see .github/workflows/release-desktop.yml, which does
exactly that before invoking this spec. That real install is what lets
build/entry.py just ``import postmortem.desktop.app`` normally,
sidestepping the repo's src/-layout entirely (same reason the
``postmortem`` and ``postmortem-desktop`` console scripts
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

# SPECPATH is PyInstaller's own injected global for *the directory*
# containing this .spec file (not the file's own path) -- confirmed by
# running this spec for real: os.path.dirname(SPECPATH) landed one level
# too high (the repo root) and couldn't find entry.py at all.
SPEC_DIR = os.path.abspath(SPECPATH)
REPO_ROOT = os.path.dirname(SPEC_DIR)

entry_script = os.path.join(SPEC_DIR, "entry.py")

# App icon: .icns on macOS (BUNDLE() below needs that exact format), .ico
# on Windows (EXE()'s icon= param). Neither format works on the other
# platform, so this is picked per-platform rather than passing one path
# to both -- same reasoning as the sys.platform gate on BUNDLE() itself
# further down. Both files are generated once from the same 1024x1024
# source art (not derived at build time) and checked in here.
if sys.platform == "darwin":
    icon_path = os.path.join(SPEC_DIR, "postmortem.icns")
elif sys.platform == "win32":
    icon_path = os.path.join(SPEC_DIR, "postmortem.ico")
else:
    icon_path = None

# Destination side ('postmortem/desktop/shell') matters more than the
# source side: once frozen, app.py's own
# `Path(__file__).resolve().parent / "shell"` lookup resolves against
# PyInstaller's synthetic __file__ for bundled pure-Python modules, which
# preserves the dotted-package-derived directory structure
# (postmortem/desktop/app.py) under sys._MEIPASS -- so the shell/
# data files need to land at that same 'postmortem/desktop/shell'
# relative path for app.py to find them at runtime.
shell_src = os.path.join(REPO_ROOT, "src", "postmortem", "desktop", "shell")
shell_dst = os.path.join("postmortem", "desktop", "shell")

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
    name="Postmortem",
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
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Postmortem",
)

# macOS only: wrap the onedir COLLECT() output in a real .app bundle.
# A bare Unix executable has no dock icon and awkward Finder
# double-click behavior -- BUNDLE() is the standard PyInstaller way to
# get a proper double-clickable macOS app. Not code-signed/notarized
# (no paid Apple Developer account available) -- see release-desktop.yml's
# top comment for the resulting Gatekeeper-warning caveat.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Postmortem.app",
        icon=icon_path,
        bundle_identifier="com.postmortem.desktop",
    )
