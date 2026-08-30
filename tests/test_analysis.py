"""Pull detection, route comparison and stats on the synthetic run."""

import json

import pytest
from conftest import (
    BOSS,
    DUNGEON_DATA,
    DUSKBLADE,
    FELWYRM,
    HEALER,
    ROUTE_PRESET,
    SHADELING,
    SUMMONED,
    TANK,
    build_run_log,
)

from mythic_analyzer.analysis.compare import compare_route
from mythic_analyzer.analysis.pulls import detect_pulls
from mythic_analyzer.analysis.run_analyzer import analyze_run
from mythic_analyzer.analysis.stats import compute_stats
from mythic_analyzer.combatlog.parser import iter_events
from mythic_analyzer.combatlog.segmenter import segment_runs
from mythic_analyzer.mdt.dungeon_data import DungeonData, DungeonDataStore
from mythic_analyzer.mdt.route import Route


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
        assert len(samples) == 4
        assert samples[0][1:] == [100.0, -200.0]
        assert samples[2][1:] == [110.0, -230.0]

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
        assert payload["death_cost"] == {"deaths": 1, "per_death_s": 15.0,
                                         "total_s": 15.0}
        assert payload["deaths"][0]["biggest_hit"] == 250000
        assert payload["deaths"][0]["damage_last_5s"] == 400000
        assert payload["encounters"][0]["kill"] is True
        players = {p["name"]: p for p in payload["players"]}
        assert players["Zappyboi-Area52"]["cpm"] == pytest.approx(1.7, abs=0.1)
        assert players["Zappyboi-Area52"]["killing_blows"] == 4
        lust = [b for b in players["Zappyboi-Area52"]["buff_uptimes"]
                if b["name"] == "Bloodlust"]
        assert lust and lust[0]["uptime_pct"] == pytest.approx(21.4, abs=0.1)
        assert len(payload["positions"]["Zappyboi-Area52"]) == 4
        assert payload["pulls"][0]["group_dps"] > 0

    def test_report_without_dungeon_data(self, run_segment, route):
        report = analyze_run(run_segment, route=route, store=None)
        assert "error" in report["comparison"]
        assert report["forces"]["required"] is None

    def test_renderers(self, run_segment, route, dungeon_data_file):
        from mythic_analyzer.report.html import render_html
        from mythic_analyzer.report.text import render_text

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
