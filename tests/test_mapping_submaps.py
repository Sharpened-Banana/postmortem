"""Per-sub-map route-map calibration (mapping.calibrate_maps and friends),
plus the MDT clone-list extraction fix it depends on.

Background (2026-09-03, real reports): a dungeon's MDT canvas is a
composite of several Blizzard uiMaps, each pasted at its own scale and
offset, so one global similarity transform can never fit -- the real
Ruby Life Pools fit had RMS 138 on 8 boss anchors, and Murder Row's
"successful" fit put player paths 200 units off the canvas. These tests
pin down the per-uiMap model that replaced it, and the two anchor-quality
bugs found alongside it (clones lost to gaps in MDT's Lua arrays; positions
never captured when the opening hit's advanced block was the player's)."""

from __future__ import annotations

import math
import textwrap

import pytest

from postmortem.analysis.mapping import (
    CANVAS_H,
    CANVAS_W,
    MAP_MAX_RESIDUAL,
    MIN_MAP_ANCHORS,
    _map_normalized,
    build_map_report,
    calibrate_maps,
    collect_map_bounds,
    entrance_anchor,
    plan_geometry,
)
from postmortem.analysis.pulls import ActualPull, UnitEngagement, collect_engagements
from postmortem.combatlog.events import Event
from postmortem.mdt.dungeon_data import DungeonData, Enemy, EnemyClone, MapPOI
from postmortem.mdt.extract import extract_dungeon_file
from postmortem.mdt.route import Pull, Route

# Two sub-maps with different world rectangles, pasted onto the canvas at
# different scales/offsets -- the shape of every real Midnight dungeon.
MAP_A, MAP_B = 2094, 2095
BOUNDS = {
    MAP_A: (1868.75, 1345.83, 181.25, -602.08),
    MAP_B: (1685.0, 1335.0, 75.0, -450.0),
}
PLACEMENT = {  # ui_map_id -> (scale, tx, ty) used to synthesize canvas positions
    MAP_A: (1.25, 47.0, 65.0),
    MAP_B: (1.05, -250.0, -59.0),
}


def _canvas_for(ui_map_id: int, wx: float, wy: float) -> tuple[float, float]:
    if ui_map_id not in PLACEMENT:  # a sub-map the run never logged bounds for
        return (wx, wy)
    s, tx, ty = PLACEMENT[ui_map_id]
    ux, uy = _map_normalized(BOUNDS[ui_map_id], wx, wy)
    return (s * ux + tx, s * uy + ty)


def _unit(npc_id, world, ui_map_id, spawn="0001"):
    return UnitEngagement(
        guid=f"Creature-0-1-1-1-{npc_id}-{spawn}", npc_id=npc_id, name="",
        first_ts=0.0, last_ts=1.0, died_at=1.0, first_pos=world, first_map_id=ui_map_id,
    )


def _dungeon(anchors: dict[int, tuple[int, tuple[float, float]]], entrance=None) -> DungeonData:
    """One single-clone enemy per (npc_id -> (ui_map_id, world_pos)), with
    its clone placed exactly where the synthetic placement says."""
    enemies = []
    for i, (npc_id, (ui_map_id, world)) in enumerate(anchors.items(), start=1):
        cx, cy = _canvas_for(ui_map_id, *world)
        enemies.append(Enemy(enemy_idx=i, npc_id=npc_id, name=f"npc{npc_id}", count=1,
                             clones=[EnemyClone(x=cx, y=cy, sublevel=1, idx=1)]))
    pois = {}
    if entrance is not None:
        pois = {1: [MapPOI(type="dungeonEntrance", x=entrance[0], y=entrance[1], size_mult=1.5)]}
    return DungeonData(dungeon_idx=999, name="Composite Test Dungeon", enemies=enemies, pois=pois)


ANCHORS = {
    # map A: five mobs spread across the rectangle
    9001: (MAP_A, (1553.0, -145.0)),
    9002: (MAP_A, (1571.0, -342.0)),
    9003: (MAP_A, (1643.0, -133.0)),
    9004: (MAP_A, (1779.0, 25.0)),
    9005: (MAP_A, (1791.0, 21.0)),
    # map B: two mobs
    9101: (MAP_B, (1601.0, -185.0)),
    9102: (MAP_B, (1549.0, -247.0)),
}


def _pull(anchors=ANCHORS, overrides=None):
    """One pull engaging every anchor mob at its planned world spot, except
    those in ``overrides`` (npc_id -> world_pos actually engaged at)."""
    overrides = overrides or {}
    units = []
    for npc_id, (ui_map_id, world) in anchors.items():
        units.append(_unit(npc_id, overrides.get(npc_id, world), ui_map_id))
    return ActualPull(index=1, units=units)


class TestCollectMapBounds:
    def test_reads_bounds_from_map_change_and_ignores_junk(self):
        events = [
            Event(0.0, "MAP_CHANGE", ["2094", '"Ruby Life Pools"', "1868.750000",
                                      "1345.829956", "181.250000", "-602.083984"]),
            Event(1.0, "MAP_CHANGE", ["2095", '"Ruby Life Pools"', "1685", "1335", "75", "-450"]),
            Event(2.0, "MAP_CHANGE", ["2094", '"dup"', "0", "0", "0", "0"]),  # first wins
            Event(3.0, "MAP_CHANGE", ["7", '"degenerate"', "5", "5", "1", "0"]),
            Event(4.0, "MAP_CHANGE", ["x", '"junk"', "a", "b", "c", "d"]),
            Event(5.0, "ZONE_CHANGE", ["2521", '"Ruby Life Pools"', "8"]),
        ]
        bounds = collect_map_bounds(events)
        assert bounds == {
            2094: (1868.75, 1345.829956, 181.25, -602.083984),
            2095: (1685.0, 1335.0, 75.0, -450.0),
        }


class TestMapNormalized:
    def test_corners_map_to_canvas_extent_north_up(self):
        x0, x1, y0, y1 = BOUNDS[MAP_A]
        # north-west corner (max x = north, max y = west) -> canvas top-left
        assert _map_normalized(BOUNDS[MAP_A], x0, y0) == pytest.approx((0.0, 0.0))
        # south-east corner -> canvas bottom-right (y negative downward)
        assert _map_normalized(BOUNDS[MAP_A], x1, y1) == pytest.approx((CANVAS_W, -CANVAS_H))


class TestCalibrateMaps:
    def test_recovers_each_submaps_own_placement(self):
        result = calibrate_maps([_pull()], _dungeon(ANCHORS), BOUNDS)
        assert result.ok is True
        assert set(result.transforms) == {MAP_A, MAP_B}
        assert result.skipped == {}
        for ui_map_id, (s, tx, ty) in PLACEMENT.items():
            t = result.transforms[ui_map_id]
            assert t.scale == pytest.approx(s, rel=1e-6)
            assert (t.tx, t.ty) == pytest.approx((tx, ty), abs=1e-6)
            assert t.residual == pytest.approx(0.0, abs=1e-6)
        assert result.transforms[MAP_A].anchor_count == 5
        assert result.transforms[MAP_B].anchor_count == 2

    def test_summary_shape(self):
        summary = calibrate_maps([_pull()], _dungeon(ANCHORS), BOUNDS).summary()
        assert summary["ok"] is True
        assert summary["method"] == "per_uimap"
        assert summary["anchor_count"] == 7
        assert set(summary["maps"]) == {str(MAP_A), str(MAP_B)}
        assert summary["maps"][str(MAP_A)]["scale"] == pytest.approx(1.25)
        assert "uncalibrated_maps" not in summary

    def test_outlier_anchor_is_rejected_not_averaged_in(self):
        # one of map A's five anchors engaged 300 canvas units from its
        # planned dot (a patrolling mob pulled far from spawn)
        bad = {9003: (1500.0, -500.0)}
        result = calibrate_maps([_pull(overrides=bad)], _dungeon(ANCHORS), BOUNDS)
        t = result.transforms[MAP_A]
        assert t.anchor_count == 4
        assert t.scale == pytest.approx(PLACEMENT[MAP_A][0], rel=1e-6)
        assert t.residual < 1e-6

    def test_two_anchors_that_disagree_are_not_trusted(self):
        # map B only has two anchors; nudge one so the 3-parameter fit has
        # a real residual (2 points over-determine scale+translation)
        anchors = dict(ANCHORS)
        dungeon = _dungeon(anchors)
        pull = _pull(overrides={9102: (1549.0 + 60.0, -247.0 + 60.0)})
        result = calibrate_maps([pull], dungeon, BOUNDS)
        assert MAP_A in result.transforms
        assert MAP_B not in result.transforms
        assert "no consistent fit" in result.skipped[MAP_B]

    def test_submap_without_bounds_or_with_one_anchor_is_skipped(self):
        anchors = dict(ANCHORS)
        anchors[9201] = (3333, (10.0, 10.0))          # a map that never logged MAP_CHANGE
        del anchors[9102]                              # leaves map B with a single anchor
        result = calibrate_maps([_pull(anchors)], _dungeon(anchors), BOUNDS)
        assert result.ok is True
        assert set(result.transforms) == {MAP_A}
        assert result.skipped[3333].startswith("no MAP_CHANGE bounds")
        assert result.skipped[MAP_B] == "insufficient anchors (1)"

    def test_extra_anchor_rescues_single_anchor_submap(self):
        anchors = dict(ANCHORS)
        del anchors[9102]
        entrance_world = (1560.0, -200.0)
        extra = [(entrance_world, _canvas_for(MAP_B, *entrance_world), MAP_B)]
        result = calibrate_maps([_pull(anchors)], _dungeon(anchors), BOUNDS, extra_anchors=extra)
        assert MAP_B in result.transforms
        assert result.transforms[MAP_B].scale == pytest.approx(PLACEMENT[MAP_B][0], rel=1e-6)

    def test_no_positions_fails_safe(self):
        pull = ActualPull(index=1, units=[
            UnitEngagement(guid="Creature-0-1-1-1-9001-0001", npc_id=9001, name="",
                           first_ts=0.0, last_ts=1.0, died_at=1.0, first_pos=None),
        ])
        result = calibrate_maps([pull], _dungeon(ANCHORS), BOUNDS)
        assert result.ok is False
        assert result.reason == "no position data (advanced combat logging required)"
        assert result.summary()["ok"] is False

    def test_nothing_calibrates_reports_insufficient_anchors(self):
        anchors = {9001: ANCHORS[9001], 9101: ANCHORS[9101]}  # one per map
        result = calibrate_maps([_pull(anchors)], _dungeon(anchors), BOUNDS)
        assert result.ok is False
        assert result.reason == "insufficient anchors"
        assert MIN_MAP_ANCHORS == 2

    def test_summoned_adds_listed_once_by_mdt_are_not_anchors(self):
        # MDT has one clone of npc 9003 but the run engaged three distinct
        # units of it (a summoned add) -- pairing any of them with the one
        # planned dot is meaningless, so it must not be an anchor.
        pull = _pull()
        pull.units.append(_unit(9003, (1400.0, -300.0), MAP_A, spawn="0002"))
        pull.units.append(_unit(9003, (1700.0, -100.0), MAP_A, spawn="0003"))
        result = calibrate_maps([pull], _dungeon(ANCHORS), BOUNDS)
        assert result.transforms[MAP_A].anchor_count == 4
        assert result.transforms[MAP_A].residual < 1e-6


class TestEntranceAnchor:
    def test_pairs_entrance_poi_with_earliest_early_sample(self):
        dungeon = _dungeon(ANCHORS, entrance=(120.0, -514.0))
        samples = {
            "p1": [[12.0, 1500.0, -100.0, MAP_A], [30.0, 1505.0, -110.0, MAP_A]],
            "p2": [[8.9, 1405.0, -164.0, MAP_B], [11.0, 1410.0, -160.0, MAP_B]],
        }
        assert entrance_anchor(dungeon, samples) == ((1405.0, -164.0), (120.0, -514.0), MAP_B)

    def test_none_without_poi_or_without_early_samples(self):
        samples = {"p1": [[45.0, 1500.0, -100.0, MAP_A]]}  # past ENTRANCE_WINDOW_S
        assert entrance_anchor(_dungeon(ANCHORS, entrance=(1.0, -1.0)), samples) is None
        assert entrance_anchor(_dungeon(ANCHORS), {"p1": [[1.0, 1.0, 1.0, MAP_A]]}) is None


class TestBuildMapReportPerMap:
    def test_paths_use_each_samples_own_submap_and_skip_unknown_maps(self):
        dungeon = _dungeon(ANCHORS)
        samples = {
            "p1": [
                [1.0, 1553.0, -145.0, MAP_A],
                [3.0, 1601.0, -185.0, MAP_B],
                [5.0, 1.0, 1.0, 4444],   # a sub-map that never calibrated
                [7.0, 1571.0, -342.0],   # legacy 3-field sample: no map -> skipped
            ],
        }
        report = build_map_report(dungeon, None, None, [_pull()], samples, {"p1": "Player"},
                                  [], 0.0, map_bounds=BOUNDS)
        assert report["calibration"]["ok"] is True
        (player,) = report["players"]
        assert [pt[0] for pt in player["path"]] == [1.0, 3.0]
        assert tuple(player["path"][0][1:]) == pytest.approx(_canvas_for(MAP_A, 1553.0, -145.0), abs=0.05)
        assert tuple(player["path"][1][1:]) == pytest.approx(_canvas_for(MAP_B, 1601.0, -185.0), abs=0.05)

    def test_path_is_split_into_segments_around_dropped_samples(self):
        dungeon = _dungeon(ANCHORS)
        samples = {"p1": [
            [1.0, 1553.0, -145.0, MAP_A],
            [3.0, 1571.0, -342.0, MAP_A],
            [5.0, 1.0, 1.0, 4444],        # dropped: uncalibrated sub-map
            [7.0, 1.0, 1.0, 4444],
            [9.0, 1601.0, -185.0, MAP_B],
            [11.0, 1549.0, -247.0, MAP_B],
        ]}
        report = build_map_report(dungeon, None, None, [_pull()], samples, {"p1": "Player"},
                                  [], 0.0, map_bounds=BOUNDS)
        (player,) = report["players"]
        assert [[pt[0] for pt in seg] for seg in player["segments"]] == [[1.0, 3.0], [9.0, 11.0]]
        assert [pt[0] for pt in player["path"]] == [1.0, 3.0, 9.0, 11.0]

    def test_no_bounds_falls_back_to_legacy_global_fit(self):
        # Without MAP_CHANGE data the old single-similarity path still runs
        # (and, for this composite dungeon, refuses -- which is the point).
        report = build_map_report(_dungeon(ANCHORS), None, None, [_pull()], {}, {}, [], 0.0)
        assert "method" not in report["calibration"]
        assert report["calibration"]["ok"] is False


class TestCloneIndexFromMdtKey:
    LUA = textwrap.dedent("""
    local dungeonIndex = 42
    MDT.dungeonList[dungeonIndex] = "Gappy"
    MDT.dungeonEnemies[dungeonIndex] = {
      [1] = {
        ["name"] = "Deepstone Earthshaper", ["id"] = 187969, ["count"] = 5,
        ["clones"] = {
          [1] = { ["x"] = 125.6, ["y"] = -366, ["g"] = 1, ["sublevel"] = 1 },
          [2] = { ["x"] = 104.4, ["y"] = -347.5, ["g"] = 2, ["sublevel"] = 1 },
          [4] = { ["x"] = 62.8, ["y"] = -269.7, ["g"] = 6, ["sublevel"] = 1 },
          [6] = { ["x"] = 124.3, ["y"] = -144.4, ["g"] = 12, ["sublevel"] = 1 },
        },
      },
    }
    """)

    def test_extract_keeps_clones_past_a_gap_and_records_mdt_key(self, tmp_path):
        path = tmp_path / "Gappy.lua"
        path.write_text(self.LUA, encoding="utf-8")
        data = extract_dungeon_file(path)
        (enemy,) = data["enemies"]
        assert [c["idx"] for c in enemy["clones"]] == [1, 2, 4, 6]
        assert [c["x"] for c in enemy["clones"]] == [125.6, 104.4, 62.8, 124.3]

    def test_from_dict_reads_idx_and_tolerates_its_absence(self):
        d = {"dungeon_idx": 1, "name": "x", "enemies": [{
            "enemy_idx": 1, "id": 5, "name": "m", "count": 1,
            "clones": [{"x": 1, "y": 2, "idx": 4}, {"x": 3, "y": 4}],
        }]}
        dungeon = DungeonData.from_dict(d)
        assert [c.idx for c in dungeon.enemies[0].clones] == [4, None]

    def test_plan_geometry_matches_route_pulls_by_mdt_key_not_position(self):
        dungeon = DungeonData(dungeon_idx=1, name="x", enemies=[Enemy(
            enemy_idx=1, npc_id=5, name="m", count=1, clones=[
                EnemyClone(x=1.0, y=-1.0, sublevel=1, idx=1),
                EnemyClone(x=2.0, y=-2.0, sublevel=1, idx=4),  # 2nd in list, key 4
            ])])
        route = Route(name="r", dungeon_idx=1, week=None, difficulty=None,
                      pulls=[Pull(index=7, enemies={1: [4]})])
        geometry = plan_geometry(dungeon, route)
        by_key = {e["x"]: e["plan_pull"] for e in geometry["enemies"]}
        assert by_key == {1.0: None, 2.0: 7}


class TestEngagementPositions:
    def _line(self, name, src, dst, adv_guid, x, y, ui_map):
        # 8 base params, 3 spell params, then a 19-field advanced block
        # laid out as combatlog.events.advanced_info expects.
        adv = [adv_guid, "0000000000000000", "100", "100", "0", "0", "0", "0",
               "0", "0", "0", "0", "0", "0", f"{x}", f"{y}", str(ui_map), "1.0", "90"]
        return Event(0.0, name, list(src) + list(dst) + ["1", '"Spell"', "0x1"] + adv + ["5", "5"])

    def test_position_is_taken_from_first_line_carrying_the_enemys_own_block(self):
        player = ("Player-1-AAAA", '"Tank"', "0x511", "0x0")
        mob = ("Creature-0-1-1-1-777-0001", '"Boss"', "0xa48", "0x0")
        events = [
            # opening hit: the advanced block is the PLAYER's -> no mob position yet
            self._line("SPELL_DAMAGE", player, mob, player[0], 1.0, 2.0, 2094),
            # the mob's own cast, 3 lines later: its position and map
            self._line("SPELL_CAST_SUCCESS", mob, player, mob[0], 1553.5, -145.25, 2095),
            self._line("SPELL_CAST_SUCCESS", mob, player, mob[0], 1600.0, -100.0, 2095),
        ]
        (eng,) = collect_engagements(events).values()
        assert eng.first_pos == (1553.5, -145.25)
        assert eng.first_map_id == 2095
