"""Pull-boundary and route-matcher behaviour pinned down by the real
reports of 2026-09-03..06 (see the module docstrings of analysis/pulls.py
and analysis/compare.py):

* a group that runs straight from one dead pack to the next (gaps of
  1.6-4.4s) must NOT have a 13-pull route fused into 4 "pulls";
* a bystander clipped once and never fought is not an engaged enemy;
* the season affix NPC is not part of any pull;
* two planned packs taken whole in one go are "chained", not "early";
* units of a planned npc beyond what the route (and MDT's dots) account
  for are spawns ("extra"), not off-route -- "off-route: 15x Kystia
  Manaheart" on a boss pull was the real symptom;
* an npc engaged in greater numbers than MDT has dots for never decides
  which planned pack a pull was, nor spills as early/off-route;
* an unplanned boss is not a deviation;
* NPCs the dungeon data doesn't know are named from the combat log.
"""

from __future__ import annotations

import pytest

from postmortem.analysis.compare import compare_route
from postmortem.analysis.pulls import (
    AFFIX_NPC_IDS,
    DEFAULT_MIN_ENGAGEMENT_S,
    DEFAULT_PULL_GAP_S,
    ActualPull,
    UnitEngagement,
    detect_pulls,
)
from postmortem.combatlog.events import Event
from postmortem.mdt.dungeon_data import DungeonData, Enemy, EnemyClone
from postmortem.mdt.route import Pull, Route

PLAYER = ("Player-1-AAAA", '"Tank"', "0x511", "0x0")
HOSTILE_FLAGS = "0xa48"


def _mob(npc_id: int, spawn: str, name: str = "Mob"):
    return (f"Creature-0-1-1-1-{npc_id}-{spawn}", f'"{name}"', HOSTILE_FLAGS, "0x0")


def _hit(t: float, mob) -> Event:
    """A player hitting ``mob``: 8 base params + spell params (no advanced block)."""
    return Event(t, "SPELL_DAMAGE", list(PLAYER) + list(mob) + ["1", '"Spell"', "0x1", "100"])


def _died(t: float, mob) -> Event:
    return Event(t, "UNIT_DIED", ["0000000000000000", "nil", "0x80000000", "0x80000000"] + list(mob))


def _fight(t0: float, t1: float, mob) -> list[Event]:
    """Engage ``mob`` at t0, keep hitting it, kill it at t1."""
    return [_hit(t0, mob), _hit((t0 + t1) / 2, mob), _died(t1, mob)]


class TestPullBoundaries:
    def test_defaults(self):
        assert DEFAULT_PULL_GAP_S == 1.5
        assert DEFAULT_MIN_ENGAGEMENT_S == 1.0

    def test_packs_chained_with_a_short_quiet_gap_are_separate_pulls(self):
        # pack A dies at 40, pack B is first hit at 42.7 (the real Ruby Life
        # Pools rhythm) -- separate pulls, where a 5s window merged them
        a1, a2 = _mob(101, "0001"), _mob(101, "0002")
        b1 = _mob(102, "0003")
        events = _fight(10, 38, a1) + _fight(11, 40, a2) + _fight(42.7, 70, b1)
        events.sort(key=lambda e: e.ts)
        pulls = detect_pulls(events)
        assert [sorted(u.guid for u in p.units) for p in pulls] == [
            sorted([a1[0], a2[0]]), [b1[0]],
        ]

    def test_overlapping_engagements_always_merge(self):
        # pack B pulled while pack A's last mob is still alive: one pull
        a1 = _mob(101, "0001")
        b1 = _mob(102, "0003")
        events = _fight(10, 40, a1) + _fight(39, 70, b1)
        events.sort(key=lambda e: e.ts)
        assert len(detect_pulls(events)) == 1

    def test_gap_under_the_threshold_merges(self):
        a1 = _mob(101, "0001")
        b1 = _mob(102, "0003")
        events = _fight(10, 40, a1) + _fight(41.0, 70, b1)
        events.sort(key=lambda e: e.ts)
        assert len(detect_pulls(events)) == 1
        assert len(detect_pulls(events, gap_seconds=0.5)) == 2

    def test_bystander_hit_once_and_never_killed_is_not_engaged(self):
        a1 = _mob(101, "0001")
        snitch = _mob(999, "0009", "Row Snitch")
        events = _fight(10, 40, a1) + [_hit(50.0, snitch)]
        pulls = detect_pulls(events)
        assert [u.guid for p in pulls for u in p.units] == [a1[0]]
        # a killed enemy always counts, however brief
        events = _fight(10, 40, a1) + [_hit(50.0, snitch), _died(50.2, snitch)]
        assert len(detect_pulls(events)) == 2

    def test_affix_npc_is_never_an_engaged_enemy(self):
        (affix_id,) = [i for i in AFFIX_NPC_IDS if i == 230937]
        xal = _mob(affix_id, "0007", "Xal'atath")
        a1 = _mob(101, "0001")
        events = _fight(10, 40, a1) + [_hit(20, xal), _hit(24, xal)]
        events.sort(key=lambda e: e.ts)
        (pull,) = detect_pulls(events)
        assert [u.guid for u in pull.units] == [a1[0]]


# --- route matcher ---------------------------------------------------------

OGRE, GOBLIN, WHELP, BOSS, IMAGE, CRITTER = 8001, 8002, 8003, 8004, 8005, 8006
UNKNOWN = 990009  # not in dungeon data


def _clones(n: int) -> list[EnemyClone]:
    return [EnemyClone(x=float(i), y=-float(i), sublevel=1, idx=i + 1) for i in range(n)]


@pytest.fixture()
def dungeon() -> DungeonData:
    return DungeonData(
        dungeon_idx=3,
        name="Matcher Test Dungeon",
        total_count={"normal": 100},
        enemies=[
            Enemy(enemy_idx=1, npc_id=OGRE, name="Ogre", count=4, clones=_clones(6)),
            Enemy(enemy_idx=2, npc_id=GOBLIN, name="Goblin", count=2, clones=_clones(3)),
            # MDT marks 2 whelps; the dungeon hatches many more mid-fight
            Enemy(enemy_idx=3, npc_id=WHELP, name="Whelp", count=1, clones=_clones(2)),
            Enemy(enemy_idx=4, npc_id=BOSS, name="Big Boss", count=0, is_boss=True, clones=_clones(1)),
            Enemy(enemy_idx=5, npc_id=IMAGE, name="Big Boss", count=0, clones=_clones(1)),
            Enemy(enemy_idx=6, npc_id=CRITTER, name="Critter", count=0, clones=_clones(1)),
        ],
    )


def _route(*pulls: dict[int, list[int]]) -> Route:
    return Route(name="t", dungeon_idx=3, week=None, difficulty=None,
                 pulls=[Pull(index=i, enemies=e) for i, e in enumerate(pulls, start=1)])


_spawn = [0]


def _unit(npc_id: int, name: str = "", boss: bool = False) -> UnitEngagement:
    _spawn[0] += 1
    return UnitEngagement(
        guid=f"Creature-0-1-1-1-{npc_id}-{_spawn[0]:04X}", npc_id=npc_id, name=name,
        first_ts=0.0, last_ts=1.0, died_at=1.0,
    )


def _pull(index: int, *npcs: int, boss: bool = False, names: dict[int, str] | None = None) -> ActualPull:
    names = names or {}
    return ActualPull(index=index, units=[_unit(n, names.get(n, "")) for n in npcs],
                      encounter_name="Big Boss" if boss else None)


class TestChainedPlanPulls:
    def test_two_whole_packs_in_one_pull_are_chained_not_early(self, dungeon):
        # plan: #1 = 2 Ogres, #2 = 1 Ogre + 1 Goblin, #3 = boss
        route = _route({1: [1, 2]}, {1: [3], 2: [1]}, {4: [1]})
        actual = [_pull(1, OGRE, OGRE, OGRE, GOBLIN), _pull(2, BOSS, boss=True)]
        comp = compare_route(route, actual, dungeon)
        m1, m2 = comp.matches
        assert m1.primary_plan_pull == 1
        assert m1.chained == [2]
        assert not m1.early and not m1.late and not m1.off_route
        assert m1.deviation_count == 0
        assert m1.match_confidence == 1.0
        assert comp.adherence_pct == 100.0
        assert comp.summary(dungeon)["pulls"][0]["chained"] == [2]

    def test_a_missed_straggler_does_not_break_the_chain(self, dungeon):
        route = _route({1: [1, 2]}, {1: [3], 2: [1, 2]}, {4: [1]})
        # plan #2's second Goblin is never engaged
        actual = [_pull(1, OGRE, OGRE, OGRE, GOBLIN), _pull(2, BOSS, boss=True)]
        comp = compare_route(route, actual, dungeon)
        assert comp.matches[0].chained == [2]
        assert comp.missed == {2: {GOBLIN: 1}}

    def test_a_pack_split_across_two_pulls_is_not_chained(self, dungeon):
        route = _route({1: [1, 2]}, {1: [3], 2: [1]}, {4: [1]})
        # the group takes plan #1 plus half of plan #2, then the rest of #2
        actual = [_pull(1, OGRE, OGRE, OGRE), _pull(2, GOBLIN), _pull(3, BOSS, boss=True)]
        comp = compare_route(route, actual, dungeon)
        m1, m2, _ = comp.matches
        assert m1.chained == []
        assert m1.early == {OGRE: 1}
        assert m2.primary_plan_pull == 2 and m2.deviation_count == 0

    def test_the_straggler_cursor_does_not_pin_later_packs(self, dungeon):
        # plan #1 leaves one Ogre never engaged; plan #3 (2 Ogres) and #4
        # (1 Ogre + Goblin) must still match cleanly afterwards instead of
        # every later Ogre being sucked back into #1's hole.
        route = _route({1: [1, 2]}, {2: [1]}, {1: [3, 4]}, {1: [5], 2: [2]})
        actual = [_pull(1, OGRE), _pull(2, GOBLIN), _pull(3, OGRE, OGRE), _pull(4, OGRE, GOBLIN)]
        comp = compare_route(route, actual, dungeon)
        assert [m.primary_plan_pull for m in comp.matches] == [1, 2, 3, 4]
        assert sum(m.deviation_count for m in comp.matches) == 0
        assert comp.missed == {1: {OGRE: 1}}


class TestExtraVersusOffRoute:
    def test_boss_images_are_extra_not_off_route(self, dungeon):
        route = _route({4: [1]})
        actual = [_pull(1, BOSS, *([IMAGE] * 15), boss=True)]
        (m,) = compare_route(route, actual, dungeon).matches
        assert m.extra == {IMAGE: 15}
        assert m.off_route == {}
        assert m.deviation_count == 0

    def test_unplanned_dots_of_a_planned_npc_are_off_route(self, dungeon):
        # 3 Goblin dots, route plans 1, the group kills all 3: the two the
        # route left out are a genuine off-route pull, not spawns.
        route = _route({1: [1, 2], 2: [1]})
        actual = [_pull(1, OGRE, OGRE, GOBLIN, GOBLIN, GOBLIN)]
        (m,) = compare_route(route, actual, dungeon).matches
        assert m.off_route == {GOBLIN: 2}
        assert m.extra == {}
        assert m.deviation_count == 2

    def test_more_units_than_dots_means_spawns_not_off_route(self, dungeon):
        # a fourth Goblin where MDT has three dots: the npc spawns, so
        # none of the surplus can be pinned on the route
        route = _route({1: [1, 2], 2: [1]})
        actual = [_pull(1, OGRE, OGRE, GOBLIN, GOBLIN, GOBLIN, GOBLIN)]
        (m,) = compare_route(route, actual, dungeon).matches
        assert m.off_route == {}
        assert m.extra == {GOBLIN: 3}
        assert m.deviation_count == 0

    def test_spawning_npc_never_counts_as_off_route_or_early(self, dungeon):
        # plan #1: 2 Ogres + 1 Whelp, plan #2: 1 Whelp + Goblin. MDT has 2
        # whelp dots; the group meets 6 -> whelps spawn. The 5 surplus
        # whelps in pull 1 must not be "early" (stealing #2's whelp quota
        # counts for nothing) nor "off-route".
        route = _route({1: [1, 2], 3: [1]}, {3: [2], 2: [1]})
        actual = [_pull(1, OGRE, OGRE, *([WHELP] * 6)), _pull(2, GOBLIN)]
        comp = compare_route(route, actual, dungeon)
        m1, m2 = comp.matches
        assert m1.primary_plan_pull == 1
        assert m1.deviation_count == 0
        assert m1.extra[WHELP] >= 4
        assert m2.primary_plan_pull == 2 and m2.deviation_count == 0
        assert comp.adherence_pct == 100.0

    def test_spawning_npc_does_not_decide_the_primary_pull(self, dungeon):
        # 10 whelps outnumber the 2 Ogres that actually identify the pack
        route = _route({1: [1, 2]}, {3: [1, 2], 2: [1]})
        actual = [_pull(1, *([WHELP] * 10), OGRE, OGRE), _pull(2, GOBLIN)]
        comp = compare_route(route, actual, dungeon)
        assert comp.matches[0].primary_plan_pull == 1
        assert comp.matches[1].primary_plan_pull == 2

    def test_unplanned_boss_is_not_a_deviation(self, dungeon):
        route = _route({1: [1, 2]})  # the route never wrote the boss down
        actual = [_pull(1, OGRE, OGRE), _pull(2, BOSS, boss=True)]
        comp = compare_route(route, actual, dungeon)
        m2 = comp.matches[1]
        assert m2.extra == {BOSS: 1} and m2.off_route == {}
        assert comp.adherence_pct == 100.0

    def test_summary_carries_extra_and_chained(self, dungeon):
        route = _route({1: [1, 2]}, {1: [3], 2: [1]})
        # one Critter (1 dot, unplanned): off-route; three Whelps where MDT
        # has 2 dots and the route none: spawns
        actual = [_pull(1, OGRE, OGRE, OGRE, GOBLIN, CRITTER, WHELP, WHELP, WHELP)]
        (row,) = compare_route(route, actual, dungeon).summary(dungeon)["pulls"]
        assert row["chained"] == [2]
        assert row["off_route"] == [{"npc_id": CRITTER, "n": 1, "name": "Critter"}]
        assert row["extra"] == [{"npc_id": WHELP, "n": 3, "name": "Whelp"}]
        assert row["deviations"] == 1


class TestNamesFromTheLog:
    def test_untracked_adds_are_named_from_the_combat_log(self, dungeon):
        route = _route({1: [1, 2]})
        actual = [_pull(1, OGRE, OGRE, UNKNOWN, names={UNKNOWN: "Hatchling"})]
        summary = compare_route(route, actual, dungeon).summary(dungeon)
        assert summary["pulls"][0]["untracked"] == [{"npc_id": UNKNOWN, "n": 1, "name": "Hatchling"}]

    def test_dungeon_data_name_still_wins(self, dungeon):
        route = _route({1: [1, 2]})
        actual = [_pull(1, OGRE, OGRE, names={OGRE: "Ogre (log spelling)"})]
        summary = compare_route(route, actual, dungeon).summary(dungeon)
        assert summary["pulls"][0]["matched"]["1"] == [{"npc_id": OGRE, "n": 2, "name": "Ogre"}]

    def test_no_name_anywhere_leaves_the_entry_unnamed(self, dungeon):
        route = _route({1: [1, 2]})
        actual = [_pull(1, OGRE, OGRE, UNKNOWN)]
        summary = compare_route(route, actual, dungeon).summary(dungeon)
        assert summary["pulls"][0]["untracked"] == [{"npc_id": UNKNOWN, "n": 1}]
