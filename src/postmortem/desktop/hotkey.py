"""A system-wide hotkey for the snapshot feature (docs/SNAPSHOT.md §3).

The addon's keybind plants its marker by toggling combat logging, which
costs a few 0.1 s gaps in the log. The desktop app is already running
and tailing that log while the user plays, so it can take the keypress
itself: a global hotkey fires here, the press is timestamped on this
machine's clock (the same clock WoW stamps the log with), and the
snapshot is sliced around it with no change to logging at all. The
addon keybind stays as the fallback for anyone playing without the app
in reach.

Two backends, both without new dependencies:

- **Windows**: ``RegisterHotKey`` via ctypes on a thread that pumps its
  own message queue (a hotkey is delivered to the registering thread).
- **macOS**: an ``NSEvent`` global key-down monitor (AppKit, which the
  pywebview build already ships). macOS only delivers those once the app
  has the *Input Monitoring* permission; the listener asks for it the
  first time and reports what to do if it is missing.

Anything else (Linux) reports "unsupported" and the addon keybind is the
only trigger. Never raises: the whole thing is best-effort, and a hotkey
that could not be set up must not stop Watch Live from starting.
"""

from __future__ import annotations

import re

import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

MODIFIER_NAMES = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "win": "cmd", "super": "cmd", "meta": "cmd",
}

#: Keys we accept beyond single characters: function keys and a few named
#: ones that are natural for a "don't hit this by accident" binding.
NAMED_KEYS = {f"f{i}" for i in range(1, 13)} | {
    "space", "insert", "delete", "home", "end", "pageup", "pagedown",
    "pause", "scrolllock", "printscreen",
}


@dataclass(frozen=True)
class Combo:
    modifiers: frozenset[str]
    key: str  # a single lowercase character, or a NAMED_KEYS entry

    def __str__(self) -> str:
        order = ("ctrl", "alt", "shift", "cmd")
        parts = [m for m in order if m in self.modifiers]
        return "+".join(parts + [self.key])


def parse_combo(text: str) -> Combo:
    """``"ctrl+alt+s"`` -> Combo. Case-insensitive, whitespace-tolerant.
    Raises ValueError on an empty key, an unknown modifier, or a key
    that is neither one character nor a named key. At least one modifier
    is required: a bare letter would type into the game."""
    # "+", "-" and whitespace all separate parts ("shift `" is what a
    # person types when the box does not say otherwise, 2026-09-18).
    raw = str(text or "").strip()
    trailing_key = ""
    if len(raw) > 2 and raw[-1] in "+-" and raw[-2] in "+- \t":
        # the key itself is a separator character ("shift+-", "ctrl++");
        # a lone trailing "+" ("ctrl+") is still an unfinished combo
        trailing_key, raw = raw[-1], raw[:-1]
    parts = [p.strip().lower() for p in re.split(r"[+\-\s]+", raw)] if raw else []
    if trailing_key:
        parts.append(trailing_key)
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError("no key")
    key = parts[-1]
    mods = set()
    for p in parts[:-1]:
        if p not in MODIFIER_NAMES:
            raise ValueError(f"unknown modifier {p!r}")
        mods.add(MODIFIER_NAMES[p])
    if not (len(key) == 1 or key in NAMED_KEYS):
        raise ValueError(f"unknown key {key!r}")
    if not mods:
        raise ValueError("a hotkey needs at least one modifier (e.g. ctrl+shift+f9)")
    return Combo(frozenset(mods), key)


class Backend:
    """What a platform backend must do. ``start`` returns (ok, message)
    and must call ``on_press`` (from any thread) on each press."""

    def start(self, combo: Combo, on_press: Callable[[], None]) -> tuple[bool, str]:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


# --- Windows -----------------------------------------------------------------

_WIN_MODS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "cmd": 0x0008}
_WIN_NAMED_VK = {
    "space": 0x20, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "pause": 0x13, "scrolllock": 0x91,
    "printscreen": 0x2C, **{f"f{i}": 0x6F + i for i in range(1, 13)},
}
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


class WindowsBackend(Backend):
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._result: Optional[tuple[bool, str]] = None
        self._ready = threading.Event()

    def start(self, combo: Combo, on_press: Callable[[], None]) -> tuple[bool, str]:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if combo.key in _WIN_NAMED_VK:
            vk = _WIN_NAMED_VK[combo.key]
        else:
            # VkKeyScanW returns a SHORT, -1 for "no key on this layout".
            # ctypes assumes int unless told, so that -1 could come back
            # as 65535, fail the check, and register virtual key 0xFF --
            # a hotkey that "works" and never fires. Declare the real
            # type, and compare the low 16 bits either way.
            try:
                user32.VkKeyScanW.restype = ctypes.c_short
                user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
            except (AttributeError, TypeError):
                pass
            scan = int(user32.VkKeyScanW(combo.key))
            if scan & 0xFFFF == 0xFFFF:
                return False, f"no virtual key for {combo.key!r} on this keyboard layout"
            vk = scan & 0xFF
        mods = _MOD_NOREPEAT
        for m in combo.modifiers:
            mods |= _WIN_MODS[m]

        def loop() -> None:
            self._thread_id = kernel32.GetCurrentThreadId()
            if not user32.RegisterHotKey(None, 1, mods, vk):
                self._result = (False, f"{combo} is already taken by another program "
                                       "(RegisterHotKey failed)")
                self._ready.set()
                return
            self._result = (True, f"hotkey {combo} active")
            self._ready.set()
            msg = wintypes.MSG()
            try:
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == _WM_HOTKEY:
                        try:
                            on_press()
                        except Exception:
                            pass
            finally:
                user32.UnregisterHotKey(None, 1)

        self._thread = threading.Thread(target=loop, name="postmortem-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self._result or (False, "hotkey thread did not start")

    def stop(self) -> None:
        if self._thread_id is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._thread_id = None


# --- macOS ---------------------------------------------------------------------

_MAC_MOD_FLAGS = {"ctrl": 1 << 18, "alt": 1 << 19, "shift": 1 << 17, "cmd": 1 << 20}
_MAC_NAMED_KEYCODES = {
    "space": 49, "insert": 114, "delete": 117, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
PERMISSION_HINT = (
    "macOS needs permission for the app to see keys pressed in other apps: "
    "System Settings > Privacy & Security > Input Monitoring > enable Postmortem, "
    "then start Watch Live again"
)


#: US-layout shifted punctuation -> the key it lives on. AppKit's
#: charactersIgnoringModifiers ignores every modifier EXCEPT Shift, so a
#: "shift+`" press reports "~"; without this the combo never matched.
_US_UNSHIFT = {
    "~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
    "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[",
    "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/",
}


def _unshift(chars: str) -> str:
    """The unshifted key for what a shifted press reports ("~" -> "`",
    "S" -> "s"); anything else unchanged."""
    return _US_UNSHIFT.get(chars, chars.lower())


class MacBackend(Backend):
    def __init__(self) -> None:
        self._monitor = None

    def start(self, combo: Combo, on_press: Callable[[], None]) -> tuple[bool, str]:
        try:
            import AppKit
            from PyObjCTools import AppHelper
        except Exception as exc:
            return False, f"AppKit unavailable ({exc})"
        try:
            from Quartz import CGPreflightListenEventAccess, CGRequestListenEventAccess
            if not CGPreflightListenEventAccess():
                CGRequestListenEventAccess()  # shows the system prompt once
                return False, PERMISSION_HINT
        except Exception:
            pass  # older macOS without the API: the monitor simply works or not

        wanted_flags = 0
        for m in combo.modifiers:
            wanted_flags |= _MAC_MOD_FLAGS[m]
        all_flags = sum(_MAC_MOD_FLAGS.values())
        keycode = _MAC_NAMED_KEYCODES.get(combo.key)

        def handler(event):
            try:
                flags = int(event.modifierFlags()) & all_flags
                if flags != wanted_flags:
                    return
                if keycode is not None:
                    hit = int(event.keyCode()) == keycode
                else:
                    chars = str(event.charactersIgnoringModifiers() or "")
                    hit = _unshift(chars) == combo.key
                if hit:
                    on_press()
            except Exception:
                pass

        done = threading.Event()
        outcome: dict = {}

        def install():
            try:
                self._monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                    AppKit.NSEventMaskKeyDown, handler)
                outcome["ok"] = self._monitor is not None
            except Exception as exc:
                outcome["error"] = str(exc)
            done.set()

        # The monitor must be installed from the main (AppKit) thread;
        # pywebview runs the app loop there.
        if threading.current_thread() is threading.main_thread():
            install()
        else:
            AppHelper.callAfter(install)
            done.wait(timeout=5.0)
        if outcome.get("ok"):
            return True, f"hotkey {combo} active"
        return False, outcome.get("error") or "could not install the key monitor"

    def stop(self) -> None:
        if self._monitor is None:
            return
        try:
            import AppKit
            AppKit.NSEvent.removeMonitor_(self._monitor)
        except Exception:
            pass
        self._monitor = None


class UnsupportedBackend(Backend):
    def start(self, combo, on_press):
        return False, f"global hotkeys are not supported on {sys.platform}; use the addon keybind"

    def stop(self) -> None:
        pass


def default_backend() -> Backend:
    if sys.platform == "win32":
        return WindowsBackend()
    if sys.platform == "darwin":
        return MacBackend()
    return UnsupportedBackend()


@dataclass
class HotkeyListener:
    """``start()`` -> (ok, message); ``stop()``. ``on_press`` may be called
    from a non-main thread; keep it quick and thread-safe."""

    combo_text: str
    on_press: Callable[[], None]
    # Looked up at construction, not at class definition, so a test (or a
    # platform shim) can swap default_backend on the module.
    backend: Backend = field(default_factory=lambda: default_backend())
    combo: Optional[Combo] = None
    active: bool = False

    def start(self) -> tuple[bool, str]:
        try:
            self.combo = parse_combo(self.combo_text)
        except ValueError as exc:
            return False, f"bad hotkey {self.combo_text!r}: {exc}"
        try:
            ok, message = self.backend.start(self.combo, self.on_press)
        except Exception as exc:
            ok, message = False, f"could not set up the hotkey: {exc}"
        self.active = bool(ok)
        return ok, message

    def stop(self) -> None:
        if not self.active:
            return
        try:
            self.backend.stop()
        finally:
            self.active = False
