"""Split a combat log into Mythic+ runs (CHALLENGE_MODE_START .. _END)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional

from .events import Event, to_int, unquote


@dataclass
class RunSegment:
    zone_name: str
    instance_id: Optional[int]
    challenge_map_id: Optional[int]
    keystone_level: Optional[int]
    affixes: list[int]
    start_ts: float
    end_ts: Optional[float] = None
    success: Optional[bool] = None
    duration_ms: Optional[int] = None  # in-game timer from CHALLENGE_MODE_END
    completed: bool = False
    events: list[Event] = field(default_factory=list)

    @property
    def wall_duration(self) -> float:
        end = self.end_ts if self.end_ts is not None else (
            self.events[-1].ts if self.events else self.start_ts
        )
        return max(0.0, end - self.start_ts)

    def summary(self) -> dict[str, Any]:
        return {
            "zone": self.zone_name,
            "instance_id": self.instance_id,
            "challenge_map_id": self.challenge_map_id,
            "keystone_level": self.keystone_level,
            "affixes": self.affixes,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "completed": self.completed,
            "timed": self.success,
            "duration_ms": self.duration_ms,
            "wall_duration_s": round(self.wall_duration, 1),
            "event_count": len(self.events),
        }


def _parse_bracket_ints(value: str) -> list[int]:
    out = []
    for token in value.strip("[] ").split(","):
        token = token.strip()
        if token and token.lstrip("-").isdigit():
            out.append(int(token))
    return out


def segment_runs(events: Iterable[Event]) -> Iterator[RunSegment]:
    """Yield RunSegments for every M+ run found in the event stream.

    A run without a CHALLENGE_MODE_END (crash, log ended, key abandoned)
    is still yielded, marked ``completed=False``.
    """
    current: Optional[RunSegment] = None
    for event in events:
        if event.name == "CHALLENGE_MODE_START":
            p = event.params
            new_run = RunSegment(
                zone_name=unquote(p[0]) if p else "",
                instance_id=to_int(p[1]) if len(p) > 1 else None,
                challenge_map_id=to_int(p[2]) if len(p) > 2 else None,
                keystone_level=to_int(p[3]) if len(p) > 3 else None,
                affixes=_parse_bracket_ints(p[4]) if len(p) > 4 else [],
                start_ts=event.ts,
            )
            if current is not None:
                # A start while a run is open: a /reload re-logs the start
                # for the same key — keep accumulating into the same run.
                same_key = (
                    current.challenge_map_id == new_run.challenge_map_id
                    and current.keystone_level == new_run.keystone_level
                )
                if same_key:
                    current.events.append(event)
                    continue
                yield current  # abandoned run
            current = new_run
            current.events.append(event)
        elif event.name == "CHALLENGE_MODE_END":
            if current is None:
                continue
            p = event.params
            current.end_ts = event.ts
            current.completed = True
            current.success = (to_int(p[1]) if len(p) > 1 else 0) != 0
            current.duration_ms = to_int(p[3]) if len(p) > 3 else None
            current.events.append(event)
            yield current
            current = None
        elif current is not None:
            current.events.append(event)
    if current is not None:
        yield current
