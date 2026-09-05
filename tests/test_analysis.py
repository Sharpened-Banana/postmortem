"""Pull detection, route comparison and stats on the synthetic run."""

import json
from pathlib import Path

import pytest
from conftest import (
    BOSS,
    DPS1,
    DUNGEON_DATA,
    DUSKBLADE,
    FELWYRM,
    HEALER,
    HOSTILE,
    LogBuilder,
    ROUTE_PRESET,
    SHADELING,
    SUMMONED,
    TANK,
    build_run_log,
)

from postmortem.analysis.avoidable import AvoidableData
from postmortem.analysis.compare import compare_route
from postmortem.analysis.interruptibility import (
    KNOWN_UNINTERRUPTIBLE_SPELL_IDS,
    InterruptibilityData,
)
from postmortem.analysis.pulls import ActualPull, UnitEngagement, detect_pulls
from postmortem.analysis.run_analyzer import (
    _cc_summary,
    _enemy_cast_summary,
    _unplanned_pulls_summary,
    analyze_run,
)
from postmortem.analysis.stats import RunStats, compute_stats
from postmortem.analysis.stealable import StealableData
from postmortem.combatlog.parser import iter_events
from postmortem.combatlog.segmenter import segment_runs
from postmortem.mdt.dungeon_data import DungeonData, DungeonDataStore, Enemy
from postmortem.mdt.route import Pull, Route


@pytest.fixture()
def run_segment():
    (run,) = list(segment_runs(iter_events(build_run_log().lines)))
    return run


@pytest.fixture()
def dungeon() -> DungeonData:
    return DungeonData.from_dict(DUNGEON_DATA["dungeons"]["160"])


@pytest.fixture()
def route() -> Route:
    return Route.from_preset(ROUTE_PRESET)


class TestDungeonDataMapExtras:
    """sublevels/map_textures/pois round-trip through the extracted-JSON
    shape (see extract.py) via DungeonData.from_dict."""

    def test_round_trips_from_extracted_dict(self):
        d = dict(DUNGEON_DATA["dungeons"]["160"])
        d["sublevels"] = {"1": "Murder Row"}
        d["map_textures"] = {"1": "Interface\\AddOns\\MythicDungeonTools\\MurderRow"}
        d["pois"] = {
            "1": [{"type": "dungeonEntrance", "x": 779.77, "y": -509.6, "size_mult": 1.5}]
        }
        dungeon = DungeonData.from_dict(d)
        assert dungeon.sublevels == {1: "Murder Row"}
        assert dungeon.map_textures[1].endswith("MurderRow")
        (poi,) = dungeon.pois[1]
        assert poi.type == "dungeonEntrance"
        assert poi.x == 779.77 and poi.y == -509.6
        assert poi.size_mult == 1.5

    def test_missing_extras_default_empty(self, dungeon):
        assert dungeon.sublevels == {}
        assert dungeon.map_textures == {}
        assert dungeon.pois == {}


class TestPullDetection:
    def test_three_pulls(self, run_segment):
        pulls = detect_pulls(run_segment.events)
        assert len(pulls) == 3
        assert pulls[0].npc_counter() == {FELWYRM: 2, DUSKBLADE: 1}
        assert pulls[1].npc_counter() == {DUSKBLADE: 1, SHADELING: 1, SUMMONED: 1}
        assert pulls[2].npc_counter() == {BOSS: 1}

    def test_boss_labeled(self, run_segment):
        pulls = detect_pulls(run_segment.events)
        assert pulls[2].encounter_name == "Big Boss"
        assert pulls[0].encounter_name is None

    def test_kill_tracking(self, run_segment):
        pulls = detect_pulls(run_segment.events)
        assert all(u.killed for u in pulls[0].units)


class TestRouteComparison:
    def test_deviations(self, run_segment, route, dungeon):
        pulls = detect_pulls(run_segment.events)
        comp = compare_route(route, pulls, dungeon)

        m1, m2, m3 = comp.matches
        # pull 1: primarily plan #1, with a Duskblade pulled early from plan #2
        assert m1.primary_plan_pull == 1
        assert m1.early == {DUSKBLADE: 1}
        assert not m1.off_route and not m1.late

        # pull 2: the remaining Duskblade from plan #2, one off-route
        # Shadeling, and one untracked summoned add
        assert m2.primary_plan_pull == 2
        assert m2.off_route == {SHADELING: 1}
        assert m2.untracked == {SUMMONED: 1}

        # pull 3: the boss, plan #4
        assert m3.primary_plan_pull == 4
        assert m3.deviation_count == 0

        # plan #3 (1x Felwyrm) was never pulled
        assert set(comp.missed) == {3}
        assert comp.missed[3] == {FELWYRM: 1}

    def test_adherence_and_forces(self, run_segment, route, dungeon):
        pulls = detect_pulls(run_segment.events)
        comp = compare_route(route, pulls, dungeon)
        # matched units: 2 felwyrm + 2 duskblade + boss = 5; deviating: early 1 + off-route 1
        assert comp.adherence_pct == pytest.approx(66.7, abs=0.1)
        # killed forces: 2*4 (felwyrm) + 2*6 (duskblade) + 2 (shadeling) + 0 (boss)
        assert comp.actual_forces == 22
        # planned: 3*4 (felwyrm) + 2*6 (duskblade) + 0 (boss)
        assert comp.plan_forces == 24
        assert comp.required_forces == 100

    def test_summary_names(self, run_segment, route, dungeon):
        pulls = detect_pulls(run_segment.events)
        summary = compare_route(route, pulls, dungeon).summary(dungeon)
        early = summary["pulls"][0]["pulled_early"]
        assert early == [{"npc_id": DUSKBLADE, "n": 1, "name": "Duskblade"}]

    def test_match_confidence(self, run_segment, route, dungeon):
        pulls = detect_pulls(run_segment.events)
        comp = compare_route(route, pulls, dungeon)
        m1, m2, m3 = comp.matches
        # m1 has 3 matched units total: 2x Felwyrm at its primary (plan #1)
        # plus the 1x Duskblade pulled early from plan #2 -> 2/3 at primary.
        assert m1.match_confidence == pytest.approx(2 / 3, abs=0.001)
        # m2 (leftover Duskblade at plan #2) and m3 (boss at plan #4) have
        # every matched unit at their primary pull.
        assert m2.match_confidence == 1.0
        assert m3.match_confidence == 1.0

        summary = comp.summary(dungeon)
        assert summary["pulls"][0]["match_confidence"] == pytest.approx(2 / 3, abs=0.001)

    def test_match_confidence_none_for_pure_off_route_pull(self):
        # A pull whose only unit isn't in the plan at all should have no
        # match_confidence — nothing matched, so there's no fraction to
        # report (and no divide-by-zero).
        dungeon = DungeonData(
            dungeon_idx=1,
            name="Test Dungeon",
            total_count={"normal": 10},
            enemies=[
                Enemy(enemy_idx=1, npc_id=700001, name="Planned Mob", count=1),
                Enemy(enemy_idx=2, npc_id=700002, name="Unplanned Mob", count=1),
            ],
        )
        route = Route(
            name="Test",
            dungeon_idx=1,
            week=None,
            difficulty=None,
            pulls=[Pull(index=1, enemies={1: [1]})],
        )
        actual = [
            ActualPull(
                index=1,
                units=[
                    UnitEngagement(
                        guid="Creature-0-1-1-1-700002-0001",
                        npc_id=700002,
                        name="Unplanned Mob",
                        first_ts=0.0,
                        last_ts=1.0,
                        died_at=1.0,
                    )
                ],
            )
        ]
        comp = compare_route(route, actual, dungeon)
        (m,) = comp.matches
        assert m.off_route == {700002: 1}
        assert m.primary_plan_pull is None
        assert m.match_confidence is None


class TestRouteComparisonDPAlignment:
    """The greedy per-unit matcher can misattribute a whole actual pull to
    the wrong planned pull when two planned pulls share an NPC type and the
    group runs them in a different order than planned. The windowed DP
    alignment in compare_route fixes this by weighing the whole actual/plan
    pull sequences together instead of consuming mobs unit-by-unit from
    wherever's cheapest at that instant."""

    OGRE = 800001
    GOBLIN = 800002

    @pytest.fixture()
    def dungeon(self) -> DungeonData:
        return DungeonData(
            dungeon_idx=2,
            name="Swap Test Dungeon",
            total_count={"normal": 10},
            enemies=[
                Enemy(enemy_idx=1, npc_id=self.OGRE, name="Ogre", count=1),
                Enemy(enemy_idx=2, npc_id=self.GOBLIN, name="Goblin", count=1),
            ],
        )

    @pytest.fixture()
    def route(self) -> Route:
        # plan pull A: 2x Ogre.  plan pull B: 1x Ogre + 1x Goblin.
        # A and B share the Ogre npc type -- that overlap is exactly what
        # trips up the greedy per-unit matcher (see the test below).
        return Route(
            name="Swap Test",
            dungeon_idx=2,
            week=None,
            difficulty=None,
            pulls=[
                Pull(index=1, enemies={1: [1, 2]}),       # A: 2x Ogre
                Pull(index=2, enemies={1: [3], 2: [1]}),  # B: 1x Ogre, 1x Goblin
            ],
        )

    def _unit(self, npc_id: int, spawn: str) -> UnitEngagement:
        return UnitEngagement(
            guid=f"Creature-0-1-1-1-{npc_id}-{spawn}",
            npc_id=npc_id,
            name="",
            first_ts=0.0,
            last_ts=1.0,
            died_at=1.0,
        )

    def test_greedy_alone_misattributes_the_swap(self, route, dungeon):
        # Sanity check on the documented bug: run the same per-unit search
        # the greedy pass uses (bypassing the DP entirely) against an
        # actual pull shaped like plan pull B (1x Ogre + 1x Goblin), and
        # confirm it pulls the Ogre from plan pull A instead of B -- a 1-1
        # tie that would make greedy's most_common() primary default to A,
        # even though the pull's composition is an exact match for B. This
        # is *why* compare_route needs the DP correction; the fix itself is
        # asserted in test_reordered_pulls_get_correct_primary below.
        from postmortem.analysis.compare import _find_plan_pull

        plan_order = [p.index for p in route.pulls]
        remaining = {p.index: p.npc_counter(dungeon) for p in route.pulls}

        pos_ogre = _find_plan_pull(self.OGRE, remaining, plan_order, 0)
        remaining[plan_order[pos_ogre]][self.OGRE] -= 1
        pos_goblin = _find_plan_pull(self.GOBLIN, remaining, plan_order, 0)

        assert pos_ogre == 0    # taken from plan pull A (wrong: this is B's pack)
        assert pos_goblin == 1  # taken from plan pull B

    def test_reordered_pulls_get_correct_primary(self, route, dungeon):
        # Actual order is reversed relative to the plan: the group kills
        # B's mobs (1x Ogre + 1x Goblin) first, then A's mobs (2x Ogre).
        pull_b_first = ActualPull(
            index=1,
            units=[self._unit(self.OGRE, "0001"), self._unit(self.GOBLIN, "0002")],
        )
        pull_a_second = ActualPull(
            index=2,
            units=[self._unit(self.OGRE, "0003"), self._unit(self.OGRE, "0004")],
        )

        comp = compare_route(route, [pull_b_first, pull_a_second], dungeon)
        m_first, m_second = comp.matches

        # The DP alignment recognizes pull_b_first's composition matches
        # plan pull B (index 2) exactly, and pull_a_second's matches plan
        # pull A (index 1) exactly -- a perfect, deviation-free assignment
        # the greedy per-unit matcher alone does not find (see the test
        # above).
        assert m_first.primary_plan_pull == 2
        assert m_second.primary_plan_pull == 1
        assert m_first.deviation_count == 0
        assert m_second.deviation_count == 0
        assert m_first.match_confidence == 1.0
        assert m_second.match_confidence == 1.0
        assert comp.missed == {}


class TestStats:
    @pytest.fixture()
    def stats(self, run_segment, dungeon):
        pulls = detect_pulls(run_segment.events)
        return compute_stats(run_segment.events, pulls, dungeon)

    def test_players_discovered(self, stats):
        names = {p.name for p in stats.players.values() if p.guid.startswith("Player-")}
        assert names == {"Thicktank-Area52", "Bigheals-Area52", "Zappyboi-Area52"}
        specs = {p.name: p.spec_id for p in stats.players.values()}
        assert specs["Thicktank-Area52"] == 66
        assert specs["Bigheals-Area52"] == 264

    def test_damage_totals(self, stats, run_segment):
        by_name = {p.name: p for p in stats.players.values()}
        dps = by_name["Zappyboi-Area52"]
        # 6x50000 (felwyrm) + 40000 + 90000 (duskblade A) + 30000 (shadeling)
        # + 10000 (add) + 90000 (duskblade B) + 7x80000 (boss)
        assert dps.damage_done == 6 * 50000 + 40000 + 90000 + 30000 + 10000 + 90000 + 7 * 80000
        tank = by_name["Thicktank-Area52"]
        assert tank.damage_overkill == 1000
        assert tank.interrupts == 1

    def test_healing(self, stats):
        healer = next(p for p in stats.players.values() if p.name == "Bigheals-Area52")
        assert healer.healing_done == 20000  # 25000 - 5000 overheal
        assert healer.overhealing == 5000

    def test_death_recap(self, stats):
        assert len(stats.deaths) == 1
        death = stats.deaths[0]
        assert death.player_name == "Bigheals-Area52"
        assert death.pull_index == 2
        assert death.killing_blow["spell"] == "Dark Bolt"
        assert death.killing_blow["spell_id"] == 1216538
        assert death.killing_blow["amount"] == 250000
        assert death.killing_blow["hp_after"] == 0

    def test_forces_timeline(self, stats):
        assert stats.forces_total == 22
        assert [f["forces"] for f in stats.forces_timeline] == [4, 8, 14, 20, 22]

    def test_lust_and_brez(self, stats):
        assert len(stats.lust_events) == 1
        assert stats.lust_events[0]["spell"] == "Bloodlust"
        assert len(stats.brez_events) == 1
        assert stats.brez_events[0]["spell"] == "Intercession"

    def test_kick_value_estimates(self, stats):
        by_name = {p.name: p for p in stats.players.values()}
        # TANK kicked Dark Bolt; it landed twice (150k, 250k) -> avg 200k
        tank = by_name["Thicktank-Area52"]
        assert tank.kick_prevented_damage == 200000
        assert tank.kick_prevented_healing == 0
        # DPS kicked an enemy heal observed once (80k), one unknown spell,
        # and a zero-damage debuff
        dps = by_name["Zappyboi-Area52"]
        assert dps.interrupts == 3
        assert dps.kick_prevented_healing == 80000
        assert dps.kick_prevented_damage == 0
        # HEALER kicked the pure-DoT Creeping Rot: 3 ticks x 15k, 1 application
        healer = by_name["Bigheals-Area52"]
        assert healer.kick_prevented_damage == 45000

        events = {e["interrupted_spell"]: e for e in stats.interrupt_events}
        dark_bolt = events["Dark Bolt"]
        assert dark_bolt["estimated_prevented_damage"] == 200000
        assert dark_bolt["prevented_dot_damage"] == 0
        assert dark_bolt["observed_casts"] == 2
        rot = events["Creeping Rot"]
        assert rot["estimated_prevented_damage"] == 45000
        assert rot["prevented_dot_damage"] == 45000
        assert rot["observed_casts"] == 1
        mending = events["Void Mending"]
        assert mending["estimated_prevented_healing"] == 80000
        assert mending["estimated_prevented_damage"] is None
        hex_kick = events["Nasty Hex"]
        assert hex_kick["estimated_prevented_damage"] is None
        assert hex_kick["prevented_debuff_applications"] == 1
        mystery = events["Mystery Bolt"]
        assert mystery["estimated_prevented_damage"] is None
        assert mystery["estimated_prevented_healing"] is None
        assert mystery["observed_casts"] == 0
        assert mystery["prevented_debuff_applications"] == 0

    def test_downtime(self, stats):
        gaps = {(w["after_pull"], w["before_pull"]): w["seconds"] for w in stats.downtime}
        assert (1, 2) in gaps and (2, 3) in gaps
        assert gaps[(1, 2)] == pytest.approx(20.0, abs=1.0)


class TestExpandedStats:
    @pytest.fixture()
    def stats(self, run_segment, dungeon):
        pulls = detect_pulls(run_segment.events)
        return compute_stats(run_segment.events, pulls, dungeon)

    def test_enemy_cast_outcomes(self, stats):
        outcomes = {v["name"]: v for v in stats.enemy_cast_outcomes.values()}
        assert outcomes["Dark Bolt"] == {"name": "Dark Bolt", "kicked": 1,
                                         "landed": 2, "expired": 0}
        assert outcomes["Creeping Rot"] == {"name": "Creeping Rot", "kicked": 1,
                                            "landed": 1, "expired": 0}
        assert outcomes["Void Mending"] == {"name": "Void Mending", "kicked": 1,
                                            "landed": 1, "expired": 1}

    def test_killing_blows(self, stats):
        by_name = {p.name: p for p in stats.players.values()}
        assert by_name["Zappyboi-Area52"].killing_blows == 4
        assert by_name["Thicktank-Area52"].killing_blows == 2

    def test_casts_and_consumables(self, stats):
        by_name = {p.name: p for p in stats.players.values()}
        assert by_name["Zappyboi-Area52"].casts_total == 4
        assert by_name["Zappyboi-Area52"].potions_used == 1
        assert by_name["Bigheals-Area52"].healthstones_used == 1
        kinds = [c["kind"] for c in stats.consumable_events]
        assert kinds == ["potion", "healthstone"]

    def test_purge_vs_dispel(self, stats):
        tank = next(p for p in stats.players.values() if p.name == "Thicktank-Area52")
        assert tank.purges == 1
        assert tank.dispels == 1
        kinds = {e["kind"] for e in stats.dispel_events}
        assert kinds == {"purge", "dispel"}

    def test_movement(self, stats):
        dps = next(p for p in stats.players.values() if p.name == "Zappyboi-Area52")
        assert dps.distance_traveled == pytest.approx(40.0, abs=0.1)
        samples = stats.position_samples[dps.guid]
        # unaffected by the throttle-bug fix: every cast in this fixture is
        # already >=2s apart, so all 4 casts still produce a sample -- the
        # bug (comparing an absolute ts against an already-relative stored
        # value) meant the throttle never actually applied before either,
        # so this specific count doesn't change, only each sample's shape.
        assert len(samples) == 4
        assert samples[0][1:3] == [100.0, -200.0]
        assert samples[2][1:3] == [110.0, -230.0]
        # 4th element is ui_map_id, now carried alongside each sample
        assert samples[0][3] == 2200

    def test_movement_throttles_samples_less_than_2s_apart(self, stats):
        # a death now also samples position (see stats.py's damage-taken
        # branch) -- the healer takes two hits 2.0s apart (t=64, t=66) at
        # the fixed (100,-200) advanced-block position build_run_log()
        # always uses for npc_damage(), plus a later cast sample at t=101.
        healer = next(p for p in stats.players.values() if p.name == "Bigheals-Area52")
        samples = stats.position_samples[healer.guid]
        times = [s[0] for s in samples]
        assert times == sorted(times)
        # no two samples closer together than the 2.0s throttle
        assert all(b - a >= 2.0 - 1e-6 for a, b in zip(times, times[1:]))

    def test_buff_uptime(self, stats):
        dps = next(p for p in stats.players.values() if p.name == "Zappyboi-Area52")
        (lust,) = [b for b in stats.buff_uptimes[dps.guid] if b["name"] == "Bloodlust"]
        assert lust["uptime_s"] == pytest.approx(30.0, abs=0.1)
        assert lust["uptime_pct"] == pytest.approx(21.4, abs=0.1)
        assert lust["applications"] == 1

    def test_encounters(self, stats):
        (enc,) = stats.encounters
        assert enc["name"] == "Big Boss"
        assert enc["kill"] is True
        assert enc["duration_s"] == pytest.approx(30.0, abs=0.1)

    def test_boss_damage(self, stats):
        dps = next(p for p in stats.players.values() if p.name == "Zappyboi-Area52")
        assert dps.damage_to_bosses == 7 * 80000


class TestEnemyCastSummary:
    """_enemy_cast_summary(): the interrupt_data-aware kick-efficiency
    filter (see run_analyzer.py). The three real spells in the synthetic
    run fixture (Dark Bolt, Creeping Rot, Void Mending) exercise the
    interrupt_data=None fallback path against the exact stats the rest of
    this test module already asserts (see test_enemy_cast_outcomes above);
    hand-built RunStats/enemy_cast_outcomes fixtures exercise the
    known-True/known-False branches in isolation, where an exact expected
    kick_efficiency_pct is easy to hand-compute.
    """

    @pytest.fixture()
    def stats(self, run_segment, dungeon):
        pulls = detect_pulls(run_segment.events)
        return compute_stats(run_segment.events, pulls, dungeon)

    def test_no_interrupt_data_matches_old_heuristic(self, stats):
        """Regression guard: interrupt_data=None (or omitted) must produce
        byte-for-byte the same spells/kick_efficiency_pct the pre-existing
        heuristic produced, plus only the new interruptible: None field on
        every spell. This is the path every existing caller that doesn't
        pass --interrupt-data still takes."""
        result = _enemy_cast_summary(stats)
        assert result["kick_efficiency_pct"] == 42.9
        by_name = {s["name"]: s for s in result["spells"]}
        assert len(by_name) == 3
        assert by_name["Dark Bolt"] == {
            "spell_id": 1216538, "name": "Dark Bolt", "kicked": 1,
            "got_through": 2, "expired": 0, "interruptible": None,
            "stealable": False,
        }
        assert by_name["Creeping Rot"] == {
            "spell_id": 777001, "name": "Creeping Rot", "kicked": 1,
            "got_through": 1, "expired": 0, "interruptible": None,
            "stealable": False,
        }
        assert by_name["Void Mending"] == {
            "spell_id": 888001, "name": "Void Mending", "kicked": 1,
            "got_through": 1, "expired": 1, "interruptible": None,
            "stealable": False,
        }

    def test_confirmed_uninterruptible_spell_excluded(self):
        """known is False (addon confirmed genuinely uninterruptible): the
        spell is dropped from `spells` entirely, and never touches the
        efficiency calculation -- excluded, not just zero-weighted."""
        stats = RunStats(enemy_cast_outcomes={
            111: {"name": "Unkickable Nuke", "kicked": 0, "landed": 5, "expired": 0},
            222: {"name": "Kickable Bolt", "kicked": 2, "landed": 2, "expired": 0},
        })
        interrupt_data = InterruptibilityData(spells={
            111: {"name": "Unkickable Nuke", "interruptible": False},
            222: {"name": "Kickable Bolt", "interruptible": True},
        })
        result = _enemy_cast_summary(stats, interrupt_data)
        names = [s["name"] for s in result["spells"]]
        assert names == ["Kickable Bolt"]
        # only Kickable Bolt (confirmed interruptible) feeds efficiency:
        # 100 * 2 kicked / (2 kicked + 2 landed) = 50.0 -- unaffected by
        # the excluded uninterruptible spell's 5 landed casts either way
        assert result["kick_efficiency_pct"] == 50.0

    def test_confirmed_uninterruptible_only_spell_yields_no_efficiency(self):
        """Same exclusion, but as the *only* tracked spell: confirms
        exclusion doesn't fabricate a 0%/100% efficiency out of nothing."""
        stats = RunStats(enemy_cast_outcomes={
            111: {"name": "Unkickable Nuke", "kicked": 0, "landed": 5, "expired": 0},
        })
        interrupt_data = InterruptibilityData(spells={
            111: {"name": "Unkickable Nuke", "interruptible": False},
        })
        result = _enemy_cast_summary(stats, interrupt_data)
        assert result["spells"] == []
        assert result["kick_efficiency_pct"] is None

    def test_confirmed_interruptible_zero_kicks_drags_efficiency_down(self):
        """known is True but kicked == 0 this run: the old heuristic made
        this spell invisible to the efficiency calc entirely (it only
        counted spells kicked at least once); now that it's confirmed
        kickable, it must appear and count as a missed kick."""
        stats = RunStats(enemy_cast_outcomes={
            333: {"name": "Missed Kick Spell", "kicked": 0, "landed": 4, "expired": 0},
        })
        interrupt_data = InterruptibilityData(spells={
            333: {"name": "Missed Kick Spell", "interruptible": True},
        })
        result = _enemy_cast_summary(stats, interrupt_data)
        assert len(result["spells"]) == 1
        assert result["spells"][0]["interruptible"] is True
        # 100 * 0 kicked / (0 kicked + 4 landed) = 0.0, not None/absent
        assert result["kick_efficiency_pct"] == 0.0

    def test_confirmed_interruptible_with_some_kicks(self):
        """known is True and it was actually kicked some this run: normal
        accounting, unaffected by the new confirmed-interruptible path."""
        stats = RunStats(enemy_cast_outcomes={
            444: {"name": "Sometimes Kicked", "kicked": 3, "landed": 7, "expired": 0},
        })
        interrupt_data = InterruptibilityData(spells={
            444: {"name": "Sometimes Kicked", "interruptible": True},
        })
        result = _enemy_cast_summary(stats, interrupt_data)
        assert result["spells"][0] == {
            "spell_id": 444, "name": "Sometimes Kicked", "kicked": 3,
            "got_through": 7, "expired": 0, "interruptible": True,
            "stealable": False,
        }
        # 100 * 3 / (3 + 7) = 30.0
        assert result["kick_efficiency_pct"] == 30.0

    def test_spell_never_seen_by_addon_falls_back_to_heuristic(self, stats):
        """known is None because the spell simply isn't in the loaded
        InterruptibilityData at all (as opposed to interrupt_data=None
        wholesale) -- Dark Bolt was kicked this run, so it must still
        appear (today's heuristic, unchanged) tagged interruptible: None."""
        interrupt_data = InterruptibilityData(spells={
            999999: {"name": "Some Other Spell", "interruptible": True},
        })
        result = _enemy_cast_summary(stats, interrupt_data)
        by_name = {s["name"]: s for s in result["spells"]}
        assert by_name["Dark Bolt"]["interruptible"] is None
        assert by_name["Dark Bolt"]["kicked"] == 1
        # unchanged from the interrupt_data=None case, since none of this
        # run's spells are in the loaded (unrelated) interrupt_data
        assert result["kick_efficiency_pct"] == 42.9


class TestChanneledEnemyCasts:
    """Channels log SPELL_CAST_SUCCESS up front with no SPELL_CAST_START
    and stay interruptible for their duration, so tracking only
    SPELL_CAST_START missed them entirely -- confirmed real (2026-09-05):
    57 of 730 landed interrupts across 13 runs (8%) were on channels
    absent from the kick table, and adding them moved one real run's kick
    efficiency from 11.5% to 21.5%.

    Instant casts reach the same "CAST_SUCCESS with no open hard cast"
    path and can never be interrupted, so only channels actually
    interrupted at least once are allowed into the report."""

    def _run(self, build):
        from conftest import DPS1, HOSTILE, LogBuilder
        b = LogBuilder()
        mob = b.npc_guid(555000, "0001")
        b.start(0)
        b.combatant(0.5, DPS1)
        build(b, mob, DPS1)
        b.end(200)
        segs = list(segment_runs(iter_events(b.text().splitlines())))
        return analyze_run(segs[-1])["enemy_casts"]

    def test_interrupted_channel_is_counted(self):
        def build(b, mob, dps):
            # three channels, one of them kicked
            b.npc_cast_success(10, mob, "Test Mob", 900700, "Test Channel")
            b.interrupt(11, dps, mob, "Test Mob", 2139, "Counterspell", 900700, "Test Channel")
            b.npc_cast_success(20, mob, "Test Mob", 900700, "Test Channel")
            b.npc_cast_success(30, mob, "Test Mob", 900700, "Test Channel")
        ec = self._run(build)
        entry = next(s for s in ec["spells"] if s["spell_id"] == 900700)
        assert entry["kicked"] == 1
        assert entry["got_through"] == 2   # the two never interrupted

    def test_uninterrupted_instant_casts_stay_out_of_the_report(self):
        def build(b, mob, dps):
            for t in (10, 12, 14, 16):
                b.npc_cast_success(t, mob, "Test Mob", 900701, "Instant Nuke")
        ec = self._run(build)
        assert all(s["spell_id"] != 900701 for s in ec["spells"])

    def test_hard_casts_are_unaffected(self):
        def build(b, mob, dps):
            b.npc_cast_start(10, mob, "Test Mob", 900702, "Hard Cast")
            b.npc_cast_success(12, mob, "Test Mob", 900702, "Hard Cast")
        ec = self._run(build)
        entry = next(s for s in ec["spells"] if s["spell_id"] == 900702)
        assert (entry["kicked"], entry["got_through"]) == (0, 1)


class TestEnemyCastSummaryKnownUninterruptible:
    """A weekly M+ affix mechanic (see KNOWN_UNINTERRUPTIBLE_SPELL_IDS) is
    excluded unconditionally -- confirmed real, 2026-09-05: "Xal'atath's
    Bargain: Devour" (465051) showed 0 kicked / 19-20 landed in two real
    reports the same week, cluttering the table with something no kick
    could ever have stopped."""

    AFFIX_SPELL_ID = next(iter(KNOWN_UNINTERRUPTIBLE_SPELL_IDS))

    def test_excluded_even_with_no_interrupt_data_at_all(self):
        stats = RunStats(enemy_cast_outcomes={
            self.AFFIX_SPELL_ID: {"name": "Affix Mechanic", "kicked": 0, "landed": 20, "expired": 0},
            111: {"name": "Normal Cast", "kicked": 1, "landed": 1, "expired": 0},
        })
        result = _enemy_cast_summary(stats)
        names = [s["name"] for s in result["spells"]]
        assert names == ["Normal Cast"]

    def test_excluded_even_if_interrupt_data_would_otherwise_confirm_it_true(self):
        # The hardcoded exclusion wins regardless of what any loaded
        # interrupt_data says -- this is a design fact, not a guess a
        # community file could contradict.
        stats = RunStats(enemy_cast_outcomes={
            self.AFFIX_SPELL_ID: {"name": "Affix Mechanic", "kicked": 0, "landed": 20, "expired": 0},
        })
        interrupt_data = InterruptibilityData(spells={
            self.AFFIX_SPELL_ID: {"name": "Affix Mechanic", "interruptible": True},
        })
        result = _enemy_cast_summary(stats, interrupt_data)
        assert result["spells"] == []

    def test_never_kicked_affix_cast_does_not_affect_efficiency(self):
        """Confirmed against a real report (2026-09-05, Murder Row): this
        spell was never counted toward kick_efficiency_pct even before
        this exclusion existed (kicked=0 already skipped the old
        heuristic's counting), so removing it from the table must not
        change the percentage -- only visual clutter goes away."""
        stats = RunStats(enemy_cast_outcomes={
            self.AFFIX_SPELL_ID: {"name": "Affix Mechanic", "kicked": 0, "landed": 20, "expired": 0},
            222: {"name": "Kicked Once", "kicked": 1, "landed": 1, "expired": 0},
        })
        without_affix = _enemy_cast_summary(RunStats(enemy_cast_outcomes={
            222: {"name": "Kicked Once", "kicked": 1, "landed": 1, "expired": 0},
        }))
        with_affix = _enemy_cast_summary(stats)
        assert with_affix["kick_efficiency_pct"] == without_affix["kick_efficiency_pct"] == 50.0


class TestEnemyCastSummaryStealable:
    """The ``stealable`` flag (see stealable.py) is independent of
    interrupt_data/kick efficiency -- purely a display tag with no effect
    on the efficiency math, unlike ``interruptible``."""

    def test_untagged_spell_is_stealable_false_not_none(self):
        stats = RunStats(enemy_cast_outcomes={
            111: {"name": "Some Cast", "kicked": 1, "landed": 1, "expired": 0},
        })
        result = _enemy_cast_summary(stats, stealable=StealableData(spells={
            222: {"name": "A Different Spell", "note": None},
        }))
        assert result["spells"][0]["stealable"] is False

    def test_tagged_spell_is_stealable_true(self):
        stats = RunStats(enemy_cast_outcomes={
            111: {"name": "Empowering Shield", "kicked": 0, "landed": 2, "expired": 0},
        })
        result = _enemy_cast_summary(stats, stealable=StealableData(spells={
            111: {"name": "Empowering Shield", "note": "big shield, steal it"},
        }))
        assert result["spells"][0]["stealable"] is True

    def test_no_stealable_data_defaults_false(self):
        stats = RunStats(enemy_cast_outcomes={
            111: {"name": "Some Cast", "kicked": 1, "landed": 1, "expired": 0},
        })
        result = _enemy_cast_summary(stats)
        assert result["spells"][0]["stealable"] is False

    def test_stealable_does_not_affect_kick_efficiency(self):
        """A stealable tag alone must not change whether a spell counts
        toward efficiency -- that's still purely interrupt_data's job."""
        stats = RunStats(enemy_cast_outcomes={
            111: {"name": "Never Kicked", "kicked": 0, "landed": 5, "expired": 0},
        })
        result = _enemy_cast_summary(stats, stealable=StealableData(spells={
            111: {"name": "Never Kicked", "note": None},
        }))
        assert result["spells"][0]["stealable"] is True
        assert result["spells"][0]["interruptible"] is None
        # never kicked, no interrupt ground truth -> still excluded from
        # the efficiency denominator despite being stealable
        assert result["kick_efficiency_pct"] is None

    def test_confirmed_uninterruptible_and_stealable_can_coexist(self):
        stats = RunStats(enemy_cast_outcomes={
            111: {"name": "Weird Combo", "kicked": 0, "landed": 3, "expired": 0},
        })
        interrupt_data = InterruptibilityData(spells={
            111: {"name": "Weird Combo", "interruptible": False},
        })
        stealable = StealableData(spells={111: {"name": "Weird Combo", "note": None}})
        result = _enemy_cast_summary(stats, interrupt_data, stealable)
        # confirmed-uninterruptible exclusion still wins -- stealable
        # tagging doesn't rescue a spell interrupt_data says to drop
        assert result["spells"] == []


class TestRealFormatAccuracy:
    """Fixes driven by comparing a real 2026-09 log against the in-game
    meter: spec detection had silently failed for every player, pet casts
    were inflating CPM, and absorbed damage wasn't counted as dealt."""

    @staticmethod
    def _analyze(b):
        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.parser import parse_file
        from postmortem.combatlog.segmenter import segment_runs
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(b.text())
        try:
            (seg,) = list(segment_runs(parse_file(path)))
            return analyze_run(seg)
        finally:
            os.unlink(path)

    @staticmethod
    def _real_combatant_info(guid: str, spec_id: int) -> str:
        # Verbatim shape of a real 2026-era COMBATANT_INFO: 25 numeric
        # fields (one more than the documented layout) and a talent block
        # that opens with "[(" -- the exact combination the old parser
        # misread (it looked for the first "(" param and landed inside the
        # second talent tuple).
        stats = ",".join(["1000"] * 23)
        return (f"COMBATANT_INFO,{guid},1,{stats},{spec_id},"
                f"[(90928,112838,1),(90929,112839,1)],(0,0,0),"
                f"[(1,2,3)],[4,5],0,0,0,0")

    def _base(self):
        b = LogBuilder()
        b.start(0)
        return b

    def test_spec_is_read_from_the_real_2026_combatant_info_layout(self):
        b = self._base()
        b.raw(0.5, self._real_combatant_info(DPS1[0], 1480))   # Devourer DH
        b.raw(0.5, self._real_combatant_info(TANK[0], 66))     # Prot Pal
        b.combatant(0.5, HEALER)  # older bare-paren layout must still work
        b.player_damage(10, DPS1, LogBuilder.npc_guid(FELWYRM, "0001"), "Felwyrm",
                        133, "Fireball", 1000)
        b.end(100)
        # keyed by guid: tank/healer only appear via COMBATANT_INFO here,
        # which carries no name
        players = {p["guid"]: p for p in self._analyze(b)["players"]}
        assert (players[DPS1[0]]["class"], players[DPS1[0]]["spec"],
                players[DPS1[0]]["role"]) == ("Demon Hunter", "Devourer", "dps")
        assert players[TANK[0]]["spec"] == "Protection"
        assert players[HEALER[0]]["spec"] == "Restoration"

    def test_pet_casts_do_not_count_toward_the_owners_cpm(self):
        b = self._base()
        pet = "Pet-0-1465-2830-12345-416-0000ABCD"
        pet_flags = 0x1112  # party-affiliated, player-controlled pet
        mob = LogBuilder.npc_guid(FELWYRM, "0001")
        # SPELL_SUMMON teaches ownership: this pet belongs to DPS1
        b.raw(1, f'SPELL_SUMMON,{DPS1[0]},"{DPS1[1]}",{DPS1[2]:#06x},0x0,'
                 f'{pet},"Imp",{pet_flags:#06x},0x0,688,"Summon Imp",0x20')
        b.cast(5, DPS1, 133, "Fireball")
        adv = LogBuilder._advanced(pet)
        for t in (6, 7, 8, 9):
            b.raw(t, f'SPELL_CAST_SUCCESS,{pet},"Imp",{pet_flags:#06x},0x0,'
                     f'{mob},"Felwyrm",{HOSTILE:#06x},0x0,3110,"Firebolt",0x4,{adv}')
        # ...but the pet's DAMAGE still lands on the owner's total
        b.spell_damage(10, pet, "Imp", pet_flags, mob, "Felwyrm", HOSTILE,
                       3110, "Firebolt", 7000)
        b.player_damage(11, DPS1, mob, "Felwyrm", 133, "Fireball", 1000)
        b.end(100)
        players = {p["name"]: p for p in self._analyze(b)["players"]}
        zap = players["Zappyboi-Area52"]
        assert zap["casts_total"] == 1          # the Fireball, not 4 Firebolts
        assert zap["damage_done"] == 8000       # 7000 imp + 1000 own
        assert "Pets & Guardians" not in players

    def test_absorbed_damage_counts_as_dealt(self):
        b = self._base()
        mob = LogBuilder.npc_guid(FELWYRM, "0001")
        adv = LogBuilder._advanced(mob)
        # amount=1000 landed, absorbed=500 by the mob's shield -> 1500 dealt
        b.raw(10, f'SPELL_DAMAGE,{DPS1[0]},"{DPS1[1]}",{DPS1[2]:#06x},0x0,'
                  f'{mob},"Felwyrm",{HOSTILE:#06x},0x0,133,"Fireball",0x4,{adv},'
                  f'1000,1500,0,0x4,0,0,500,nil,nil,nil,nil')
        b.end(100)
        players = {p["name"]: p for p in self._analyze(b)["players"]}
        assert players["Zappyboi-Area52"]["damage_done"] == 1500

    def test_an_unowned_pet_that_only_casts_does_not_appear_as_a_player(self):
        b = self._base()
        stray = "Creature-0-1465-2830-12345-99999-0000BEEF"
        pet_flags = 0x1112
        b.player_damage(5, DPS1, LogBuilder.npc_guid(FELWYRM, "0001"), "Felwyrm",
                        133, "Fireball", 1000)
        # a party pet with no known owner casts but never deals damage
        adv = LogBuilder._advanced(stray)
        b.raw(6, f'SPELL_CAST_SUCCESS,{stray},"Stray",{pet_flags:#06x},0x0,'
                 f'0000000000000000,nil,0x80000000,0x0,1,"Growl",0x1,{adv}')
        b.end(100)
        names = [p["name"] for p in self._analyze(b)["players"]]
        assert "Pets & Guardians" not in names


class TestAnalyzeRun:
    def test_full_report(self, run_segment, route, dungeon_data_file):
        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store)
        # JSON-serializable end to end
        payload = json.loads(json.dumps(report))
        assert payload["run"]["timed"] is True
        assert payload["dungeon"]["name"] == "Murder Row"
        assert payload["forces"]["killed"] == 22
        assert payload["forces"]["pct"] == 22.0
        assert payload["comparison"]["adherence_pct"] == 66.7
        assert len(payload["pulls"]) == 3
        assert payload["pulls"][2]["boss"] == "Big Boss"
        assert payload["deaths"][0]["player"] == "Bigheals-Area52"
        players = {p["name"]: p for p in payload["players"]}
        assert players["Zappyboi-Area52"]["dps"] > 0
        assert players["Thicktank-Area52"]["role"] == "tank"
        kicks = payload["kick_value"]
        assert kicks["total_estimated_prevented_damage"] == 245000
        assert kicks["total_estimated_prevented_healing"] == 80000
        # The two summary tiles (damage, enemy healing) must together
        # equal the players table's combined "Kick prev. (dmg+heal)"
        # column. Real report (2026-09-05): only the damage tile existed,
        # so a run whose kicks mostly stopped enemy *healing* showed
        # ~2.11m in the summary against ~7.94m added up across the player
        # rows -- same underlying numbers, two different subsets, no way
        # for a reader to reconcile them.
        column_total = sum(
            (p.get("kick_prevented_damage") or 0) + (p.get("kick_prevented_healing") or 0)
            for p in payload["players"]
        )
        assert column_total == (kicks["total_estimated_prevented_damage"]
                                + kicks["total_estimated_prevented_healing"])
        assert kicks["by_player"][0]["name"] == "Thicktank-Area52"
        obs = {(o["spell_id"], o["kind"]): o for o in kicks["spell_observations"]}
        assert obs[(1216538, "damage")]["avg_per_cast"] == 200000
        assert obs[(888001, "healing")]["observed_casts"] == 1
        rot_obs = obs[(777001, "damage")]
        assert rot_obs["avg_per_cast"] == 45000
        assert rot_obs["avg_dot"] == 45000 and rot_obs["avg_direct"] == 0
        hex_obs = obs[(777002, "damage")]
        assert hex_obs["avg_per_cast"] == 0
        assert hex_obs["debuff_applications"] == 1
        # new run-level sections
        assert payload["enemy_casts"]["kick_efficiency_pct"] == 42.9
        through = {s["name"]: s for s in payload["enemy_casts"]["spells"]}
        assert through["Dark Bolt"]["got_through"] == 2
        # no --interrupt-data passed to analyze_run(): every spell falls
        # back to the unchanged heuristic, tagged interruptible: None
        assert through["Dark Bolt"]["interruptible"] is None
        assert payload["death_cost"] == {"deaths": 1, "per_death_s": 15.0,
                                         "total_s": 15.0}
        assert payload["deaths"][0]["biggest_hit"] == 250000
        assert payload["deaths"][0]["damage_last_5s"] == 400000
        assert payload["encounters"][0]["kill"] is True
        players = {p["name"]: p for p in payload["players"]}
        # Rates are over combat-*active* time (sum of pull durations --
        # what in-game meters divide by), not wall time: this fixture's
        # pulls cover about half its wall clock, so active-time CPM is
        # ~2x the old wall-time 1.7. The wall-clock figures survive as
        # dps_wall/hps_wall.
        zap = players["Zappyboi-Area52"]
        assert payload["run"]["active_duration_s"] < payload["run"]["wall_duration_s"]
        assert zap["cpm"] == pytest.approx(3.4, abs=0.1)
        assert zap["dps"] > zap["dps_wall"] > 0
        assert zap["dps_wall"] == pytest.approx(
            zap["damage_done"] / payload["run"]["wall_duration_s"], rel=1e-3)
        assert zap["dps"] == pytest.approx(
            zap["damage_done"] / payload["run"]["active_duration_s"], rel=1e-3)
        assert players["Zappyboi-Area52"]["killing_blows"] == 4
        lust = [b for b in players["Zappyboi-Area52"]["buff_uptimes"]
                if b["name"] == "Bloodlust"]
        assert lust and lust[0]["uptime_pct"] == pytest.approx(21.4, abs=0.1)
        assert len(payload["positions"]["Zappyboi-Area52"]) == 4
        assert payload["pulls"][0]["group_dps"] > 0
        # "map" block: present whenever dungeon data is present, with the
        # geometry-only planned map even though this run's synthetic
        # dungeon data doesn't have enough single-clone npcs to calibrate
        # a player-path overlay (see TestCalibrate for the calibration
        # math itself, tested in isolation).
        m = payload["map"]
        assert m["canvas"] == {"width": 840, "height": 555}
        assert len(m["enemies"]) == 4 + 3 + 1 + 4  # Felwyrm/Duskblade/Boss/Shadeling clones
        assert m["calibration"]["ok"] is False
        assert m["calibration"]["reason"] == "insufficient anchors"
        assert m["players"] == []
        assert m["deaths"] == []

    def test_report_without_dungeon_data(self, run_segment, route):
        report = analyze_run(run_segment, route=route, store=None)
        assert "error" in report["comparison"]
        assert report["forces"]["required"] is None
        # no dungeon data -> no map block at all (not an empty/broken one)
        assert "map" not in report

    def test_renderers(self, run_segment, route, dungeon_data_file):
        from postmortem.report.html import render_html
        from postmortem.report.text import render_text

        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store)
        text = render_text(report)
        assert "MYTHIC+ POST-MORTEM" in text
        assert "Murder Row" in text
        assert "EARLY: 1x Duskblade" in text
        assert "OFF-ROUTE: 1x Shadeling" in text
        assert "~200.0k dmg prevented" in text
        assert "~80.0k healing prevented" in text
        assert "~45.0k dmg (45.0k of it DoT) prevented" in text
        assert "a debuff application (seen 1x elsewhere, no damage)" in text
        assert "never landed" in text  # Mystery Bolt kick has no estimate
        assert "Kick efficiency: 42.9%" in text
        assert "ENEMY CASTS THAT GOT THROUGH" in text
        assert "Death cost: 1 deaths" in text
        assert "CONSUMABLES" in text
        assert "1 potions" in text
        html = render_html(report)
        assert "<html" in html and "Murder Row" in html
        assert "</script>" in html
        # both kick-value tiles exist in the template, and the players
        # column says which components it combines (see the reconciliation
        # assertion in test_full_report)
        assert "dmg prevented by kicks (est.)" in html
        assert "enemy healing prevented by kicks (est.)" in html
        assert "Kick prev. (dmg+heal)" in html
        # no --avoidable-data passed: no section in either renderer (the
        # HTML template's JS source always defines avoidableDamage(), but
        # without avoidable_damage data in the embedded JSON it renders "")
        assert "AVOIDABLE DAMAGE" not in text
        assert '"avoidable_damage"' not in html
        # map block present (plan-only, since this fixture can't calibrate):
        # the SVG-rendering JS is always defined in the template (like
        # avoidableDamage() above), so what actually distinguishes "the
        # section has data" is the embedded report JSON, not raw
        # substring-presence of "<svg" in the page's JS source text.
        assert "<svg" in html
        assert '"map":' in html

    def test_stealable_data_flows_through_and_colors_the_reports(
        self, run_segment, route, dungeon_data_file,
    ):
        from postmortem.report.html import render_html
        from postmortem.report.text import render_text

        store = DungeonDataStore.load(dungeon_data_file)
        stealable = StealableData(spells={1216538: {"name": "Dark Bolt", "note": None}})
        report = analyze_run(run_segment, route=route, store=store, stealable=stealable)

        by_name = {s["name"]: s for s in report["enemy_casts"]["spells"]}
        assert by_name["Dark Bolt"]["stealable"] is True
        assert by_name["Creeping Rot"]["stealable"] is False

        # render_html() only embeds the report as JSON -- the "stealable"
        # row class/star are generated client-side by the template's JS
        # from that JSON at page-load, not by this Python renderer (same
        # reasoning as test_renderers' own avoidable-damage checks below:
        # what distinguishes "this section has data" from html's output
        # is the embedded JSON, not literal rendered markup). The CSS
        # (static, always present) and the client-side rendering logic
        # that reads r.stealable are checked instead of a visual result.
        html = render_html(report)
        assert '"stealable": true' in html
        assert "tr.stealable" in html  # CSS for the row highlight exists
        assert "s.stealable" in html  # the JS template actually reads the flag

        text = render_text(report)
        assert "* Dark Bolt" in text
        assert "  Creeping Rot" in text  # untagged: two-space prefix, no star
        assert "* = worth Spellstealing" in text

    def test_no_stealable_data_never_shows_the_stealable_flag_or_star(
        self, run_segment, route, dungeon_data_file,
    ):
        from postmortem.report.html import render_html
        from postmortem.report.text import render_text

        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store)
        html = render_html(report)
        assert '"stealable": true' not in html
        text = render_text(report)
        assert "worth Spellstealing" not in text
        assert not any(line.startswith("* ") for line in text.splitlines())

    def test_html_title_escapes_a_malicious_zone_name(self):
        # Stored-XSS regression (2026-09-01): the report <title> took the
        # zone name unescaped, and zone comes straight from a combat log's
        # CHALLENGE_MODE_START -- attacker-chosen text once a log is
        # uploaded to the public tracker, whose CSP allows inline scripts.
        # A "</title><script>" zone must not produce a live script element
        # in the document head.
        from postmortem.report.html import render_html

        report = {"run": {"zone": "</title><script>alert(1)</script>",
                          "keystone_level": 10}}
        html = render_html(report)
        # The title is escaped, and the embedded JSON separately escapes
        # "</" to "<\/", so the raw breakout string appears nowhere live.
        assert "</title><script>alert(1)</script>" not in html
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_renderer_omits_map_without_dungeon_data(self, run_segment, route):
        from postmortem.report.html import render_html

        report = analyze_run(run_segment, route=route, store=None)
        assert "map" not in report
        html = render_html(report)
        assert "<html" in html
        # no dungeon data -> no "map" key in the embedded report JSON, so
        # the JS guard (`if (!m ...) return "";`) renders nothing for it
        assert '"map":' not in html

    def test_renderer_guards_forces_tile_behind_required_data(self, run_segment, route):
        """Regression test for a real bug (2026-08-31): the forces stat
        tile used to render unconditionally, showing a misleading "0
        forces killed" whenever no dungeon data was available -- which is
        *always* true for the public site's raw-log /upload path (it has
        no way to supply dungeon data), so every website-uploaded run
        showed a permanent, wrong-looking zero. Every other
        data-dependent stat in the same grid (route/adherence/kick value)
        was already correctly omitted via a ternary; forces wasn't.

        Unlike the report JSON itself, the rendered HTML page's JS only
        executes in a real browser -- this test suite has no headless
        browser to run it (see test_renderer_omits_map_without_dungeon_data's
        own note on this same limitation) -- so this pins the guard's
        presence in the generated JS source rather than the rendered
        visual output.
        """
        from postmortem.report.html import render_html

        report = analyze_run(run_segment, route=route, store=None)
        assert report["forces"]["required"] is None  # the no-data case this guards
        html = render_html(report)
        assert "forces.required ? stat(" in html

    def test_html_renders_calibrated_path_and_death_markers(
        self, run_segment, route, dungeon_data_file
    ):
        # Exercise the calibrated-overlay branch directly (the synthetic
        # fixture's dungeon data doesn't have enough single-clone npcs to
        # calibrate for real -- see TestAnalyzeRun.test_full_report) by
        # building a report whose "map" block already reflects a
        # successful calibration, and confirming the renderer draws a
        # player path and a death marker rather than just the dim
        # fallback note.
        from postmortem.report.html import render_html

        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store)
        report["map"]["calibration"] = {
            "ok": True, "anchor_count": 5, "residual": 1.2,
            "scale": 4.0, "rotation": 0.1, "reflected": False,
            "translation": {"x": 10.0, "y": -5.0},
        }
        report["map"]["players"] = [
            {"name": "Zappyboi-Area52", "guid": "Player-x",
             "path": [[0.0, 100.0, -100.0], [5.0, 110.0, -120.0]]}
        ]
        report["map"]["deaths"] = [
            {"player": "Bigheals-Area52", "t": 66.5, "x": 105.0, "y": -110.0}
        ]
        html = render_html(report)
        assert "<svg" in html
        # the calibrated-overlay data actually made it into the embedded
        # report JSON the page's JS reads (the JS source text itself
        # always contains e.g. "polyline"/"No player-path overlay" as
        # literal strings regardless of data, so those aren't meaningful
        # substring checks -- see test_renderers above for the same point)
        assert '"ok": true' in html
        assert '"anchor_count": 5' in html
        assert '"insufficient anchors"' not in html
        assert '"path": [[0.0, 100.0, -100.0]' in html
        assert '"player": "Bigheals-Area52"' in html


class TestAvoidableDataLoad:
    """AvoidableData.load: schema parsing, with and without 'dungeons'."""

    def test_loads_example_schema(self, tmp_path):
        path = tmp_path / "avoidable.json"
        path.write_text(json.dumps({
            "spells": [
                {"id": 1216538, "name": "Dark Bolt", "note": "easily dodged"},
            ],
            "dungeons": {"160": [1216538]},
        }), encoding="utf-8")
        data = AvoidableData.load(path)
        assert data.spells[1216538] == {"name": "Dark Bolt", "note": "easily dodged"}
        assert data.dungeons == {160: {1216538}}

    def test_loads_without_dungeons_key(self, tmp_path):
        path = tmp_path / "avoidable.json"
        path.write_text(json.dumps({
            "spells": [{"id": 1216538, "name": "Dark Bolt"}],
        }), encoding="utf-8")
        data = AvoidableData.load(path)
        assert data.spells[1216538]["name"] == "Dark Bolt"
        assert data.spells[1216538]["note"] is None
        assert data.dungeons == {}

    def test_loads_the_shipped_example_file(self):
        example = Path(__file__).resolve().parents[1] / "docs" / "avoidable_spells.example.json"
        data = AvoidableData.load(example)
        assert 1216538 in data.spells
        assert data.spells[1216538]["name"] == "Fel Detonation"
        assert data.dungeons.get(160) == {1216538, 1217099}

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            AvoidableData.load(tmp_path / "does-not-exist.json")

    def test_malformed_json_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            AvoidableData.load(path)

    def test_missing_spells_key_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"dungeons": {}}), encoding="utf-8")
        with pytest.raises(KeyError):
            AvoidableData.load(path)


class TestAvoidableDamage:
    """Avoidable-damage tagging: PlayerStats totals/hits, the full report
    block, and the top-15-truncation regression the WP brief calls out."""

    DARK_BOLT = 1216538
    LOW_FREQ_SPELL = 900001

    @pytest.fixture()
    def avoidable(self) -> AvoidableData:
        return AvoidableData(spells={
            self.DARK_BOLT: {"name": "Dark Bolt", "note": "test fixture"},
        })

    def test_player_totals_and_hits(self, run_segment, dungeon, avoidable):
        pulls = detect_pulls(run_segment.events)
        stats = compute_stats(run_segment.events, pulls, dungeon, avoidable=avoidable)
        by_name = {p.name: p for p in stats.players.values()}

        healer = by_name["Bigheals-Area52"]
        # Dark Bolt hit the healer twice: 150000 (t=64) and 250000 (t=66)
        assert healer.avoidable_damage_taken == 400000
        assert healer.avoidable_hits == 2
        assert healer.damage_taken_hits_by_spell[(self.DARK_BOLT, "Dark Bolt")] == 2

        # players never hit by a tagged spell stay at zero
        tank = by_name["Thicktank-Area52"]
        assert tank.avoidable_damage_taken == 0
        assert tank.avoidable_hits == 0

    def test_hit_counter_tracks_all_damage_taken_spells(self, run_segment, dungeon):
        # damage_taken_hits_by_spell is populated regardless of tagging
        pulls = detect_pulls(run_segment.events)
        stats = compute_stats(run_segment.events, pulls, dungeon)
        healer = next(p for p in stats.players.values() if p.name == "Bigheals-Area52")
        assert healer.damage_taken_hits_by_spell[(1216538, "Dark Bolt")] == 2

    def test_no_avoidable_data_leaves_players_untouched(self, run_segment, dungeon):
        pulls = detect_pulls(run_segment.events)
        stats = compute_stats(run_segment.events, pulls, dungeon)
        for p in stats.players.values():
            assert p.avoidable_damage_taken == 0
            assert p.avoidable_hits == 0

    def test_full_report_json_roundtrip(self, run_segment, route, dungeon_data_file, avoidable):
        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store, avoidable=avoidable)
        payload = json.loads(json.dumps(report))

        block = payload["avoidable_damage"]
        assert block["tagged_spell_count"] == 1
        assert block["total_damage"] == 400000
        by_player = {e["name"]: e for e in block["by_player"]}
        assert by_player["Bigheals-Area52"]["avoidable_damage_taken"] == 400000
        assert by_player["Bigheals-Area52"]["avoidable_hits"] == 2
        (spell,) = by_player["Bigheals-Area52"]["by_spell"]
        assert spell == {"spell_id": 1216538, "name": "Dark Bolt",
                          "amount": 400000, "hits": 2}
        assert "Thicktank-Area52" not in by_player  # untouched players are omitted

        # per-player flat field on the player summary too
        players = {p["name"]: p for p in payload["players"]}
        assert players["Bigheals-Area52"]["avoidable_damage_taken"] == 400000
        assert players["Thicktank-Area52"]["avoidable_damage_taken"] == 0

    def test_no_flag_omits_report_block(self, run_segment, route, dungeon_data_file):
        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store)
        assert "avoidable_damage" not in report

    def test_truncated_top15_does_not_hide_tagged_spell(self):
        """Regression test for the top_damage_taken truncation gap: a
        player takes damage from 15 high-damage spells plus one low-damage
        *tagged* spell. summary()'s top_damage_taken (top 15) must exclude
        the low-damage one, while avoidable-damage tagging -- which reads
        the full damage_taken_by_spell Counter -- must still catch it."""
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        b.combatant(0.5, TANK)
        for i in range(15):
            b.npc_damage(10 + i, npc, "Felwyrm", TANK, 800000 + i, f"Big Hit {i}", 100000)
        b.npc_damage(30, npc, "Felwyrm", TANK, self.LOW_FREQ_SPELL, "Sneaky Puddle", 1)
        b.npc_damage(31, npc, "Felwyrm", TANK, self.LOW_FREQ_SPELL, "Sneaky Puddle", 2)
        b.end(40)

        (run,) = list(segment_runs(iter_events(b.lines)))
        pulls = detect_pulls(run.events)
        avoidable = AvoidableData(spells={
            self.LOW_FREQ_SPELL: {"name": "Sneaky Puddle", "note": None},
        })
        stats = compute_stats(run.events, pulls, avoidable=avoidable)
        tank = next(p for p in stats.players.values() if p.name == TANK[1])

        top_ids = {s["spell_id"] for s in tank.summary()["top_damage_taken"]}
        assert len(tank.summary()["top_damage_taken"]) == 15
        assert self.LOW_FREQ_SPELL not in top_ids  # confirms the truncation actually bites

        assert tank.avoidable_damage_taken == 3
        assert tank.avoidable_hits == 2

    def test_renderers_show_section_when_present(self, run_segment, route,
                                                  dungeon_data_file, avoidable):
        from postmortem.report.html import render_html
        from postmortem.report.text import render_text

        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store, avoidable=avoidable)

        text = render_text(report)
        assert "AVOIDABLE DAMAGE" in text
        assert "Bigheals-Area52" in text
        assert "Dark Bolt" in text

        # the HTML report is a static JS template + an embedded JSON data
        # payload (see render_html) -- the JS function's source text is
        # always present in the template either way, so the meaningful
        # presence signal is the data it reads out of, not the function.
        html = render_html(report)
        assert "function avoidableDamage()" in html  # the renderer exists
        assert '"avoidable_damage"' in html           # ...and has data to render
        assert '"Bigheals-Area52"' in html

    def test_renderers_omit_section_when_absent(self, run_segment, route, dungeon_data_file):
        from postmortem.report.html import render_html
        from postmortem.report.text import render_text

        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store)

        text = render_text(report)
        assert "AVOIDABLE DAMAGE" not in text

        html = render_html(report)
        assert '"avoidable_damage"' not in html


class TestDeathDefensives:
    """WP-A3: personal defensive usage on deaths (gamedata.DEFENSIVES)."""

    BIG_HIT_SPELL = 900001

    @staticmethod
    def _minimal_log():
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        return b, npc

    def _kill(self, b, npc, player, t=50.0):
        b.npc_damage(t, npc, "Felwyrm", player, self.BIG_HIT_SPELL, "Big Hit",
                     500000, hp=0)
        b.unit_died(t + 0.5, player[0], player[1], player[2])
        b.end(t + 10)

    @staticmethod
    def _one_death(b):
        (run,) = list(segment_runs(iter_events(b.lines)))
        pulls = detect_pulls(run.events)
        return compute_stats(run.events, pulls)

    def test_defensive_cast_shortly_before_death(self):
        # Protection Paladin (spec 66): Divine Shield cast 5s before death,
        # well inside the 10s cast_timeline window.
        b, npc = self._minimal_log()
        b.combatant(0.5, TANK)
        b.cast(45, TANK, 642, "Divine Shield")
        self._kill(b, npc, TANK)

        (death,) = self._one_death(b).deaths
        assert death.died_without_defensive is False
        (used,) = death.defensives_used_before_death
        assert used["spell_id"] == 642
        assert used["name"] == "Divine Shield"
        # log ts values are absolute (see LogBuilder._stamp); death was
        # built to land 5.5s after the cast (t=45 cast, t=50.5 death)
        assert death.ts - used["ts"] == pytest.approx(5.5, abs=0.01)

    def test_no_defensive_used(self):
        # same spec, same death, but no defensive cast anywhere in the log
        b, npc = self._minimal_log()
        b.combatant(0.5, TANK)
        self._kill(b, npc, TANK)

        (death,) = self._one_death(b).deaths
        assert death.died_without_defensive is True
        assert death.defensives_used_before_death == []

    def test_defensive_still_active_beyond_10s_cast_window(self):
        # Divine Shield goes up 15s before death and is never removed --
        # still active at death, but outside the naive 10s cast window, so
        # only the retained buff-window path can catch it.
        b, npc = self._minimal_log()
        b.combatant(0.5, TANK)
        b.aura(35, TANK, TANK, 642, "Divine Shield")
        self._kill(b, npc, TANK)  # dies at t=50.5

        (death,) = self._one_death(b).deaths
        assert death.died_without_defensive is False
        (used,) = death.defensives_used_before_death
        assert used["spell_id"] == 642
        # aura applied at t=35, death at t=50.5 -- 15.5s gap, well outside
        # the 10s cast-timeline window, only found via the buff-window path
        assert death.ts - used["ts"] == pytest.approx(15.5, abs=0.01)

    def test_unrecognized_spec_reports_none(self):
        # Devastation Evoker (spec 1467): a real, known spec_id, but one
        # gamedata.DEFENSIVES intentionally doesn't cover -- must never be
        # reported as "died without a defensive" since we don't actually
        # know whether that's true.
        mystery = ("Player-1403-0000000D", "Mysteryman-Area52", 0x511, 1467)
        b, npc = self._minimal_log()
        b.combatant(0.5, mystery)
        self._kill(b, npc, mystery)

        (death,) = self._one_death(b).deaths
        assert death.died_without_defensive is None
        assert death.defensives_used_before_death == []

    def test_missing_spec_info_reports_none(self):
        # no COMBATANT_INFO at all for this player -> spec_id stays None
        ghost = ("Player-1403-0000000E", "Ghosty-Area52", 0x511, 66)
        b, npc = self._minimal_log()
        self._kill(b, npc, ghost)

        (death,) = self._one_death(b).deaths
        assert death.died_without_defensive is None

    def test_no_cast_timeline_falls_back_to_none_not_false_true(self):
        # --no-cast-timeline (full_cast_timeline=False): cast_timeline is
        # empty, so the cast-based half of detection has no data. Must not
        # be reported as a confident "died without a defensive".
        b, npc = self._minimal_log()
        b.combatant(0.5, TANK)
        self._kill(b, npc, TANK)

        (run,) = list(segment_runs(iter_events(b.lines)))
        pulls = detect_pulls(run.events)
        stats = compute_stats(run.events, pulls, full_cast_timeline=False)

        assert stats.cast_timeline == []
        (death,) = stats.deaths
        assert death.died_without_defensive is None

    def test_no_cast_timeline_still_finds_active_buff_window(self):
        # the buff-window path doesn't depend on full_cast_timeline, so an
        # active defensive is still caught even with casts untracked
        b, npc = self._minimal_log()
        b.combatant(0.5, TANK)
        b.aura(35, TANK, TANK, 642, "Divine Shield")
        self._kill(b, npc, TANK)

        (run,) = list(segment_runs(iter_events(b.lines)))
        pulls = detect_pulls(run.events)
        stats = compute_stats(run.events, pulls, full_cast_timeline=False)

        (death,) = stats.deaths
        assert death.died_without_defensive is False
        assert death.defensives_used_before_death[0]["spell_id"] == 642

    def test_cast_timeline_carries_player_guid(self):
        # the name/GUID mismatch fix: cast_timeline entries must carry
        # player_guid (matching interrupt_events' existing pattern), since
        # death matching joins on GUID, not the fragile name string
        b, npc = self._minimal_log()
        b.combatant(0.5, TANK)
        b.cast(45, TANK, 642, "Divine Shield")
        stats = self._one_death(b)
        assert stats.cast_timeline[0]["player_guid"] == TANK[0]

    def test_renderers_show_biggest_hit_damage_last_5s_and_defensive_status(
        self, run_segment, route, dungeon_data_file
    ):
        from postmortem.report.html import render_html
        from postmortem.report.text import render_text

        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store)
        # the log's one death (Bigheals-Area52, Restoration Shaman) never
        # casts its known defensive (Astral Shift, spell id 108271)
        assert report["deaths"][0]["biggest_hit"] == 250000
        assert report["deaths"][0]["damage_last_5s"] == 400000
        assert report["deaths"][0]["died_without_defensive"] is True

        text = render_text(report)
        assert "biggest hit: 250.0k" in text
        assert "last 5s: 400.0k" in text
        assert "no defensive used" in text

        html = render_html(report)
        assert '"biggest_hit": 250000' in html
        assert '"damage_last_5s": 400000' in html
        assert '"died_without_defensive": true' in html

        # JSON round-trippable end to end, new fields included
        payload = json.loads(json.dumps(report))
        assert payload["deaths"][0]["died_without_defensive"] is True


class TestCCUptime:
    """Hard-CC uptime landed on hostile targets (gamedata.CC_SPELLS,
    stats.cc_events / RunStats.cc_events) -- distinct from interrupts, which
    already have their own coverage (TestEnemyCastSummary)."""

    POLYMORPH = 118  # CC_SPELLS: ("Polymorph", "incapacitate")
    HOJ = 853  # CC_SPELLS: ("Hammer of Justice", "stun")

    def test_cc_application_tracked_with_duration_and_caster(self):
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        target = (npc, "Felwyrm", HOSTILE, None)
        b.start(0)
        b.combatant(0.5, TANK)
        b.aura(5, TANK, target, self.POLYMORPH, "Polymorph", kind="DEBUFF")
        b.aura_removed(15, TANK, target, self.POLYMORPH, "Polymorph", kind="DEBUFF")
        b.end(40)

        (run,) = list(segment_runs(iter_events(b.lines)))
        stats = compute_stats(run.events, detect_pulls(run.events))

        assert len(stats.cc_events) == 1
        ev = stats.cc_events[0]
        assert ev["spell_id"] == self.POLYMORPH
        assert ev["spell"] == "Polymorph"
        assert ev["cc_type"] == "incapacitate"
        assert ev["target"] == "Felwyrm"
        assert ev["caster"] == TANK[1]
        assert ev["duration_s"] == pytest.approx(10.0, abs=0.1)

    def test_cc_still_active_at_run_end_counts_until_last_event(self):
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        target = (npc, "Felwyrm", HOSTILE, None)
        b.start(0)
        b.combatant(0.5, TANK)
        b.aura(5, TANK, target, self.POLYMORPH, "Polymorph", kind="DEBUFF")
        b.end(20)  # never removed

        (run,) = list(segment_runs(iter_events(b.lines)))
        stats = compute_stats(run.events, detect_pulls(run.events))

        assert len(stats.cc_events) == 1
        assert stats.cc_events[0]["duration_s"] == pytest.approx(15.0, abs=0.1)

    def test_cc_ends_at_target_death_not_lost(self):
        # a dead unit's auras never get an explicit SPELL_AURA_REMOVED --
        # an open CC window must close at the target's UNIT_DIED instead of
        # leaking or being silently dropped.
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        target = (npc, "Felwyrm", HOSTILE, None)
        b.start(0)
        b.combatant(0.5, TANK)
        b.aura(5, TANK, target, self.POLYMORPH, "Polymorph", kind="DEBUFF")
        b.unit_died(12, npc, "Felwyrm", HOSTILE)
        b.end(20)

        (run,) = list(segment_runs(iter_events(b.lines)))
        stats = compute_stats(run.events, detect_pulls(run.events))

        assert len(stats.cc_events) == 1
        assert stats.cc_events[0]["duration_s"] == pytest.approx(7.0, abs=0.1)

    def test_summary_aggregates_by_player_and_type(self):
        b = LogBuilder()
        t1 = (b.npc_guid(FELWYRM, "0001"), "Felwyrm", HOSTILE, None)
        t2 = (b.npc_guid(FELWYRM, "0002"), "Felwyrm", HOSTILE, None)
        b.start(0)
        b.combatant(0.5, TANK)
        b.combatant(0.5, DPS1)
        b.aura(5, TANK, t1, self.POLYMORPH, "Polymorph", kind="DEBUFF")
        b.aura_removed(10, TANK, t1, self.POLYMORPH, "Polymorph", kind="DEBUFF")
        b.aura(6, DPS1, t2, self.HOJ, "Hammer of Justice", kind="DEBUFF")
        b.aura_removed(9, DPS1, t2, self.HOJ, "Hammer of Justice", kind="DEBUFF")
        b.end(20)

        (run,) = list(segment_runs(iter_events(b.lines)))
        stats = compute_stats(run.events, detect_pulls(run.events))
        summary = _cc_summary(stats)

        assert summary["total_duration_s"] == pytest.approx(8.0, abs=0.1)  # 5 + 3
        players = {e["name"]: e for e in summary["by_player"]}
        assert players[TANK[1]] == {"name": TANK[1], "casts": 1, "total_duration_s": 5.0}
        assert players[DPS1[1]] == {"name": DPS1[1], "casts": 1, "total_duration_s": 3.0}
        types = {e["cc_type"] for e in summary["by_type"]}
        assert types == {"incapacitate", "stun"}
        assert len(summary["events"]) == 2

    def test_no_cc_gives_empty_summary(self):
        stats = RunStats()
        summary = _cc_summary(stats)
        assert summary == {
            "total_duration_s": 0, "by_player": [], "by_type": [], "events": [],
        }

    def test_renders_in_text_and_html(self):
        from postmortem.report.html import render_html
        from postmortem.report.text import render_text

        b = LogBuilder()
        target = (b.npc_guid(FELWYRM, "0001"), "Felwyrm", HOSTILE, None)
        b.start(0)
        b.combatant(0.5, TANK)
        b.aura(5, TANK, target, self.POLYMORPH, "Polymorph", kind="DEBUFF")
        b.aura_removed(15, TANK, target, self.POLYMORPH, "Polymorph", kind="DEBUFF")
        b.end(40)

        (run,) = list(segment_runs(iter_events(b.lines)))
        report = analyze_run(run)

        text = render_text(report)
        assert "CROWD CONTROL" in text
        assert "Polymorph" in text and "Felwyrm" in text

        html = render_html(report)
        assert "Polymorph" in html
        assert '"cc_type": "incapacitate"' in html


class TestCloseCalls:
    """Damage that drops a player below CLOSE_CALL_HP_PCT without killing
    them (RunStats.close_calls / _tag_close_calls)."""

    BIG_HIT = 900001

    def test_drop_below_threshold_recorded_as_close_call(self):
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        b.combatant(0.5, TANK)
        b.npc_damage(5, npc, "Felwyrm", TANK, self.BIG_HIT, "Big Hit", 50000, hp=150000)  # 15%
        b.end(20)

        (run,) = list(segment_runs(iter_events(b.lines)))
        stats = compute_stats(run.events, detect_pulls(run.events))

        assert len(stats.close_calls) == 1
        cc = stats.close_calls[0]
        assert cc["player"] == TANK[1]
        assert cc["hp_pct"] == pytest.approx(15.0, abs=0.1)
        assert cc["spell"] == "Big Hit"
        assert cc["source"] == "Felwyrm"

    def test_healthy_hit_is_not_a_close_call(self):
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        b.combatant(0.5, TANK)
        b.npc_damage(5, npc, "Felwyrm", TANK, self.BIG_HIT, "Chip", 50000, hp=800000)  # 80%
        b.end(20)

        (run,) = list(segment_runs(iter_events(b.lines)))
        stats = compute_stats(run.events, detect_pulls(run.events))
        assert stats.close_calls == []

    def test_only_the_transition_into_danger_is_recorded(self):
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        b.combatant(0.5, TANK)
        b.npc_damage(5, npc, "Felwyrm", TANK, self.BIG_HIT, "Hit 1", 50000, hp=150000)  # 15%: new
        b.npc_damage(6, npc, "Felwyrm", TANK, self.BIG_HIT, "Hit 2", 10000, hp=140000)  # still low
        b.end(20)

        (run,) = list(segment_runs(iter_events(b.lines)))
        stats = compute_stats(run.events, detect_pulls(run.events))
        assert len(stats.close_calls) == 1

    def test_a_hit_that_actually_kills_is_not_reported_as_a_close_call(self):
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        b.combatant(0.5, TANK)
        b.npc_damage(5, npc, "Felwyrm", TANK, self.BIG_HIT, "Killing Hit", 50000, hp=10000)
        b.unit_died(5.05, TANK[0], TANK[1], TANK[2])
        b.end(20)

        (run,) = list(segment_runs(iter_events(b.lines)))
        stats = compute_stats(run.events, detect_pulls(run.events))
        assert stats.close_calls == []
        assert len(stats.deaths) == 1

    def test_renders_in_text_and_html(self):
        from postmortem.report.html import render_html
        from postmortem.report.text import render_text

        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        b.combatant(0.5, TANK)
        b.npc_damage(5, npc, "Felwyrm", TANK, self.BIG_HIT, "Big Hit", 50000, hp=150000)
        b.end(20)

        (run,) = list(segment_runs(iter_events(b.lines)))
        report = analyze_run(run)

        text = render_text(report)
        assert "CLOSE CALLS" in text
        assert "Big Hit" in text and "15.0%" in text

        html = render_html(report)
        assert "Big Hit" in html
        assert '"hp_pct": 15.0' in html


class TestUnplannedPulls:
    """Actual pulls that included enemies not part of the pasted route,
    surfaced directly from compare_route()'s existing off_route/untracked
    data (see TestRouteComparison for the underlying comparison coverage)."""

    def test_summary_surfaces_off_route_and_untracked_pulls(self, run_segment, route, dungeon):
        pulls = detect_pulls(run_segment.events)
        comparison = compare_route(route, pulls, dungeon).summary(dungeon)
        summary = _unplanned_pulls_summary(comparison)

        assert summary is not None
        (entry,) = summary["pulls"]  # only pull 2 has off-route/untracked content
        assert entry["actual_pull"] == 2
        assert {e["npc_id"] for e in entry["off_route"]} == {SHADELING}
        assert {e["npc_id"] for e in entry["untracked"]} == {SUMMONED}
        assert summary["total_off_route_mobs"] == 1
        assert summary["total_untracked_mobs"] == 1

    def test_no_comparison_returns_none(self):
        assert _unplanned_pulls_summary(None) is None
        assert _unplanned_pulls_summary({"error": "no dungeon data"}) is None

    def test_untracked_adds_shown_in_text_render(self, run_segment, route, dungeon_data_file):
        # Regression: text.py's per-pull deviation flags used to show
        # OFF-ROUTE but silently dropped untracked adds entirely (html.py
        # already had both) -- pull 2 in this fixture has one of each.
        from postmortem.report.text import render_text

        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store)
        text = render_text(report)
        assert "OFF-ROUTE" in text
        assert "ADDS" in text

    def test_wired_into_full_report(self, run_segment, route, dungeon_data_file):
        store = DungeonDataStore.load(dungeon_data_file)
        report = analyze_run(run_segment, route=route, store=store)

        assert "unplanned_pulls" in report
        assert report["unplanned_pulls"]["total_off_route_mobs"] == 1
        assert report["unplanned_pulls"]["total_untracked_mobs"] == 1

        payload = json.loads(json.dumps(report))
        assert payload["unplanned_pulls"]["pulls"][0]["actual_pull"] == 2
