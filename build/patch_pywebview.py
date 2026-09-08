"""Make pywebview's Windows backend load under modern .NET (CoreCLR).

Run against the installed pywebview *before* PyInstaller freezes it
(.github/workflows/release-desktop.yml does; for a local Windows build:
``python build/patch_pywebview.py`` after ``pip install -e ".[desktop]"``).

Why: the desktop app has to host pywebview on CoreCLR (see
postmortem/desktop/app.py), and pywebview's ``platforms/winforms.py``
defines an ``OpenFolderDialog`` whose *class body* reflects on private
internals of the .NET Framework's file dialog
(``FileDialogNative+IFileDialog``, ``CreateVistaDialog``, ...). Modern
.NET's Windows Forms has none of them, so ``Assembly.GetType`` returns
None and the module dies at import with "'NoneType' object has no
attribute 'GetMethod'" (alpha-desktop-33, 2026-09-08) -- taking the
whole window backend with it.

What: wraps that class in ``try``/``except`` and, when it can't be
built, defines an ``OpenFolderDialog`` with the same ``show()`` contract
on top of ``System.Windows.Forms.FolderBrowserDialog`` -- which on
modern .NET *is* the Vista-style picker the original was reaching for
through reflection. Idempotent; exits non-zero if pywebview's source no
longer looks like it expects, so a pywebview upgrade can't silently
ship an unpatched build.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# patched by postmortem/build/patch_pywebview.py"
CLASS_HEADER = "class OpenFolderDialog:\n"
CLASS_END = "_main_window_created = Event()\n"

FALLBACK = '''except Exception:  {marker}
    # Modern .NET's Windows Forms has none of the .NET Framework internals
    # the class above reflects on; its FolderBrowserDialog is the same
    # Vista-style picker, reached the supported way.
    class OpenFolderDialog:
        @classmethod
        def show(cls, parent=None, initialDirectory=None, allow_multiple=False, title=None):
            dialog = WinForms.FolderBrowserDialog()
            if initialDirectory:
                dialog.InitialDirectory = initialDirectory
            if title:
                dialog.Description = title
                dialog.UseDescriptionForTitle = True
            try:
                dialog.Multiselect = bool(allow_multiple)  # .NET 8+
            except Exception:
                pass
            result = dialog.ShowDialog(parent) if parent is not None else dialog.ShowDialog()
            if result != WinForms.DialogResult.OK:
                return None
            try:
                paths = tuple(dialog.SelectedPaths)
            except Exception:
                paths = (dialog.SelectedPath,)
            return paths or None


'''


def patch_source(source: str) -> tuple[str, str]:
    """``(patched_source, status)`` where status is "patched", "already"
    or raises ValueError when the expected class isn't there."""
    if MARKER in source:
        return source, "already"
    start = source.find("\n" + CLASS_HEADER)
    if start < 0:
        raise ValueError("class OpenFolderDialog not found")
    start += 1
    end = source.find("\n" + CLASS_END, start)
    if end < 0:
        raise ValueError("end of OpenFolderDialog (the _main_window_created line) not found")
    end += 1
    block = source[start:end].rstrip("\n") + "\n"
    indented = "".join(("    " + line if line.strip() else line) for line in block.splitlines(keepends=True))
    wrapped = "try:\n" + indented + "\n" + FALLBACK.format(marker=MARKER)
    return source[:start] + wrapped + source[end:], "patched"


def winforms_path() -> Path:
    import webview  # the installed one, wherever it is

    return Path(webview.__file__).resolve().parent / "platforms" / "winforms.py"


def main() -> int:
    path = winforms_path()
    try:
        patched, status = patch_source(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"patch_pywebview: {path}: {exc} -- pywebview changed; update this script", file=sys.stderr)
        return 1
    if status == "patched":
        compile(patched, str(path), "exec")  # never leave a broken module behind
        path.write_text(patched, encoding="utf-8")
    print(f"patch_pywebview: {status}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
