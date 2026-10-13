"""Who pulled the mobs that weren't on the route.

Each engagement records the group member on the other end of its first
interaction and which way it went: the group hit the enemy first
("group"), or the enemy came at the group first ("enemy" -- a body pull,
a linked pack, a patrol). Pets are credited to their owner. The route
comparison carries that onto its off-route entries, and the reports show
it next to them.
"""

from conftest import DPS1, HEALER, HOSTILE, TANK, LogBuilder

from postmortem.analysis.compare import compare_route
from postmortem.analysis.pulls import (
    OPENED_BY_ENEMY,
    OPENED_BY_GROUP,
    ActualPull,
    UnitEngagement,
    collect_engagements,
)
from postmortem.combatlog.parser import iter_events
from postmortem.mdt.dungeon_data import DungeonData, Enemy, EnemyClone
from postmortem.mdt.route import Pull, Route
from postmortem.report.text import render_text

PET = "Pet-0-1465-2830-12345-165189-0100000001"
PET_FLAGS = 0x1111  # group pet: mine, friendly, player-controlled


def _engagements(b: LogBuilder):
    b.lines.sort(key=lambda line: line.split("  ", 1)[0])
    return collect_engagements(iter_events(b.lines))


class TestOpener:
    def test_player_hitting_first_is_the_opener(self):
        b = LogBuilder()
        mob = b.npc_guid(1001, "0001")
        b.player_damage(10, DPS1, mob, "Trash", 133, "Fireball", 1000)
        b.npc_damage(11, mob, "Trash", TANK, 1, "Bite", 1000)
        b.player_damage(12, TANK, mob, "Trash", 31935, "Avenger's Shield", 1000)
        (eng,) = _engagements(b).values()
        assert eng.opener_name == DPS1[1]
        assert eng.opener_guid == DPS1[0]
        assert eng.opened_by == OPENED_BY_GROUP

    def test_combatant_info_does_not_name_the_player(self):
        # COMBATANT_INFO also opens with the player's GUID, then a faction
        # number: a real log credited 14 of 17 engagements to "1"
        b = LogBuilder()
        b.combatant(0.5, DPS1)
        mob = b.npc_guid(1001, "0001")
        b.player_damage(10, DPS1, mob, "Trash", 133, "Fireball", 1000)
        (eng,) = _engagements(b).values()
        assert eng.opener_name == DPS1[1]

    def test_enemy_acting_first_is_an_aggro_onto_its_target(self):
        b = LogBuilder()
        mob = b.npc_guid(1001, "0001")
        b.npc_damage(10, mob, "Patrol", HEALER, 1, "Bite", 1000)
        b.player_damage(11, TANK, mob, "Patrol", 31935, "Avenger's Shield", 1000)
        (eng,) = _engagements(b).values()
        assert eng.opener_name == HEALER[1]
        assert eng.opened_by == OPENED_BY_ENEMY

    def test_summoned_pet_is_credited_to_its_owner(self):
        b = LogBuilder()
        mob = b.npc_guid(1001, "0001")
        b.raw(5, f'SPELL_SUMMON,{DPS1[0]},"{DPS1[1]}",{DPS1[2]:#06x},0x0,'
                 f'{PET},"Fluffy",0xa28,0x0,883,"Call Pet 1",0x1')
        b.spell_damage(10, PET, "Fluffy", PET_FLAGS, mob, "Trash", HOSTILE,
                       16827, "Claw", 1000)
        (eng,) = _engagements(b).values()
        assert eng.opener_name == DPS1[1]
        assert eng.opened_by == OPENED_BY_GROUP

    def test_pet_owner_from_the_advanced_block(self):
        # no SPELL_SUMMON (pet was out before logging started); the pet's
        # own melee line carries its owner in the advanced block
        b = LogBuilder()
        mob = b.npc_guid(1001, "0001")
        b.player_damage(1, DPS1, b.npc_guid(1002, "0002"), "Elsewhere", 133, "Fireball", 1)
        b.swing_damage(10, PET, "Fluffy", PET_FLAGS, mob, "Trash", HOSTILE, 1000)
        b.lines[-1] = b.lines[-1].replace(
            f"{PET},0000000000000000,", f"{PET},{DPS1[0]},", 1)
        engs = _engagements(b)
        assert engs[mob].opener_name == DPS1[1]

    def test_unknown_owner_falls_back_to_the_pet(self):
        b = LogBuilder()
        mob = b.npc_guid(1001, "0001")
        b.spell_damage(10, PET, "Fluffy", PET_FLAGS, mob, "Trash", HOSTILE,
                       16827, "Claw", 1000)
        (eng,) = _engagements(b).values()
        assert eng.opener_name == "Fluffy"

    def test_a_reset_mob_gets_a_fresh_opener(self):
        b = LogBuilder()
        mob = b.npc_guid(1001, "0001")
        b.player_damage(10, DPS1, mob, "Runner", 133, "Fireball", 1000)
        b.npc_damage(100, mob, "Runner", TANK, 1, "Bite", 1000)
        first, second = _engagements(b).values()
        assert (first.opener_name, first.opened_by) == (DPS1[1], OPENED_BY_GROUP)
        assert (second.opener_name, second.opened_by) == (TANK[1], OPENED_BY_ENEMY)


# --- route comparison ------------------------------------------------------

OGRE, GOBLIN = 8001, 8002


def _dungeon() -> DungeonData:
    clones = lambda n: [EnemyClone(x=float(i), y=0.0, sublevel=1, idx=i + 1) for i in range(n)]
    return DungeonData(
        dungeon_idx=3, name="t", total_count={"normal": 100},
        enemies=[
            Enemy(enemy_idx=1, npc_id=OGRE, name="Ogre", count=4, clones=clones(2)),
            Enemy(enemy_idx=2, npc_id=GOBLIN, name="Goblin", count=2, clones=clones(3)),
        ],
    )


def _unit(npc_id, spawn, t, opener, how):
    return UnitEngagement(
        guid=f"Creature-0-1-1-1-{npc_id}-{spawn:04X}", npc_id=npc_id, name="",
        first_ts=t, last_ts=t + 5, died_at=t + 5,
        opener_guid=opener, opener_name=opener, opened_by=how,
    )


class TestComparison:
    def test_off_route_entry_says_who_pulled_it(self):
        route = Route(name="t", dungeon_idx=3, week=None, difficulty=None,
                      pulls=[Pull(index=1, enemies={1: [1, 2]})])
        pull = ActualPull(index=1, units=[
            _unit(OGRE, 1, 0.0, "Tank", OPENED_BY_GROUP),
            _unit(OGRE, 2, 0.1, "Tank", OPENED_BY_GROUP),
            _unit(GOBLIN, 3, 4.0, "Tank", OPENED_BY_ENEMY),
            _unit(GOBLIN, 4, 4.2, "Hunter", OPENED_BY_GROUP),
        ])
        (row,) = compare_route(route, [pull], _dungeon()).summary(_dungeon())["pulls"]
        (entry,) = row["off_route"]
        assert entry["npc_id"] == GOBLIN and entry["n"] == 2
        assert entry["pulled_by"] == [
            {"name": "Hunter", "how": "group", "n": 1},
            {"name": "Tank", "how": "enemy", "n": 1},
        ]

    def test_partly_planned_npc_blames_the_units_engaged_last(self):
        # the route plans one Goblin; the pack's Goblin is opened by the
        # tank, two more walk in afterwards
        route = Route(name="t", dungeon_idx=3, week=None, difficulty=None,
                      pulls=[Pull(index=1, enemies={2: [1]})])
        pull = ActualPull(index=1, units=[
            _unit(GOBLIN, 1, 0.0, "Tank", OPENED_BY_GROUP),
            _unit(GOBLIN, 2, 6.0, "Mage", OPENED_BY_ENEMY),
            _unit(GOBLIN, 3, 6.1, "Mage", OPENED_BY_ENEMY),
        ])
        (m,) = compare_route(route, [pull], _dungeon()).matches
        assert m.off_route == {GOBLIN: 2}
        (row,) = compare_route(route, [pull], _dungeon()).summary(_dungeon())["pulls"]
        assert row["off_route"][0]["pulled_by"] == [{"name": "Mage", "how": "enemy", "n": 2}]

    def test_on_route_entries_carry_no_opener(self):
        route = Route(name="t", dungeon_idx=3, week=None, difficulty=None,
                      pulls=[Pull(index=1, enemies={1: [1, 2]})])
        pull = ActualPull(index=1, units=[
            _unit(OGRE, 1, 0.0, "Tank", OPENED_BY_GROUP),
            _unit(OGRE, 2, 0.1, "Tank", OPENED_BY_GROUP),
        ])
        (row,) = compare_route(route, [pull], _dungeon()).summary(_dungeon())["pulls"]
        assert row["off_route"] == []
        assert all("pulled_by" not in e for e in row["matched"]["1"])


class TestEndToEnd:
    def test_text_report_names_who_pulled_the_off_route_pack(self, dungeon_data_file):
        from conftest import ROUTE_PRESET, build_run_log

        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.segmenter import segment_runs
        from postmortem.mdt.dungeon_data import DungeonDataStore

        (run,) = list(segment_runs(iter_events(build_run_log().lines)))
        report = analyze_run(run, route=Route.from_preset(ROUTE_PRESET),
                             store=DungeonDataStore.load(dungeon_data_file))
        (entry,) = [e for p in report["comparison"]["pulls"] for e in p["off_route"]]
        # conftest's run: the mage opens on the Shadeling at t=61
        assert entry["pulled_by"] == [{"name": DPS1[1], "how": "group", "n": 1}]
        assert f"OFF-ROUTE: 1x Shadeling (pulled by {DPS1[1]})" in render_text(report)

    def test_text_wording_for_an_aggro_and_a_mix(self):
        from postmortem.report.text import _npcs

        assert _npcs([{"npc_id": 1, "n": 1, "name": "Rat",
                       "pulled_by": [{"name": "Tank", "how": "enemy", "n": 1}]}]) \
            == "1x Rat (aggroed on Tank)"
        assert _npcs([{"npc_id": 1, "n": 3, "name": "Rat",
                       "pulled_by": [{"name": "Mage", "how": "group", "n": 2},
                                     {"name": None, "how": "enemy", "n": 1}]}]) \
            == "3x Rat (2x pulled by Mage; 1x aggroed on ?)"

    def test_html_report_names_who_pulled_the_off_route_pack(self, dungeon_data_file):
        import json
        import shutil

        import pytest
        from conftest import ROUTE_PRESET, build_run_log
        from test_report_escaping import _extract_script, _run

        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.segmenter import segment_runs
        from postmortem.mdt.dungeon_data import DungeonDataStore
        from postmortem.report.html import render_html

        if shutil.which("node") is None:
            pytest.skip("needs node to run the page's own JS")
        (run,) = list(segment_runs(iter_events(build_run_log().lines)))
        report = analyze_run(run, route=Route.from_preset(ROUTE_PRESET),
                             store=DungeonDataStore.load(dungeon_data_file))
        out = _run(_extract_script(render_html(report)), "report-data",
                   json.dumps(report), extra="render();")
        cell = out[out.index("off-route: 1x Shadeling"):][:400]
        assert "pulled by " in cell and ">Zappyboi<" in cell, cell


# --- pulls started by someone other than the tank --------------------------

from postmortem.analysis.pull_openers import (
    TANK_GRACE_S,
    non_tank_pulls_summary,
    pull_openers,
)


def _touched(unit, **touches):
    unit.first_touch = dict(touches)
    return unit


class TestNonTankPulls:
    def test_tank_opening_is_not_flagged(self):
        pull = ActualPull(index=1, units=[
            _touched(_unit(OGRE, 1, 10.0, "Tank", OPENED_BY_GROUP), Tank=10.0, Mage=10.2)])
        (e,) = pull_openers([pull], "Tank")
        assert e["opener"] == "Tank" and not e["flagged"]

    def test_dps_opening_well_before_the_tank_is_flagged(self):
        pull = ActualPull(index=1, units=[
            _touched(_unit(OGRE, 1, 10.0, "Mage", OPENED_BY_GROUP), Mage=10.0, Tank=12.0)])
        (e,) = pull_openers([pull], "Tank")
        assert e["flagged"] and e["tank_lead_s"] == 2.0

    def test_dps_landing_with_the_tank_is_not_flagged(self):
        t = 10.0 + TANK_GRACE_S
        pull = ActualPull(index=1, units=[
            _touched(_unit(OGRE, 1, 10.0, "Mage", OPENED_BY_GROUP), Mage=10.0),
            _touched(_unit(OGRE, 2, t, "Tank", OPENED_BY_GROUP), Tank=t)])
        (e,) = pull_openers([pull], "Tank")
        assert e["opener"] == "Mage" and not e["flagged"]

    def test_mob_running_at_a_dps_first_is_a_flagged_aggro(self):
        pull = ActualPull(index=1, units=[
            _touched(_unit(OGRE, 1, 10.0, "Priest", OPENED_BY_ENEMY), Priest=10.0, Tank=13.0)])
        (e,) = pull_openers([pull], "Tank")
        assert e["flagged"] and e["how"] == OPENED_BY_ENEMY

    def test_mob_running_at_the_tank_is_fine(self):
        pull = ActualPull(index=1, units=[
            _touched(_unit(OGRE, 1, 10.0, "Tank", OPENED_BY_ENEMY), Tank=10.0)])
        (e,) = pull_openers([pull], "Tank")
        assert not e["flagged"]

    def test_tank_never_touching_the_pull_is_flagged(self):
        pull = ActualPull(index=1, units=[
            _touched(_unit(OGRE, 1, 10.0, "Mage", OPENED_BY_GROUP), Mage=10.0)])
        (e,) = pull_openers([pull], "Tank")
        assert e["flagged"] and e["tank_lead_s"] is None

    def test_summary_counts_per_player(self):
        entries = [
            {"pull": 1, "start_ts": 0, "boss": None, "opener": "Mage", "how": "group",
             "tank_lead_s": 2.0, "flagged": True},
            {"pull": 2, "start_ts": 0, "boss": None, "opener": "Mage", "how": "enemy",
             "tank_lead_s": 1.0, "flagged": True},
            {"pull": 3, "start_ts": 0, "boss": None, "opener": "Tank", "how": "group",
             "tank_lead_s": None, "flagged": False},
        ]
        s = non_tank_pulls_summary(entries, "Tank")
        assert s["pull_count"] == 3 and [e["pull"] for e in s["flagged"]] == [1, 2]
        assert s["by_player"] == [{"name": "Mage", "pulled": 1, "aggroed": 1}]

    def test_early_units_say_who_pulled_them(self):
        route = Route(name="t", dungeon_idx=3, week=None, difficulty=None,
                      pulls=[Pull(index=1, enemies={1: [1, 2]}), Pull(index=2, enemies={2: [1, 2]}),
                             Pull(index=3, enemies={2: [3]})])
        actual = [
            ActualPull(index=1, units=[
                _unit(OGRE, 1, 0.0, "Tank", OPENED_BY_GROUP),
                _unit(OGRE, 2, 0.1, "Tank", OPENED_BY_GROUP),
                _unit(GOBLIN, 3, 3.0, "Rogue", OPENED_BY_ENEMY)]),
            ActualPull(index=2, units=[
                _unit(GOBLIN, 4, 30.0, "Tank", OPENED_BY_GROUP),
                _unit(GOBLIN, 5, 30.1, "Tank", OPENED_BY_GROUP)]),
        ]
        rows = compare_route(route, actual, _dungeon()).summary(_dungeon())["pulls"]
        (early,) = rows[0]["pulled_early"]
        assert early["pulled_by"] == [{"name": "Rogue", "how": "enemy", "n": 1}]

    def test_report_section_end_to_end(self, dungeon_data_file):
        from conftest import ROUTE_PRESET, build_run_log

        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.segmenter import segment_runs
        from postmortem.mdt.dungeon_data import DungeonDataStore

        (run,) = list(segment_runs(iter_events(build_run_log().lines)))
        report = analyze_run(run, route=Route.from_preset(ROUTE_PRESET),
                             store=DungeonDataStore.load(dungeon_data_file))
        ntp = report["non_tank_pulls"]
        # conftest's run: the mage opens every Felwyrm hit half a second
        # ahead of the tank (inside the grace), and the boss a full second
        assert ntp["tank"] == TANK[1]
        assert [e["pull"] for e in ntp["flagged"]] == [3]
        assert ntp["flagged"][0]["opener"] == DPS1[1]
        assert report["pulls"][0]["opener"]["flagged"] is False
        text = render_text(report)
        assert f"pulled by {DPS1[1]} (1.0s before the tank)" in text

    def test_html_section(self, dungeon_data_file):
        import json
        import shutil

        import pytest
        from conftest import ROUTE_PRESET, build_run_log
        from test_report_escaping import _extract_script, _run

        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.segmenter import segment_runs
        from postmortem.mdt.dungeon_data import DungeonDataStore
        from postmortem.report.html import render_html

        if shutil.which("node") is None:
            pytest.skip("needs node to run the page's own JS")
        (run,) = list(segment_runs(iter_events(build_run_log().lines)))
        report = analyze_run(run, route=Route.from_preset(ROUTE_PRESET),
                             store=DungeonDataStore.load(dungeon_data_file))
        out = _run(_extract_script(render_html(report)), "report-data",
                   json.dumps(report), extra="render();")
        assert "Pulls started by a non-tank (1 of 3)" in out
        assert "1.0s before the tank" in out

    def test_no_tank_no_section(self):
        from conftest import build_run_log

        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.segmenter import segment_runs

        b = build_run_log()
        # drop COMBATANT_INFO: no specs, so no tank to measure against
        b.lines = [line for line in b.lines if "COMBATANT_INFO" not in line]
        (run,) = list(segment_runs(iter_events(b.lines)))
        report = analyze_run(run)
        assert "non_tank_pulls" not in report
        assert all("opener" not in p for p in report["pulls"])
