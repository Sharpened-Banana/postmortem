"""Shared fixtures for the mythic_site test suite.

``pytest.importorskip("fastapi")`` at module scope means this whole file
-- and therefore this whole test suite -- just skips cleanly if fastapi
isn't installed, so the existing top-level ``tests/`` suite (a separate,
parallel work package's territory) is unaffected either way.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
_SITE = _REPO_ROOT / "site"

# Mirrors the sys.path setup the main tests/conftest.py already does for
# `src` (this repo also has an editable install of mythic-analyzer, so
# this is mostly a belt-and-suspenders fallback); `site` needs the same
# treatment so `import mythic_site` resolves without the package being
# separately pip-installed.
for _p in (str(_SRC), str(_SITE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Reuse the main suite's synthetic-dungeon/route/log builders
# (build_run_log, ROUTE_PRESET, DUNGEON_DATA) to get a real, fully-valid
# report dict out of the actual analyze_run() pipeline, rather than
# hand-rolling a fixture report dict that could silently drift out of
# sync with what analyze_run() actually produces.
#
# Loaded via importlib under a private module name -- NOT via
# `sys.path.insert(str(tests_dir)); import conftest` -- because pytest
# itself auto-imports *this* file (site/tests/conftest.py) as a conftest
# module, and the top-level tests/conftest.py has the same basename;
# aliasing both to the bare name "conftest" risks either a stale
# sys.modules hit (silently reusing whichever one got imported first) or
# pytest's own "import file mismatch" error. A distinct module name
# sidesteps the whole question.
_conftest_spec = importlib.util.spec_from_file_location(
    "_mythic_site_shared_test_helpers", _REPO_ROOT / "tests" / "conftest.py"
)
_shared = importlib.util.module_from_spec(_conftest_spec)
sys.modules[_conftest_spec.name] = _shared
_conftest_spec.loader.exec_module(_shared)

from fastapi.testclient import TestClient  # noqa: E402

from mythic_analyzer.analysis.run_analyzer import analyze_run  # noqa: E402
from mythic_analyzer.combatlog.parser import iter_events  # noqa: E402
from mythic_analyzer.combatlog.segmenter import segment_runs  # noqa: E402
from mythic_analyzer.mdt.dungeon_data import DungeonDataStore  # noqa: E402
from mythic_analyzer.mdt.route import Route  # noqa: E402

from mythic_site import config as site_config  # noqa: E402
from mythic_site.app import app  # noqa: E402


@pytest.fixture()
def site_db(tmp_path, monkeypatch) -> Path:
    """Point mythic_site.config.DB_PATH at a fresh per-test SQLite file
    so tests never touch a real /data/runs.db.

    Monkeypatching the module attribute (not an env var) works because
    every handler in app.py reads `config.DB_PATH` dynamically at
    request time rather than capturing its value at import time.
    """
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(site_config, "DB_PATH", str(db_path))
    return db_path


@pytest.fixture()
def report(tmp_path) -> dict:
    """A real, fully-valid report dict produced by the actual
    analyze_run() pipeline against the main suite's synthetic dungeon,
    route and combat log (a tiny but complete M+ run)."""
    (run_segment,) = list(
        segment_runs(iter_events(_shared.build_run_log().lines))
    )
    route = Route.from_preset(_shared.ROUTE_PRESET)

    dungeon_data_path = tmp_path / "mdt_data.json"
    dungeon_data_path.write_text(json.dumps(_shared.DUNGEON_DATA), encoding="utf-8")
    store = DungeonDataStore.load(dungeon_data_path)

    return analyze_run(run_segment, route=route, store=store)


@pytest.fixture()
def client(site_db) -> TestClient:
    return TestClient(app)
