"""SQLite-backed run history (see store.py)."""

from .store import Store, ingest, query_runs

__all__ = ["Store", "ingest", "query_runs"]
