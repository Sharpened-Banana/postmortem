"""Shared fixtures for the postmortem_site test suite.

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
# `src` (this repo also has an editable install of postmortem, so
# this is mostly a belt-and-suspenders fallback); `site` needs the same
# treatment so `import postmortem_site` resolves without the package being
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
    "_postmortem_site_shared_test_helpers", _REPO_ROOT / "tests" / "conftest.py"
)
_shared = importlib.util.module_from_spec(_conftest_spec)
sys.modules[_conftest_spec.name] = _shared
_conftest_spec.loader.exec_module(_shared)

from fastapi.testclient import TestClient  # noqa: E402

from postmortem.analysis.run_analyzer import analyze_run  # noqa: E402
from postmortem.combatlog.parser import iter_events  # noqa: E402
from postmortem.combatlog.segmenter import segment_runs  # noqa: E402
from postmortem.mdt.dungeon_data import DungeonDataStore  # noqa: E402
from postmortem.mdt.route import Route  # noqa: E402

from postmortem_site import config as site_config  # noqa: E402
from postmortem_site.app import app  # noqa: E402


@pytest.fixture()
def site_db(tmp_path, monkeypatch) -> Path:
    """Point postmortem_site.config.DB_PATH at a fresh per-test SQLite file
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
def raw_log_text() -> str:
    """The main suite's synthetic-but-fully-valid WoWCombatLog.txt text,
    for exercising POST /upload's own parse_file/segment_runs/analyze_run
    pipeline directly -- unlike the `report` fixture above, which already
    runs that pipeline and hands back the resulting dict."""
    return _shared.build_run_log().text()


@pytest.fixture()
def route_string() -> str:
    """A real MDT export string decoding to _shared.ROUTE_PRESET -- for
    exercising POST /upload's optional pasted-route field."""
    return _shared.encode_mdt_string(_shared.ROUTE_PRESET, "mdt2")


def _point_dungeon_store_at(monkeypatch, tmp_path, payload: dict, name: str = "dungeon_data.json"):
    """Shared helper: point postmortem_site.config.DUNGEON_DATA_PATH at a
    fresh file holding `payload`, and reset app.py's module-level
    dungeon-store cache so the swap actually takes effect this test (see
    app.py's _get_dungeon_store() docstring on why it's cached at all --
    without resetting the cache, whichever store another test already
    triggered a load of would stick around)."""
    import json as _json

    from postmortem_site import app as app_module
    from postmortem_site import config as site_config

    path = tmp_path / name
    path.write_text(_json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(site_config, "DUNGEON_DATA_PATH", str(path))
    monkeypatch.setattr(app_module, "_dungeon_store", None)
    monkeypatch.setattr(app_module, "_dungeon_store_loaded", False)
    return path


@pytest.fixture(autouse=True)
def isolated_dungeon_store(monkeypatch, tmp_path):
    """Autouse for the whole test session: points the dungeon-data
    bundle at an empty (zero-dungeon) file by default, so no test
    accidentally exercises the real production bundle -- raw_log_text's
    synthetic run's challenge_map_id (587) is deliberately realistic
    (matches real Murder Row), so without this every test would silently
    pick up real forces/dungeon data from whatever's actually bundled,
    coupling unrelated tests' behavior to the current WoW season and
    making the "no dungeon data" case impossible to test at all. Tests
    that specifically want dungeon data present use
    dungeon_store_with_data below, which overrides this.
    """
    _point_dungeon_store_at(monkeypatch, tmp_path, {"dungeons": {}})


@pytest.fixture()
def dungeon_store_with_data(monkeypatch, tmp_path):
    """Opt-in override of isolated_dungeon_store: points the dungeon-data
    bundle at _shared.DUNGEON_DATA, which does match raw_log_text's
    synthetic run (challenge_map_id 587) -- for tests exercising the
    "forces/route comparison actually populate" path specifically."""
    return _point_dungeon_store_at(monkeypatch, tmp_path, _shared.DUNGEON_DATA)


@pytest.fixture()
def client(site_db) -> TestClient:
    return TestClient(app)
