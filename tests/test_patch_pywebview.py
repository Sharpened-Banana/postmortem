"""build/patch_pywebview.py: the build-time patch that lets pywebview's
Windows backend import under modern .NET."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "patch_pywebview", Path(__file__).resolve().parents[1] / "build" / "patch_pywebview.py"
)
patch_pywebview = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = patch_pywebview
_SPEC.loader.exec_module(patch_pywebview)

SAMPLE = '''import things

class BrowserView:
    pass


class OpenFolderDialog:
    flags = 3
    windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
    iFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+IFileDialog')

    @classmethod
    def show(cls, parent=None, initialDirectory=None, allow_multiple=False, title=None):
        return None


_main_window_created = Event()
_main_window_created.clear()
'''


class TestPatchSource:
    def test_wraps_the_class_and_adds_the_fallback(self):
        out, status = patch_pywebview.patch_source(SAMPLE)
        assert status == "patched"
        compile(out, "winforms.py", "exec")  # still valid Python
        assert "try:\n    class OpenFolderDialog:" in out
        assert "except Exception:" in out and "FolderBrowserDialog()" in out
        # everything around the class is untouched
        assert out.startswith("import things\n\nclass BrowserView:")
        assert out.endswith("_main_window_created = Event()\n_main_window_created.clear()\n")

    def test_is_idempotent(self):
        once, _ = patch_pywebview.patch_source(SAMPLE)
        twice, status = patch_pywebview.patch_source(once)
        assert status == "already" and twice == once

    def test_refuses_an_unfamiliar_file(self):
        with pytest.raises(ValueError, match="OpenFolderDialog not found"):
            patch_pywebview.patch_source("class Something:\n    pass\n")

    def test_applies_to_the_installed_pywebview(self):
        # the real thing, so a pywebview upgrade that moves the class
        # fails here before it fails the Windows release build
        pytest.importorskip("webview")
        source = patch_pywebview.winforms_path().read_text(encoding="utf-8")
        out, status = patch_pywebview.patch_source(source)
        assert status in ("patched", "already")
        compile(out, "winforms.py", "exec")
        assert "OpenFolderDialog.show(" in out  # the caller pywebview keeps using
