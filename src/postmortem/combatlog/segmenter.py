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
    truncated: bool = False
    events: list[Event] = field(default_factory=list)

    @property
    def wall_duration(self) -> float:
        end = self.end_ts if self.end_ts is not None else (
            self.events[-1].ts if self.events else self.start_ts
        )
        return max(0.0, end - self.start_ts)

    @property
    def likely_abandoned(self) -> bool:
        """Best-effort guess for a run with no CHALLENGE_MODE_END: did the
        group leave the instance (hard-abandon vote, or just walking/
        hearthing out) rather than the log merely being cut off mid-key or
        a crash? Only meaningful when ``completed`` is already False --
        WoW never writes an explicit "abandoned" marker to the combat log
        itself (confirmed: the real client event that fires on an abandon,
        CHALLENGE_MODE_RESET, is Lua-only and never reaches the combat log
        text file), so this is inference, not a certain answer.

        Looks for a ZONE_CHANGE to a different zone than this run's own
        instance_id anywhere in this segment's events. Deliberately checks
        ZONE_CHANGE (the overall instance/zone), not MAP_CHANGE (which
        changes between a multi-floor dungeon's own sublevels *without*
        leaving the instance -- using it here would false-positive on a
        normal floor transition mid-key).
        """
        if self.instance_id is None:
            return False
        for event in self.events:
            if event.name != "ZONE_CHANGE" or not event.params:
                continue
            raw = event.params[0].strip()
            if not raw.lstrip("-").isdigit():
                continue
            if int(raw) != self.instance_id:
                return True
        return False

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


def segment_runs(
    events: Iterable[Event], max_run_events: Optional[int] = None,
) -> Iterator[RunSegment]:
    """Yield RunSegments for every M+ run found in the event stream.

    A run without a CHALLENGE_MODE_END (crash, log ended, key abandoned)
    is still yielded, marked ``completed=False``.

    ``max_run_events``, when given, caps how many events a single run
    accumulates before it's yielded early (marked ``truncated=True``,
    ``completed=False``) and the rest of that run's events are dropped
    until the next CHALLENGE_MODE_START -- exactly like an abandoned run
    otherwise, just cut off by size instead of by a missing END. Default
    None preserves the original unbounded behavior for every existing
    caller (CLI, desktop app) -- this exists for postmortem_site's
    upload path, which has a real per-run memory ceiling a local CLI/
    desktop run never needs (see config.py's MAX_RUN_EVENTS comment).
    """
    current: Optional[RunSegment] = None
    for event in events:
        if (
            max_run_events is not None
            and current is not None
            and len(current.events) >= max_run_events
        ):
            current.truncated = True
            yield current
            current = None
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
            total_time_ms = to_int(p[3]) if len(p) > 3 else 0
            # WoW fires an all-zeroed phantom CHALLENGE_MODE_END
            # ("...END,<id>,0,0,0,0.000000,0.000000") immediately before
            # *every* CHALLENGE_MODE_START -- confirmed against real logs
            # (2026-09-01), where every one of 13 keys' pre-start phantoms
            # had timed=0/level=0/totalTimeMs=0, and every real end (timed
            # OR depleted) had a nonzero totalTimeMs. totalTimeMs
            # (params[3]) == 0 is thus the phantom's unambiguous signature:
            # a run that reaches a real END always ran for nonzero time.
            #
            # A phantom on an OPEN run means that run is being abandoned/
            # reset without a real completion (the next key is spinning
            # up). Yield it as abandoned right here rather than closing it
            # as a false "depleted" completion (completed=True,
            # success=False, duration 0). Yielding at the phantom -- not
            # deferring to the next CHALLENGE_MODE_START's "different key"
            # branch -- is what correctly handles an abandoned key
            # followed by another key in the *same* dungeon at the *same*
            # level: that next START would otherwise hit the /reload
            # same-key MERGE path and fold the two runs into one. The
            # phantom END between them is exactly what distinguishes a real
            # key transition (phantom present) from a mid-key /reload (bare
            # re-logged START, no phantom) -- so keying the split on the
            # phantom, not on the START, gets both cases right.
            if total_time_ms == 0:
                yield current  # abandoned run
                current = None
                continue
            # Instance-mismatch defense: a stray *real* END (nonzero
            # duration) for some other instance shouldn't close this run.
            end_instance = p[0].strip() if p else ""
            if (
                end_instance.lstrip("-").isdigit()
                and int(end_instance) != current.instance_id
            ):
                current.events.append(event)
                continue
            current.end_ts = event.ts
            current.completed = True
            current.success = (to_int(p[1]) if len(p) > 1 else 0) != 0
            current.duration_ms = total_time_ms
            current.events.append(event)
            yield current
            current = None
        elif current is not None:
            current.events.append(event)
    if current is not None:
        yield current
