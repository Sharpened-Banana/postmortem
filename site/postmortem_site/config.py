"""Env-driven settings for the public site.

Plain module, no framework imports -- importable/testable with nothing
beyond the stdlib. Every value is a module-level attribute (not baked
into a frozen settings object) so tests can override it after import via
``monkeypatch.setattr(postmortem_site.config, "SOME_VALUE", ...)``; the rest
of this package always reads these through the ``config`` module
(``config.DB_PATH``, etc.) rather than importing the names directly, so
such a monkeypatch actually takes effect.
"""

from __future__ import annotations

import os
from pathlib import Path

# Where the site's SQLite database lives. Overridable via env var so
# tests can point this at a tmp path instead of the real prod location.
DB_PATH = os.environ.get("MYTHIC_SITE_DB", "/data/runs.db")

# Bundled MDT dungeon/enemy data (extracted via `postmortem extract-data`
# against a real, currently-installed MythicDungeonTools -- see
# app.py's _get_dungeon_store()), used for every /upload run so forces
# progress and route-adherence comparison work even though a raw-log
# upload has no way to run `extract-data` itself. A season rotation
# eventually makes this stale -- re-extract and redeploy when that
# happens; there's no auto-refresh. Overridable via env var for tests.
DUNGEON_DATA_PATH = os.environ.get(
    "MYTHIC_SITE_DUNGEON_DATA",
    str(Path(__file__).resolve().parent / "dungeon_data.json"),
)

# Upload body-size cap, in bytes. 5MB is comfortably above the real
# ~1.2MB max report size measured against a real +10 dungeon log this
# session -- Starlette does not enforce a body size cap on its own, so
# app.py's POST /api/runs handler checks this itself.
MAX_BODY_BYTES = int(os.environ.get("MYTHIC_SITE_MAX_BODY_BYTES", 5 * 1024 * 1024))

# Raw-combat-log upload cap, in bytes, for POST /upload -- a blunt
# sanity/DoS ceiling on the whole request (temp-file disk space, not
# holding an unbounded request open forever), nothing more. History:
# 60MB (v1) -> 250MB -> 1GB -> 200MB, that last drop a real production
# incident: this constant used to be doing double duty as BOTH the raw
# upload gate AND the only thing standing between a single giant
# continuous run and an OOM crash, and 200MB was sized for the memory
# concern, not the disk/DoS one -- which meant a perfectly reasonable
# multi-hour, multi-key session log (several separate keys, each one
# individually cheap) got rejected outright just for having a big total
# byte count, even though _handle_log_upload's per-run streaming loop
# was already built to handle exactly that case safely. The two
# concerns are now split: this cap only bounds total request size, and
# MAX_RUN_EVENTS (below) is the real memory-safety limit, enforced per
# run during parsing instead of on the file as a whole. Set generously
# for a real full-evening farming session; a raw temp file this large
# is fine on disk, it was never actually the constraint.
MAX_LOG_BYTES = int(os.environ.get("MYTHIC_SITE_MAX_LOG_BYTES", 500 * 1024 * 1024))

# Per-run memory-safety cap, in Event count, for POST /upload -- passed
# as segment_runs()'s max_run_events so a single CONTINUOUS run gets cut
# off (yielded early, marked truncated) once it reaches this many
# events, instead of accumulating unboundedly in memory. This is the
# real fix for the incident above: a single run's raw text costs
# roughly 6-9x its own byte size in Python memory once parsed into
# Event objects (measured with tracemalloc against a real synthetic
# log, not estimated -- a 74MB single run took 458MB to parse, 655MB
# peak RSS end to end). fly.toml's VM memory was raised 512MB -> 2GB for
# exactly this (see its own comment). Budgeting from that 2GB, minus
# headroom for baseline process overhead (Python/uvicorn/FastAPI/
# SQLite) and the json.dumps()/DB-write steps after parsing, leaves
# roughly 200MB of raw single-run text as safe -- and the fixture log
# used for the tracemalloc measurement averaged ~260 bytes/event, so
# 200MB / 260B is roughly 800,000 events. A truncated run isn't lost
# silently: _handle_log_upload reports it as a failed run with a message
# pointing at the desktop app/CLI (no size limit at all, runs locally)
# for that specific oversized key.
MAX_RUN_EVENTS = int(os.environ.get("MYTHIC_SITE_MAX_RUN_EVENTS", 800_000))

# Rate-limit window (seconds) between two uploads from the same
# X-Upload-Token -- the primary anti-spam guard.
UPLOAD_MIN_INTERVAL_S = int(os.environ.get("MYTHIC_SITE_UPLOAD_MIN_INTERVAL_S", 30))

# Rate-limit window (seconds) between two uploads from the same client
# IP, regardless of token. Shorter than the per-token window since this
# is a coarser secondary guard (one IP can legitimately front several
# uploaders, e.g. a raid group behind the same NAT), not the primary one.
IP_MIN_INTERVAL_S = int(os.environ.get("MYTHIC_SITE_IP_MIN_INTERVAL_S", 10))

# Most-recent-N cap for the public feed (GET /runs, GET /api/runs).
# No further pagination in v1.
FEED_LIMIT = int(os.environ.get("MYTHIC_SITE_FEED_LIMIT", 200))

SITE_TITLE = "Postmortem — Public Runs"
