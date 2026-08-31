"""World->canvas coordinate calibration: closed-form similarity-transform
fit (pure math, tested first in isolation) and the anchor-collection/ICP/
gating pipeline built on top of it (tested against directly-constructed
ActualPull/UnitEngagement/DungeonData objects, per WP-A1's precedent)."""

from __future__ import annotations

import math

import pytest

from postmortem.analysis.mapping import (
    MAX_RESIDUAL,
    MIN_FINAL_ANCHORS,
    build_map_report,
    calibrate,
    fit_transform,
    plan_geometry,
)
from postmortem.analysis.pulls import ActualPull, UnitEngagement
from postmortem.mdt.dungeon_data import DungeonData, Enemy, EnemyClone, MapPOI
from postmortem.mdt.route import Pull, Route


def _apply_known(pts, s, theta, tx, ty, reflect=False):
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    out = []
    for x, y in pts:
        if reflect:
            y = -y
        rx = cos_t * x - sin_t * y
        ry = sin_t * x + cos_t * y
        out.append((s * rx + tx, s * ry + ty))
    return out


WORLD_PTS = [
    (100.0, -50.0), (250.0, -400.0), (10.0, -900.0), (500.0, -120.0), (300.0, -700.0),
]


class TestFitTransformRoundTrip:
    """The most important test in this WP: recover a known transform from
    synthetic correspondences, including a reflected case. Verified first,
    in isolation, before anything downstream depends on it."""

    def test_recovers_known_transform_no_reflection(self):
        s, theta, tx, ty = 2.3, 0.4, 50.0, -30.0
        canvas_pts = _apply_known(WORLD_PTS, s, theta, tx, ty, reflect=False)
        pairs = list(zip(WORLD_PTS, canvas_pts))

        result = fit_transform(pairs)
        assert result is not None
        transform, residual = result

        assert residual == pytest.approx(0.0, abs=1e-6)
        assert transform.reflected is False
        assert transform.scale == pytest.approx(s, abs=1e-6)
        assert transform.rotation == pytest.approx(theta, abs=1e-6)
        assert transform.tx == pytest.approx(tx, abs=1e-6)
        assert transform.ty == pytest.approx(ty, abs=1e-6)

        # and applying it reproduces every canvas point directly
        for (wx, wy), (cx, cy) in pairs:
            px, py = transform.apply(wx, wy)
            assert px == pytest.approx(cx, abs=1e-6)
            assert py == pytest.approx(cy, abs=1e-6)

    def test_recovers_known_transform_with_reflection(self):
        # e.g. world Y-south-positive vs. canvas Y-down (or vice versa) --
        # a real possibility this fitter must handle, per the WP notes.
        s, theta, tx, ty = 1.7, -1.1, -200.0, 400.0
        canvas_pts = _apply_known(WORLD_PTS, s, theta, tx, ty, reflect=True)
        pairs = list(zip(WORLD_PTS, canvas_pts))

        result = fit_transform(pairs)
        assert result is not None
        transform, residual = result

        assert residual == pytest.approx(0.0, abs=1e-6)
        assert transform.reflected is True
        assert transform.scale == pytest.approx(s, abs=1e-6)
        assert transform.tx == pytest.approx(tx, abs=1e-6)
        assert transform.ty == pytest.approx(ty, abs=1e-6)
        for (wx, wy), (cx, cy) in pairs:
            px, py = transform.apply(wx, wy)
            assert px == pytest.approx(cx, abs=1e-6)
            assert py == pytest.approx(cy, abs=1e-6)

    def test_reflected_data_is_not_forced_through_non_reflected_fit(self):
        # Sanity check that the two candidates are actually being compared:
        # a reflected point set fit as if unreflected should NOT land near
        # zero residual (otherwise the "try both, keep the better" logic
        # isn't doing anything).
        s, theta, tx, ty = 1.0, 0.2, 0.0, 0.0
        canvas_pts = _apply_known(WORLD_PTS, s, theta, tx, ty, reflect=True)
        pairs = list(zip(WORLD_PTS, canvas_pts))
        result = fit_transform(pairs)
        assert result is not None
        transform, residual = result
        # the fitter must have picked the reflected candidate to reach ~0
        assert transform.reflected is True
        assert residual == pytest.approx(0.0, abs=1e-6)

    def test_too_few_points_returns_none(self):
        assert fit_transform([((0.0, 0.0), (0.0, 0.0))]) is None
        assert fit_transform([]) is None

    def test_degenerate_coincident_points_returns_none(self):
        # all world points identical -> zero spread, no orientation/scale
        # information to recover.
        pairs = [((5.0, 5.0), (10.0, 10.0)) for _ in range(4)]
        assert fit_transform(pairs) is None


# --- anchor collection / ICP / gating ------------------------------------

NPC_SOLO_A = 900001  # single clone -- unambiguous seed anchor
NPC_SOLO_B = 900002
NPC_SOLO_C = 900003
NPC_SOLO_D = 900004
NPC_MULTI = 900005   # two clones -- only usable after ICP disambiguation


def _dungeon_with_clones() -> DungeonData:
    return DungeonData(
        dungeon_idx=999,
        name="Calibration Test Dungeon",
        enemies=[
            Enemy(enemy_idx=1, npc_id=NPC_SOLO_A, name="Solo A", count=1,
                  clones=[EnemyClone(x=100.0, y=-100.0, sublevel=1)]),
            Enemy(enemy_idx=2, npc_id=NPC_SOLO_B, name="Solo B", count=1,
                  clones=[EnemyClone(x=300.0, y=-150.0, sublevel=1)]),
            Enemy(enemy_idx=3, npc_id=NPC_SOLO_C, name="Solo C", count=1,
                  clones=[EnemyClone(x=500.0, y=-350.0, sublevel=1)]),
            Enemy(enemy_idx=4, npc_id=NPC_SOLO_D, name="Solo D", count=1,
                  clones=[EnemyClone(x=200.0, y=-450.0, sublevel=1)]),
            Enemy(enemy_idx=5, npc_id=NPC_MULTI, name="Multi", count=2,
                  clones=[EnemyClone(x=600.0, y=-100.0, sublevel=1),
                          EnemyClone(x=50.0, y=-500.0, sublevel=1)]),
        ],
    )


def _unit(npc_id: int, world_pos, spawn: str) -> UnitEngagement:
    return UnitEngagement(
        guid=f"Creature-0-1-1-1-{npc_id}-{spawn}", npc_id=npc_id, name="",
        first_ts=0.0, last_ts=1.0, died_at=1.0, first_pos=world_pos,
    )


# The known transform used to generate synthetic "world" positions from the
# dungeon's canvas clone positions, for calibration tests below.
_S, _THETA, _TX, _TY = 4.0, 0.15, 1000.0, -2000.0


def _world_for(cx: float, cy: float) -> tuple[float, float]:
    """Invert the known canvas<-world transform to get a synthetic world
    position for a given canvas point (so canvas = fit(world) recovers
    _S/_THETA/_TX/_TY)."""
    # forward: canvas = S * R(theta) * world + T  =>  world = R(-theta)/S * (canvas - T)
    dx, dy = cx - _TX, cy - _TY
    cos_t, sin_t = math.cos(-_THETA), math.sin(-_THETA)
    wx = (cos_t * dx - sin_t * dy) / _S
    wy = (sin_t * dx + cos_t * dy) / _S
    return (wx, wy)


class TestCalibrate:
    def test_insufficient_seed_anchors_fails_safe(self):
        dungeon = _dungeon_with_clones()
        # only 2 single-clone npcs engaged -- below MIN_SEED_ANCHORS
        pull = ActualPull(index=1, units=[
            _unit(NPC_SOLO_A, _world_for(100.0, -100.0), "0001"),
            _unit(NPC_SOLO_B, _world_for(300.0, -150.0), "0002"),
        ])
        result = calibrate([pull], dungeon)
        assert result.ok is False
        assert result.reason == "insufficient anchors"
        assert result.transform is None

    def test_no_position_data_fails_safe_with_specific_reason(self):
        dungeon = _dungeon_with_clones()
        pull = ActualPull(index=1, units=[
            UnitEngagement(guid="Creature-0-1-1-1-900001-0001", npc_id=NPC_SOLO_A,
                            name="", first_ts=0.0, last_ts=1.0, died_at=1.0,
                            first_pos=None),
        ])
        result = calibrate([pull], dungeon)
        assert result.ok is False
        assert result.reason == "no position data (advanced combat logging required)"

    def test_clean_anchor_set_recovers_confident_fit(self):
        dungeon = _dungeon_with_clones()
        pull = ActualPull(index=1, units=[
            _unit(NPC_SOLO_A, _world_for(100.0, -100.0), "0001"),
            _unit(NPC_SOLO_B, _world_for(300.0, -150.0), "0002"),
            _unit(NPC_SOLO_C, _world_for(500.0, -350.0), "0003"),
            _unit(NPC_SOLO_D, _world_for(200.0, -450.0), "0004"),
        ])
        result = calibrate([pull], dungeon)
        assert result.ok is True
        assert result.reason is None
        assert result.anchor_count >= MIN_FINAL_ANCHORS
        assert result.residual is not None and result.residual < 1.0
        assert result.transform.scale == pytest.approx(_S, rel=1e-3)

    def test_icp_disambiguates_multi_clone_npc(self):
        # With a confident transform from the 4 solo anchors, the ambiguous
        # NPC_MULTI (engaged near its clone at (600,-100)) should get
        # correctly assigned to that clone rather than the other one at
        # (50,-500), growing the anchor count.
        dungeon = _dungeon_with_clones()
        pull = ActualPull(index=1, units=[
            _unit(NPC_SOLO_A, _world_for(100.0, -100.0), "0001"),
            _unit(NPC_SOLO_B, _world_for(300.0, -150.0), "0002"),
            _unit(NPC_SOLO_C, _world_for(500.0, -350.0), "0003"),
            _unit(NPC_SOLO_D, _world_for(200.0, -450.0), "0004"),
            _unit(NPC_MULTI, _world_for(600.0, -100.0), "0005"),
        ])
        result = calibrate([pull], dungeon)
        assert result.ok is True
        assert result.anchor_count == 5
        assert result.residual < 1.0

    def test_degenerate_anchor_set_rejected(self):
        # 4 seed anchors that are all mutually collinear/degenerate for a
        # similarity fit (all on one line -> no rotation/scale information)
        # should not be forced through.
        dungeon = DungeonData(
            dungeon_idx=999, name="Degenerate",
            enemies=[
                Enemy(enemy_idx=1, npc_id=1, name="A", count=1,
                      clones=[EnemyClone(x=10.0, y=0.0)]),
                Enemy(enemy_idx=2, npc_id=2, name="B", count=1,
                      clones=[EnemyClone(x=10.0, y=0.0)]),
                Enemy(enemy_idx=3, npc_id=3, name="C", count=1,
                      clones=[EnemyClone(x=10.0, y=0.0)]),
            ],
        )
        pull = ActualPull(index=1, units=[
            _unit(1, (5.0, 5.0), "0001"),
            _unit(2, (5.0, 5.0), "0002"),
            _unit(3, (5.0, 5.0), "0003"),
        ])
        result = calibrate([pull], dungeon)
        assert result.ok is False
        assert result.reason in ("degenerate anchor geometry", "insufficient anchors")

    def test_noisy_anchors_with_low_residual_still_pass_within_threshold(self):
        dungeon = _dungeon_with_clones()
        # nudge one anchor's world position slightly off its "true" inverse
        # so a small but nonzero residual results; should still pass since
        # it's well under MAX_RESIDUAL.
        wx, wy = _world_for(500.0, -350.0)
        pull = ActualPull(index=1, units=[
            _unit(NPC_SOLO_A, _world_for(100.0, -100.0), "0001"),
            _unit(NPC_SOLO_B, _world_for(300.0, -150.0), "0002"),
            _unit(NPC_SOLO_C, (wx + 0.05, wy - 0.05), "0003"),
            _unit(NPC_SOLO_D, _world_for(200.0, -450.0), "0004"),
        ])
        result = calibrate([pull], dungeon)
        assert result.ok is True
        assert 0 < result.residual < MAX_RESIDUAL


class TestPlanGeometry:
    def test_geometry_without_route(self):
        dungeon = _dungeon_with_clones()
        geo = plan_geometry(dungeon, route=None, comparison=None)
        assert geo["canvas"] == {"width": 840, "height": 555}
        assert len(geo["enemies"]) == 6  # 4 solo + 2 multi clones
        assert all(e["plan_pull"] is None for e in geo["enemies"])
        assert all(e["deviated"] is False for e in geo["enemies"])
        # bounds should cover all clone coordinates with padding
        xs = [e["x"] for e in geo["enemies"]]
        ys = [e["y"] for e in geo["enemies"]]
        assert geo["bounds"]["min_x"] <= min(xs)
        assert geo["bounds"]["max_x"] >= max(xs)
        assert geo["bounds"]["min_y"] <= min(ys)
        assert geo["bounds"]["max_y"] >= max(ys)

    def test_geometry_with_route_assigns_plan_pull(self):
        dungeon = _dungeon_with_clones()
        route = Route(
            name="Test", dungeon_idx=999, week=None, difficulty=None,
            pulls=[Pull(index=1, enemies={1: [1], 2: [1]})],
        )
        geo = plan_geometry(dungeon, route=route, comparison=None)
        by_npc = {e["npc_id"]: e for e in geo["enemies"]}
        assert by_npc[NPC_SOLO_A]["plan_pull"] == 1
        assert by_npc[NPC_SOLO_B]["plan_pull"] == 1
        assert by_npc[NPC_SOLO_C]["plan_pull"] is None

    def test_geometry_flags_deviated_plan_pulls(self):
        dungeon = _dungeon_with_clones()
        route = Route(
            name="Test", dungeon_idx=999, week=None, difficulty=None,
            pulls=[Pull(index=1, enemies={1: [1]})],
        )
        comparison = {
            "pulls": [{"actual_pull": 1, "primary_plan_pull": 1, "deviations": 2}],
            "missed": {},
        }
        geo = plan_geometry(dungeon, route=route, comparison=comparison)
        by_npc = {e["npc_id"]: e for e in geo["enemies"]}
        assert by_npc[NPC_SOLO_A]["deviated"] is True

    def test_geometry_includes_pois(self):
        dungeon = _dungeon_with_clones()
        dungeon.pois = {1: [MapPOI(type="dungeonEntrance", x=779.77, y=-509.6, size_mult=1.5)]}
        geo = plan_geometry(dungeon)
        assert geo["pois"] == [
            {"type": "dungeonEntrance", "x": 779.77, "y": -509.6, "size_mult": 1.5}
        ]


class TestBuildMapReport:
    def test_falls_back_plan_only_with_reason_when_uncalibrated(self):
        dungeon = _dungeon_with_clones()
        pull = ActualPull(index=1, units=[
            _unit(NPC_SOLO_A, _world_for(100.0, -100.0), "0001"),
        ])
        report = build_map_report(
            dungeon, None, None, [pull], position_samples={}, player_names={},
            deaths=[], run_start_ts=0.0,
        )
        assert report["calibration"]["ok"] is False
        assert report["calibration"]["reason"] == "insufficient anchors"
        assert report["players"] == []
        assert report["deaths"] == []
        assert len(report["enemies"]) == 6  # geometry still present

    def test_transforms_player_paths_and_deaths_when_calibrated(self):
        dungeon = _dungeon_with_clones()
        pull = ActualPull(index=1, units=[
            _unit(NPC_SOLO_A, _world_for(100.0, -100.0), "0001"),
            _unit(NPC_SOLO_B, _world_for(300.0, -150.0), "0002"),
            _unit(NPC_SOLO_C, _world_for(500.0, -350.0), "0003"),
            _unit(NPC_SOLO_D, _world_for(200.0, -450.0), "0004"),
        ])
        wx, wy = _world_for(300.0, -150.0)
        samples = {"Player-1": [[0.0, wx, wy, 111]]}

        from postmortem.analysis.stats import DeathRecord
        deaths = [DeathRecord(ts=0.0, player_guid="Player-1", player_name="Zug",
                               pull_index=1, killing_blow=None)]

        report = build_map_report(
            dungeon, None, None, [pull], position_samples=samples,
            player_names={"Player-1": "Zug"}, deaths=deaths, run_start_ts=0.0,
        )
        assert report["calibration"]["ok"] is True
        (player,) = report["players"]
        assert player["name"] == "Zug"
        (t, px, py) = player["path"][0]
        assert px == pytest.approx(300.0, abs=0.5)
        assert py == pytest.approx(-150.0, abs=0.5)
        (death,) = report["deaths"]
        assert death["player"] == "Zug"
        assert death["x"] == pytest.approx(300.0, abs=0.5)
        assert death["y"] == pytest.approx(-150.0, abs=0.5)
