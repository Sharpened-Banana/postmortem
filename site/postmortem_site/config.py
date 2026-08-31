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

# Where the site's SQLite database lives. Overridable via env var so
# tests can point this at a tmp path instead of the real prod location.
DB_PATH = os.environ.get("MYTHIC_SITE_DB", "/data/runs.db")

# Upload body-size cap, in bytes. 5MB is comfortably above the real
# ~1.2MB max report size measured against a real +10 dungeon log this
# session -- Starlette does not enforce a body size cap on its own, so
# app.py's POST /api/runs handler checks this itself.
MAX_BODY_BYTES = int(os.environ.get("MYTHIC_SITE_MAX_BODY_BYTES", 5 * 1024 * 1024))

# Raw-combat-log upload cap, in bytes, for POST /upload (a whole
# WoWCombatLog.txt, not an analyzed report -- much bigger than
# MAX_BODY_BYTES since a real advanced-logging session can run tens of
# MB). 60MB comfortably covers a multi-key farming session.
MAX_LOG_BYTES = int(os.environ.get("MYTHIC_SITE_MAX_LOG_BYTES", 60 * 1024 * 1024))

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
