"""Make the process's stdout/stderr safe for anything the reports print.

Windows consoles (and a frozen app's captured stdout) default to a legacy
code page such as cp1252, and ``print()`` of a report line containing a
character it can't represent -- the text report's "≈" (U+2248) -- raises
UnicodeEncodeError. In the desktop app's Watch Live that landed *after*
the run's reports were written and marked the run failed, so it was never
uploaded ("Run failed: 'charmap' codec can't encode character '\\u2248'",
2026-09-08). Reconfiguring the streams to UTF-8 with replacement fixes the
whole class of it; where a stream can't be reconfigured (None under a
windowed frozen build, a non-TextIOWrapper), it's left alone.
"""

from __future__ import annotations

import sys


def make_streams_safe() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def safe_print(*args, **kwargs) -> None:
    """``print`` that degrades to ASCII-with-replacements instead of
    raising when the stream can't encode the text -- for callers that
    run before/without make_streams_safe(), or on an exotic stream."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = kwargs.pop("sep", " ").join(str(a) for a in args)
        stream = kwargs.get("file") or sys.stdout
        if stream is None:
            return
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"), **kwargs)
