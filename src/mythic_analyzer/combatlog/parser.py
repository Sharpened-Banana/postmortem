"""Line-level parsing of WoWCombatLog.txt.

Handles both timestamp flavors:

    old (pre-10.1.7):  4/20 21:23:41.301  EVENT,params...
    new:               4/20/2026 21:23:41.301-4  EVENT,params...

Timestamps are converted to POSIX seconds in local time (the combat log is
written in the player's local clock; the trailing UTC offset in the new
format is recorded but times remain local so in-run deltas are exact).
For year-less logs the year is inferred (file mtime by default) with
December->January rollover handling.
"""

from __future__ import annotations

import calendar
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .events import Event


class CombatLogParseError(ValueError):
    pass


def split_params(text: str) -> list[str]:
    """Split a combat-log parameter string on top-level commas.

    Commas inside double-quoted strings and inside []/() groups
    (COMBATANT_INFO, affix lists...) do not split.
    """
    if '"' not in text and "[" not in text and "(" not in text:
        return text.split(",")
    out: list[str] = []
    buf: Optional[str] = None
    in_quotes = False
    depth = 0
    for part in text.split(","):
        if buf is None:
            if (
                '"' not in part and "[" not in part and "(" not in part
                and "]" not in part and ")" not in part
            ):
                out.append(part)
                continue
            buf = part
        else:
            buf += "," + part
        for ch in part:
            if in_quotes:
                if ch == '"':
                    in_quotes = False
            elif ch == '"':
                in_quotes = True
            elif ch == "[" or ch == "(":
                depth += 1
            elif ch == "]" or ch == ")":
                depth -= 1
        if not in_quotes and depth <= 0:
            out.append(buf)
            buf = None
            depth = 0
    if buf is not None:
        out.append(buf)
    return out


@dataclass
class _ClockState:
    """Tracks inferred year for year-less logs (and month rollover)."""

    year: int
    last_month: int = 0

    def observe(self, month: int) -> int:
        if self.last_month and month < self.last_month and self.last_month == 12:
            self.year += 1
        self.last_month = month
        return self.year


_DAY_CACHE: dict[tuple[int, int, int], float] = {}


def _day_epoch(year: int, month: int, day: int) -> float:
    key = (year, month, day)
    cached = _DAY_CACHE.get(key)
    if cached is None:
        cached = float(
            time.mktime((year, month, day, 0, 0, 0, 0, 1, -1))
        )
        _DAY_CACHE[key] = cached
    return cached


def parse_line(
    line: str,
    line_no: int = 0,
    clock: Optional[_ClockState] = None,
) -> Optional[Event]:
    """Parse one combat log line into an Event; None if not parseable."""
    line = line.rstrip("\r\n")
    if not line:
        return None
    # timestamp and payload are separated by two spaces
    sep = line.find("  ")
    if sep < 0:
        return None
    stamp = line[:sep]
    payload = line[sep + 2:]
    if not payload:
        return None

    try:
        date_part, time_part = stamp.split(" ", 1)
        dfields = date_part.split("/")
        month = int(dfields[0])
        day = int(dfields[1])
        if len(dfields) >= 3:
            year = int(dfields[2])
        elif clock is not None:
            year = clock.observe(month)
        else:
            year = datetime.now().year

        # strip a trailing UTC offset like "-4", "+13", "-04:30"
        offset = None
        for i, ch in enumerate(time_part):
            if ch in "+-" and i > 0:
                offset = time_part[i:]
                time_part = time_part[:i]
                break
        hh, mm, ss = time_part.split(":")
        ts = _day_epoch(year, month, day) + int(hh) * 3600 + int(mm) * 60 + float(ss)
    except (ValueError, IndexError):
        return None

    params = split_params(payload)
    name = params[0]
    return Event(ts=ts, name=name, params=params[1:], line_no=line_no, utc_offset=offset)


def iter_events(
    lines: Iterable[str],
    base_year: Optional[int] = None,
) -> Iterator[Event]:
    clock = _ClockState(year=base_year or datetime.now().year)
    for line_no, line in enumerate(lines, start=1):
        event = parse_line(line, line_no, clock)
        if event is not None:
            yield event


def parse_file(path: str | Path, base_year: Optional[int] = None) -> Iterator[Event]:
    """Stream events from a WoWCombatLog.txt file."""
    path = Path(path)
    if base_year is None:
        try:
            base_year = time.localtime(os.path.getmtime(path)).tm_year
        except OSError:
            base_year = datetime.now().year
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        yield from iter_events(fh, base_year)
