"""WoWCombatLog.txt parsing: tokenizer, event model, GUIDs, run segmentation."""

from .parser import parse_line, parse_file, iter_events
from .events import Event
from .segmenter import segment_runs, RunSegment

__all__ = [
    "parse_line",
    "parse_file",
    "iter_events",
    "Event",
    "segment_runs",
    "RunSegment",
]
