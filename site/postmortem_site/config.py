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

# Raw-combat-log upload cap, in bytes, for POST /upload. History: 60MB
# (v1) -> 250MB -> 1GB, each raise justified by "peak memory is bounded
# by one run's worth of events at a time, not the whole file" -- true,
# but incomplete: that reasoning caps memory *per run*, not the memory
# one run itself can need. A real production OOM crash on a real user's
# upload (with the 1GB cap live) traced this back with tracemalloc: a
# single CONTINUOUS run's raw text costs roughly 6-9x its own byte size
# in Python memory just to hold as parsed Event objects (measured, not
# estimated -- a 74MB single run took 458MB to parse, 655MB peak RSS
# end to end). So the real constraint was never the *file's* total size,
# it's the size of whichever single run in it is largest -- which the
# server can't know in advance without parsing the whole thing.
#
# fly.toml's VM memory was raised 512MB -> 2GB for exactly this (see its
# own comment for the full reasoning). This cap is set to what that 2GB
# budget can safely absorb even in the worst case -- the *entire*
# upload turning out to be one continuous run -- with real headroom for
# baseline process overhead (Python/uvicorn/FastAPI/SQLite) and the
# json.dumps()/DB-write steps that follow parsing. A file with several
# separate keys is safe well beyond this (each run's memory is
# independently bounded -- see _handle_log_upload's per-run streaming
# loop), so this number is a worst-case floor, not a typical-case one.
MAX_LOG_BYTES = int(os.environ.get("MYTHIC_SITE_MAX_LOG_BYTES", 200 * 1024 * 1024))

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
