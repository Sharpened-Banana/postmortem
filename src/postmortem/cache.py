"""Generic on-disk cache for ``Fetcher``-shaped API lookups.

``Fetcher`` here means the same shape used by :mod:`postmortem.raiderio`:
a callable that takes a URL and returns a parsed JSON dict, or ``None`` on
failure (network error, 404, etc.). ``cached_fetcher`` wraps any such
callable with a JSON-file-backed cache keyed by the request URL, so
repeated lookups for the same URL within the TTL skip the network entirely.

Built for wrapping ``raiderio._default_fetcher``, but deliberately generic:
a later work package adding a second kind of cached lookup (e.g. Raider.io's
static dungeon-timer data) can reuse ``cache_dir()`` and ``cached_fetcher()``
with its own ``filename`` rather than re-deriving the directory-resolution
or cache-file logic.

This is a single-user CLI tool invoked once at a time in normal use, not a
server: cache writes are a plain whole-file read-modify-write with no
locking. A corrupt/unparseable existing cache file is treated as empty
rather than raised -- unlike an explicitly-passed ``--dungeon-data`` or
``--avoidable-data`` path, this file is our own cache, not user-supplied
configuration, so the bar is "don't crash and don't lose the ability to
keep working," not "raise a clear CLI error."
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

Fetcher = Callable[[str], Optional[dict]]

#: Env var giving the cache *directory* (not a specific file path).
ENV_VAR = "MYTHIC_ANALYZER_CACHE"

#: Default time-to-live for a cache entry, in seconds.
DEFAULT_TTL_SECONDS = 6 * 60 * 60


def _resolve_cache_dir() -> Path:
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".cache" / "postmortem"


def cache_dir() -> Path:
    """Directory cache files live in.

    Honors ``$MYTHIC_ANALYZER_CACHE`` (interpreted as a directory) when
    set; otherwise defaults to ``~/.cache/postmortem``. Other cached
    data sources should call this (or pass their own ``cache_dir=`` through
    to :func:`cached_fetcher`) rather than re-deriving the override logic.
    """
    return _resolve_cache_dir()


def _load_cache(path: Path) -> dict[str, Any]:
    """Read the cache file at ``path``, tolerating missing/corrupt content."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def cached_fetcher(
    fetcher: Fetcher,
    filename: str = "raiderio.json",
    *,
    cache_dir: Optional[Path] = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    clock: Callable[[], float] = time.time,
) -> Fetcher:
    """Wrap ``fetcher`` with a JSON-file disk cache keyed by request URL.

    Returns a new ``Fetcher``-shaped callable (drop-in anywhere a
    ``Fetcher`` is expected) that:

    - on a hit (URL present in the cache, fetched less than ``ttl_seconds``
      ago): returns the cached data without calling ``fetcher`` at all.
    - on a miss (absent, or present but stale): calls ``fetcher``. If it
      returns non-``None``, the result is written to the cache file
      (creating the cache directory if needed) before being returned. If
      it returns ``None`` (a failed lookup), nothing is cached -- a
      transient failure gets retried next time rather than being
      remembered as a permanent miss for the TTL window.

    ``filename`` is the cache file's name within the resolved cache
    directory (default ``raiderio.json``, matching the character-lookup
    fetch this was built for) -- a different cached data source should
    pass its own ``filename`` to share the directory without colliding.

    ``cache_dir`` overrides directory resolution (mainly for tests);
    defaults to :func:`cache_dir` (the module-level function of the same
    name).

    ``clock`` overrides the time source (mainly for tests simulating TTL
    expiry); defaults to :func:`time.time`.
    """
    directory = Path(cache_dir) if cache_dir is not None else _resolve_cache_dir()
    path = directory / filename

    def _fetch(url: str) -> Optional[dict]:
        cache = _load_cache(path)
        entry = cache.get(url)
        if isinstance(entry, dict):
            ts = entry.get("ts")
            if isinstance(ts, (int, float)) and (clock() - ts) < ttl_seconds:
                return entry.get("data")

        result = fetcher(url)
        if result is not None:
            cache[url] = {"ts": clock(), "data": result}
            _save_cache(path, cache)
        return result

    return _fetch
