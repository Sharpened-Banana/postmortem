"""Desktop app auto-update: check GitHub Releases for a newer
``alpha-desktop-N`` build than the one currently running, download it,
and swap it into place.

Self-replacing a running GUI app's own files needs a process that
outlives the app itself -- you can't reliably delete/overwrite the
directory a process is currently executing out of *from that same
process* (definitely not on Windows, where the running .exe is
file-locked). The standard shape (Sparkle on macOS, Squirrel on
Windows, and every other self-updater) is: stage the new build
somewhere else first, hand off to a small detached helper script that
(1) waits for this process's PID to exit, (2) moves the old install
aside as a backup (never hard-deleted -- if the swap goes wrong, the
previous working build is still sitting right there), (3) moves the
staged build into the old install's place, (4) relaunches it, then the
running app exits to let that happen. ``apply_update_and_relaunch()``
below is that hand-off; the actual "wait and swap" logic runs in a
small platform-native script (``/bin/sh`` on macOS, PowerShell on
Windows) written to a temp file rather than a second bundled
executable, since a shell/PowerShell script needs no separate
PyInstaller build of its own.

Every public function here either returns a clearly-optional result
(``check_for_update`` -> ``None`` means "nothing to report", never an
error) or is meant to be called from inside ``desktop/api.py``'s own
try/except (matching that module's "never raise across the JS bridge"
contract at the API layer, not this one) -- the download/extract/apply
steps *do* raise on a real problem (corrupt zip, missing expected
files, disk full), since those are genuine failures the caller needs
to see and report, not something to silently swallow this deep.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from ._version import VERSION

# A PyInstaller-frozen build has no CA certificate bundle of its own --
# confirmed as the real cause of a real report (2026-09-01: "no update
# banner" on a real installed macOS build, with the update check itself
# never actually failing loudly -- reproduced directly: without a
# locatable cert store, urllib.request.urlopen() over https:// raises
# ssl.SSLCertVerificationError, wrapped as a urllib.error.URLError,
# which _default_fetcher's own except clause (by design, to never let a
# network hiccup crash the update check) was silently swallowing every
# single time. ``certifi`` ships its own CA bundle as a real file
# (certifi.where()), which is what actually needs bundling -- passing
# it explicitly here, rather than relying on the interpreter's own
# guess at a system cert path, is what makes this work the same whether
# frozen or not. Only imported here (desktop/updater.py is desktop-only
# code, never imported by the plain CLI or the site) -- tolerant of it
# being missing anyway (falls back to the interpreter's own default
# verification behavior) rather than a hard ImportError, since a
# missing optional dependency shouldn't be worse than the bug this
# fixes.
try:
    import certifi
    _SSL_CONTEXT: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

REPO = "Sharpened-Banana/postmortem"
_RELEASES_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
_TAG_RE = re.compile(r"^alpha-desktop-(\d+)$")
_DOWNLOAD_CHUNK_BYTES = 262_144  # 256KB -- matches postmortem_site's own streaming-read chunk size

# Release assets only ever come from a GitHub-hosted URL (see
# release-desktop.yml's own softprops/action-gh-release step) -- this is
# a defense-in-depth check before downloading-and-relaunching whatever
# check_for_update() returned, not a trust boundary this module is
# actually crossing (the URL always originates from our own GitHub API
# call two lines up the call stack, never from user input).
_TRUSTED_DOWNLOAD_HOSTS = ("github.com", "objects.githubusercontent.com")

Fetcher = Callable[[str], Optional[dict]]
ProgressCallback = Callable[[dict], None]


def _current_build_number() -> Optional[int]:
    m = _TAG_RE.match(VERSION)
    return int(m.group(1)) if m else None


def _asset_name_for_platform() -> Optional[str]:
    if sys.platform == "darwin":
        return "Postmortem-macos.zip"
    if sys.platform == "win32":
        return "Postmortem-windows.zip"
    return None


def _default_fetcher(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={
        "User-Agent": "postmortem-desktop",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CONTEXT) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def check_for_update(fetcher: Fetcher = _default_fetcher) -> Optional[dict]:
    """Returns ``{"tag": str, "download_url": str, "notes": str}`` if a
    newer ``alpha-desktop-N`` build is published on GitHub than the one
    currently running, else ``None``.

    ``None`` covers every "nothing to report" case alike -- a dev/
    unstamped build, no network, a malformed or non-alpha-N latest tag,
    a platform with no published asset, or a latest release that's the
    same build or older -- deliberately not distinguished from each
    other, since none of them are errors a user needs to see; there's
    just no update.
    """
    current = _current_build_number()
    if current is None:
        return None
    asset_name = _asset_name_for_platform()
    if asset_name is None:
        return None
    payload = fetcher(_RELEASES_LATEST_URL)
    if not payload:
        return None
    tag = payload.get("tag_name", "")
    m = _TAG_RE.match(tag)
    if not m or int(m.group(1)) <= current:
        return None
    asset = next(
        (a for a in payload.get("assets", []) if a.get("name") == asset_name), None,
    )
    download_url = asset.get("browser_download_url") if asset else None
    if not download_url:
        return None
    return {"tag": tag, "download_url": download_url, "notes": payload.get("body") or ""}


def _is_trusted_download_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname in _TRUSTED_DOWNLOAD_HOSTS


def download_update(url: str, dest: Path, on_progress: Optional[ProgressCallback] = None) -> Path:
    """Stream ``url`` to ``dest``, fixed-size chunks (never the whole
    response body in memory at once -- same reasoning as
    postmortem_site's own chunked upload reads). Raises ``ValueError``
    for an untrusted URL, or ``OSError``/``urllib.error.URLError`` for a
    real network/disk failure -- this is an internal step of a larger
    flow the caller already wraps in its own error handling, not a
    boundary that needs to swallow failures itself.
    """
    if not _is_trusted_download_url(url):
        raise ValueError(f"refusing to download from untrusted host: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "postmortem-desktop"})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        written = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if on_progress is not None:
                    on_progress({"written": written, "total": total})
    return dest


def _safe_extract(zip_path: Path, dest_dir: Path) -> None:
    """Extract a zip archive by hand -- NOT ``ZipFile.extractall()``,
    which does neither of the two things a macOS ``.app`` bundle
    actually needs. Confirmed as a real bug (2026-09-01): a real
    self-applied update downloaded, validated, and swapped in cleanly,
    but the result was completely unlaunchable
    ("Launchd job spawn failed"). Root cause, found by diffing the
    broken build against a known-good one extracted the normal way (via
    ``ditto``/``unzip``, not this module):

    1. ``extractall()`` doesn't restore Unix permission bits at all --
       the main executable came out ``-rw-r--r--`` instead of
       ``-rwxr-xr-x``, so nothing could even launch it.
    2. ``extractall()`` doesn't reconstruct symlinks -- each one came
       out as an ordinary file whose *content* is the literal target
       path text, not a real symlink. This project's own build
       (``build/postmortem.spec``) has real symlinks inside the bundle
       (e.g. ``Contents/Frameworks/postmortem`` -> ``../Resources/
       postmortem``), and macOS's own ``Python.framework`` layout is
       symlink-heavy internally (``Versions/Current`` -> ``Versions/A``,
       etc.) -- losing those breaks the bundle's own internal structure,
       not just this project's files.

    A zip entry's Unix mode (including the ``S_IFLNK`` file-type bit)
    lives in ``ZipInfo.external_attr``'s upper 16 bits -- the same
    encoding ``ditto``/``unzip``/``zip`` all honor. For an entry with
    that bit set, the entry's stored "content" is the link target text,
    not file data. The Windows zip (``Compress-Archive``, no Unix
    concept of any of this) simply never sets these bits, so every
    branch below that depends on them is a no-op there -- no
    platform-specific handling needed here.

    Still rejects any entry that would escape ``dest_dir`` (an absolute
    path, or one that ``..``s out) -- defense-in-depth for an archive
    that, while normally only ever produced by our own CI, is about to
    be relaunched as a real executable, so it's cheap insurance to
    actually check first.
    """
    with zipfile.ZipFile(zip_path) as zf:
        dest_resolved = dest_dir.resolve()
        infos = zf.infolist()
        targets: dict[str, Path] = {}
        for info in infos:
            target = (dest_dir / info.filename).resolve()
            if dest_resolved not in target.parents and target != dest_resolved:
                raise ValueError(f"refusing to extract unsafe zip entry: {info.filename}")
            targets[info.filename] = target

        # Directories (and every entry's parent dir) before any file/
        # symlink content -- a zip isn't guaranteed to list explicit
        # directory entries in (or even at all) before the files inside
        # them.
        for info in infos:
            if info.is_dir():
                targets[info.filename].mkdir(parents=True, exist_ok=True)
            else:
                targets[info.filename].parent.mkdir(parents=True, exist_ok=True)

        for info in infos:
            if info.is_dir():
                continue
            target = targets[info.filename]
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(zf.read(info.filename).decode("utf-8"))
            else:
                with zf.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                if mode:
                    target.chmod(mode)

        # Directory permissions last -- a restrictive mode applied
        # before its contents are written could block writing them.
        for info in infos:
            if not info.is_dir():
                continue
            mode = info.external_attr >> 16
            if mode:
                targets[info.filename].chmod(mode)


def _validate_macos_bundle(extract_dir: Path) -> Path:
    bundle = extract_dir / "Postmortem.app"
    exe = bundle / "Contents" / "MacOS" / "Postmortem"
    if not exe.exists():
        raise ValueError(f"downloaded build looks incomplete -- missing {exe}")
    return bundle


def _validate_windows_install(extract_dir: Path) -> Path:
    exe = extract_dir / "Postmortem.exe"
    if not exe.exists():
        raise ValueError(f"downloaded build looks incomplete -- missing {exe}")
    return extract_dir


def extract_update(zip_path: Path, staging_dir: Path) -> Path:
    """Extract a downloaded release zip into ``staging_dir`` and return
    the path to the validated new install (a ``Postmortem.app`` bundle
    on macOS, the extracted folder itself on Windows -- the two
    platforms' zips differ in this exact way, see
    release-desktop.yml's own packaging steps: ``ditto --keepParent``
    keeps the ``.app`` as a top-level entry, ``Compress-Archive -Path
    dist/Postmortem/*`` does not wrap its contents in a folder at all).
    Raises ``ValueError`` if the expected executable isn't where it
    should be after extracting.
    """
    _safe_extract(zip_path, staging_dir)
    if sys.platform == "darwin":
        return _validate_macos_bundle(staging_dir)
    if sys.platform == "win32":
        return _validate_windows_install(staging_dir)
    raise ValueError(f"no update support for this platform: {sys.platform}")


def _current_install_root() -> Path:
    """The root of the currently-running frozen build: the ``.app``
    bundle on macOS, the install folder on Windows. Only meaningful
    when actually frozen (``sys.frozen`` -- set by PyInstaller's
    bootloader); callers must check that first, since ``sys.executable``
    in a normal ``python`` run just points at the interpreter itself.
    """
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        # .../Postmortem.app/Contents/MacOS/Postmortem -> Postmortem.app
        return exe.parents[2]
    return exe.parent  # Windows: .../Postmortem/Postmortem.exe -> Postmortem/


_MACOS_RELAUNCH_SCRIPT = """#!/bin/sh
# Auto-generated by postmortem's desktop updater -- see updater.py.
set -e
PID="$1"
OLD_APP="$2"
NEW_APP="$3"
BACKUP_APP="$4"

while kill -0 "$PID" 2>/dev/null; do
  sleep 0.3
done
sleep 0.5

rm -rf "$BACKUP_APP" 2>/dev/null || true
if [ -e "$OLD_APP" ]; then
  mv "$OLD_APP" "$BACKUP_APP"
fi
mv "$NEW_APP" "$OLD_APP"
open -n "$OLD_APP"
"""

_WINDOWS_RELAUNCH_SCRIPT = """# Auto-generated by postmortem's desktop updater -- see updater.py.
param(
    [int]$ProcId,
    [string]$OldDir,
    [string]$NewDir,
    [string]$BackupDir
)
try { Wait-Process -Id $ProcId -Timeout 30 -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Seconds 1

if (Test-Path $BackupDir) { Remove-Item -Recurse -Force $BackupDir -ErrorAction SilentlyContinue }
if (Test-Path $OldDir) { Move-Item -Path $OldDir -Destination $BackupDir }
Move-Item -Path $NewDir -Destination $OldDir
Start-Process -FilePath (Join-Path $OldDir "Postmortem.exe")
"""


def apply_update_and_relaunch(new_install_path: Path, pid: Optional[int] = None) -> None:
    """Hand off to a detached helper script that waits for this process
    to exit, moves the current install aside as a timestamped backup
    (never deleted outright), moves ``new_install_path`` into its place,
    and relaunches it. Does **not** exit this process itself -- the
    caller (``desktop/api.py``) does that once it's told the UI an
    update is about to relaunch it, so the message actually has a
    moment to render first.

    Only valid inside a frozen build (``sys.frozen``); raises
    ``RuntimeError`` otherwise -- there's no "current install" to swap
    out of a source checkout.
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("apply_update_and_relaunch() only makes sense in a frozen build")

    pid = pid if pid is not None else os.getpid()
    old_root = _current_install_root()
    backup_root = old_root.with_name(old_root.name + f".backup-{int(time.time())}")
    helper_dir = Path(tempfile.mkdtemp(prefix="postmortem-update-"))

    if sys.platform == "darwin":
        script_path = helper_dir / "relaunch.sh"
        script_path.write_text(_MACOS_RELAUNCH_SCRIPT, encoding="utf-8")
        script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
        args = ["/bin/sh", str(script_path), str(pid), str(old_root),
                str(new_install_path), str(backup_root)]
    elif sys.platform == "win32":
        script_path = helper_dir / "relaunch.ps1"
        script_path.write_text(_WINDOWS_RELAUNCH_SCRIPT, encoding="utf-8")
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script_path),
                "-ProcId", str(pid), "-OldDir", str(old_root),
                "-NewDir", str(new_install_path), "-BackupDir", str(backup_root)]
    else:
        raise RuntimeError(f"no update support for this platform: {sys.platform}")

    # Detached: must outlive this process. start_new_session (POSIX) /
    # CREATE_NEW_PROCESS_GROUP (Windows) keep it from being killed
    # alongside this one when this process exits.
    creationflags = 0x00000200 if sys.platform == "win32" else 0  # CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        args,
        cwd=str(helper_dir),
        start_new_session=(sys.platform != "win32"),
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def perform_update(
    download_url: str, work_dir: Path, on_progress: Optional[ProgressCallback] = None,
) -> Path:
    """Download and extract an update into ``work_dir``, returning the
    validated new install's path (ready for ``apply_update_and_relaunch``).
    Raises on any real failure -- see ``download_update``/
    ``extract_update`` -- the caller (``desktop/api.py``) is what turns
    that into a reported ``{"type": "failed", ...}`` event instead of an
    unhandled exception on the update thread.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    zip_path = work_dir / "update.zip"
    download_update(download_url, zip_path, on_progress=on_progress)
    staging_dir = work_dir / "extracted"
    staging_dir.mkdir(parents=True, exist_ok=True)
    new_install = extract_update(zip_path, staging_dir)
    zip_path.unlink(missing_ok=True)
    return new_install
