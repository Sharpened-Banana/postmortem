"""Per-run talent builds and gear, read from the log's own
COMBATANT_INFO (combatlog/combatant.py) and named via the bundled talent
map (talents.py), with choice nodes settled from the run's own casts.

Shapes here are taken from a real 2026 log line, not invented: the
talent block is ``[(node,entry,rank),...]``, gear is 18 INVSLOT-ordered
``(itemID,ilvl,(enchants),(bonusIDs),(gemID,gemIlvl,...))`` tuples, and
the pre-Dragonflight bare-paren talent layout still has to parse for its
spec id (the synthetic fixtures in conftest.py use it).
"""

from __future__ import annotations

import json

from postmortem.combatlog.combatant import (
    GEAR_SLOTS,
    parse_combatant_info,
    _split_top_level,
)
from postmortem.talents import TalentData, summarize

# One real player's line, trimmed to two gear slots for readability.
REAL_PARAMS = (
    "Player-162-0C227FE9,0,512,2424,34519,424,0,0,0,0,1151,1151,1151,91,0,"
    "736,736,736,61,492,416,416,416,956,66,"
    "[(81469,102430,1),(81471,102433,2),(95231,117876,1)],"
    "(0,0,0,0),"
    "[(271510,295,(7991,0,0),(6652,13696),()),"
    "(272228,305,(),(12841,6652),(240906,295,240900,295))],"
    "[Player-162-0C227FE9,1235111,1],11,0,0,0"
).split(",")


class TestSplitTopLevel:
    def test_respects_nesting_and_quotes(self):
        assert _split_top_level("a,(b,c),[d,(e,f)],\"g,h\"") == [
            "a", "(b,c)", "[d,(e,f)]", '"g,h"',
        ]


class TestParseCombatantInfo:
    def test_reads_spec_talents_and_gear_from_a_real_line(self):
        info = parse_combatant_info(REAL_PARAMS)
        assert info.guid == "Player-162-0C227FE9"
        assert info.spec_id == 66
        assert info.talents == [(81469, 102430, 1), (81471, 102433, 2), (95231, 117876, 1)]
        assert [g.slot for g in info.gear] == GEAR_SLOTS[:2]
        head, neck = info.gear
        assert (head.item_id, head.item_level) == (271510, 295)
        assert head.enchants == [7991]        # the trailing 0s are dropped
        assert head.gems == []
        # gems are (itemID, itemLevel) pairs -- two gems here, not four
        assert neck.gems == [240906, 240900]

    def test_average_item_level_ignores_empty_slots(self):
        info = parse_combatant_info(REAL_PARAMS)
        assert info.average_item_level() == 300.0  # (295 + 305) / 2

    def test_older_bare_paren_layout_still_yields_its_spec(self):
        # pre-Dragonflight talents were a bare "(...)" group; the spec id
        # still sits immediately before it (conftest's fixtures use this)
        params = ("Player-1-AAAA,0," + ",".join(["1000"] * 21)
                  + ",264,(1,2,3),(0,0,0,0),[1],[2],[3],630,0,0,0").split(",")
        info = parse_combatant_info(params)
        assert info.spec_id == 264
        assert info.talents == []   # nothing decodable in that layout
        assert info.gear == []

    def test_a_junk_line_yields_what_it_can_and_never_raises(self):
        info = parse_combatant_info(["Player-1-AAAA", "nonsense"])
        assert info is not None and info.guid == "Player-1-AAAA"
        assert info.spec_id is None and info.talents == []
        assert parse_combatant_info([]) is None


class TestTalentDecoding:
    DATA = TalentData({
        11: {"options": [{"name": "Solo Talent", "spell_id": 100}]},
        22: {"options": [{"name": "Bladestorm", "spell_id": 200},
                          {"name": "Ravager", "spell_id": 201}]},
        33: {"options": [{"name": "Hidden Passive A", "spell_id": 300},
                          {"name": "Hidden Passive B", "spell_id": 301}]},
    })

    def test_single_option_nodes_are_named_outright(self):
        (pick,) = self.DATA.decode([(11, 999, 2)])
        assert (pick.name, pick.rank, pick.spell_id) == ("Solo Talent", 2, 100)
        assert not pick.ambiguous

    def test_choice_node_settled_by_a_spell_id_seen_in_the_run(self):
        (pick,) = self.DATA.decode([(22, 999, 1)], spells_seen={201})
        assert pick.name == "Ravager" and not pick.ambiguous

    def test_choice_node_settled_by_spell_name_when_ids_differ(self):
        # a talent's tree spell id often isn't the id its effect logs
        # under, but the name matches -- that's what rescues most picks
        (pick,) = self.DATA.decode([(22, 999, 1)], spell_names_seen={"bladestorm"})
        assert pick.name == "Bladestorm"

    def test_choice_node_neither_option_witnessed_stays_ambiguous(self):
        (pick,) = self.DATA.decode([(33, 999, 1)])
        assert pick.name is None and pick.ambiguous
        assert pick.options == ["Hidden Passive A", "Hidden Passive B"]

    def test_choice_node_with_both_options_witnessed_stays_ambiguous(self):
        # can't tell them apart, so say so rather than pick one
        (pick,) = self.DATA.decode([(22, 999, 1)], spells_seen={200, 201})
        assert pick.ambiguous

    def test_unknown_node_keeps_its_id_and_rank(self):
        (pick,) = self.DATA.decode([(4242, 1, 1)])
        assert pick.node_id == 4242 and pick.name is None and pick.options is None

    def test_summary_counts(self):
        picks = self.DATA.decode([(11, 1, 1), (22, 1, 1), (33, 1, 1)], spells_seen={200})
        assert summarize(picks) == {
            "picks": [
                {"node_id": 11, "rank": 1, "name": "Solo Talent", "spell_id": 100},
                {"node_id": 22, "rank": 1, "name": "Bladestorm", "spell_id": 200},
                {"node_id": 33, "rank": 1,
                 "options": ["Hidden Passive A", "Hidden Passive B"]},
            ],
            "named_count": 2, "total_count": 3, "ambiguous_count": 1,
        }


class TestTalentDataLoading:
    def test_missing_or_broken_file_is_simply_empty(self, tmp_path):
        assert not TalentData.load(None)
        assert not TalentData.load(tmp_path / "nope.json")
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert not TalentData.load(bad)

    def test_round_trips_a_built_file(self, tmp_path):
        path = tmp_path / "talents.json"
        path.write_text(json.dumps({"nodes": {
            "81469": {"options": [{"name": "A", "spell_id": 1},
                                   {"name": "B", "spell_id": 2}]},
            "bogus": {"options": [{"name": "X"}]},
        }}), encoding="utf-8")
        data = TalentData.load(path)
        assert set(data.nodes) == {81469}
        assert len(data.nodes[81469]["options"]) == 2


class TestBuildsInTheReport:
    def test_players_carry_their_run_build(self, tmp_path):
        """analyze_run attaches build.talents/build.gear per player."""
        from conftest import build_run_log
        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.parser import iter_events
        from postmortem.combatlog.segmenter import segment_runs

        # a talent map naming one of the synthetic run's own spells, so
        # the choice node resolves through the run's casts
        data_path = tmp_path / "talents.json"
        data_path.write_text(json.dumps({"nodes": {
            "500": {"options": [{"name": "Fireball", "spell_id": 133},
                                 {"name": "Never Cast", "spell_id": 999999}]},
        }}), encoding="utf-8")

        (run,) = list(segment_runs(iter_events(build_run_log().lines)))
        report = analyze_run(run, talent_data_path=data_path)
        # the synthetic fixture uses the old bare-paren layout, so it has
        # no decodable talents or gear -- the report simply carries none
        assert all("build" not in p for p in report["players"])

    def test_a_modern_combatant_info_line_produces_a_named_build(self, tmp_path):
        """The real payoff: a modern-layout line yields talents (with the
        choice node settled by a spell the player actually cast in the
        run) and gear with an average item level."""
        from conftest import DPS1, build_run_log
        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.parser import iter_events
        from postmortem.combatlog.segmenter import segment_runs

        data_path = tmp_path / "talents.json"
        data_path.write_text(json.dumps({"nodes": {
            "500": {"options": [{"name": "Solo", "spell_id": 111}]},
            # DPS1 casts Fireball (133) in the synthetic run, so this
            # choice node must resolve to Fireball, not Never Cast
            "501": {"options": [{"name": "Fireball", "spell_id": 133},
                                 {"name": "Never Cast", "spell_id": 999999}]},
            "502": {"options": [{"name": "Invisible A", "spell_id": 900001},
                                 {"name": "Invisible B", "spell_id": 900002}]},
        }}), encoding="utf-8")

        builder = build_run_log()
        stats = ",".join(["1000"] * 21)
        builder.raw(0.6, (
            f"COMBATANT_INFO,{DPS1[0]},0,{stats},63,"
            "[(500,1,1),(501,2,1),(502,3,2)],(0,0,0,0),"
            "[(271510,300,(7991,0,0),(),()),(272228,310,(),(),(240906,295))],"
            "[],11,0,0,0"
        ))
        # raw() appends, which would land after CHALLENGE_MODE_END and so
        # outside the run -- move it up next to the other combatant lines
        builder.lines.insert(2, builder.lines.pop())
        (run,) = list(segment_runs(iter_events(builder.lines)))
        report = analyze_run(run, talent_data_path=data_path)

        (player,) = [p for p in report["players"] if p["guid"] == DPS1[0]]
        talents = player["build"]["talents"]
        assert talents["named_count"] == 2 and talents["ambiguous_count"] == 1
        names = [p["name"] for p in talents["picks"] if p.get("name")]
        assert names == ["Solo", "Fireball"]
        (unsure,) = [p for p in talents["picks"] if p.get("options")]
        assert unsure["options"] == ["Invisible A", "Invisible B"]
        assert unsure["rank"] == 2

        gear = player["build"]["gear"]
        assert gear["average_item_level"] == 305.0  # (300 + 310) / 2
        assert gear["items"][0]["enchants"] == [7991]
        assert gear["items"][1]["gems"] == [240906]

    def test_html_report_renders_the_build(self, tmp_path):
        from conftest import DPS1, build_run_log
        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.parser import iter_events
        from postmortem.combatlog.segmenter import segment_runs
        from postmortem.report.html import render_html

        data_path = tmp_path / "talents.json"
        data_path.write_text(json.dumps({"nodes": {
            "501": {"options": [{"name": "Fireball", "spell_id": 133}]},
        }}), encoding="utf-8")
        builder = build_run_log()
        stats = ",".join(["1000"] * 21)
        builder.raw(0.6, (
            f"COMBATANT_INFO,{DPS1[0]},0,{stats},63,[(501,2,1)],(0,0,0,0),"
            "[(271510,300,(),(),())],[],11,0,0,0"
        ))
        builder.lines.insert(2, builder.lines.pop())
        (run,) = list(segment_runs(iter_events(builder.lines)))
        html = render_html(analyze_run(run, talent_data_path=data_path))
        # the renderer builds rows client-side from embedded JSON, so
        # assert on the data and on the function that renders it
        assert "buildDetail" in html
        assert '"name": "Fireball"' in html
        assert '"average_item_level": 300.0' in html
