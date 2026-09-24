"""Melee hits read the victim's HP from SWING_DAMAGE_LANDED.

In a real log a melee hit is two lines: SWING_DAMAGE, whose advanced block
describes the ATTACKER, then SWING_DAMAGE_LANDED, whose block describes the
VICTIM. Everything that wants the victim's health or position off a hit
(close calls, the death recap's hp_after, the movement track, the snapshot
tank HP series) used to read only SWING_DAMAGE and so never saw a melee hit
at all. The synthetic LogBuilder gave SWING_DAMAGE the victim's block, the
same wrong model, which is why no test noticed.
"""

from pathlib import Path

import pytest
from conftest import FELWYRM, HOSTILE, TANK, LogBuilder

from postmortem.analysis.pulls import detect_pulls
from postmortem.analysis.snapshot import _scan
from postmortem.analysis.stats import compute_stats
from postmortem.combatlog.events import advanced_info
from postmortem.combatlog.parser import iter_events, parse_file
from postmortem.combatlog.segmenter import segment_runs

REAL_LOG = Path(__file__).parent / "fixtures" / "real_logs" / "abandoned_key_instance_mismatch.txt"

# Lines 76 and 78 of the real fixture, verbatim after the timestamp: a
# "Lost Sethrak" melee hit of 108,313 on a player left at 966,107 HP.
SETHRAK = "Creature-0-3778-2923-117308-243996-0002150BD5"
VICTIM = "Player-162-0C1ECF61"
REAL_SWING = (
    'SWING_DAMAGE,Creature-0-3778-2923-117308-243996-0002150BD5,"Lost Sethrak",0xa48,'
    '0x80000000,Player-162-0C1ECF61,"Player10-TestRealm-US",0x511,0x80000000,'
    'Creature-0-3778-2923-117308-243996-0002150BD5,0000000000000000,6437583,6444997,'
    '0,0,1470,0,0,0,1,0,0,0,4457.29,-417.74,2574,0.3389,90,108313,281297,-1,1,0,0,'
    '3797,nil,nil,nil'
)
REAL_LANDED = (
    'SWING_DAMAGE_LANDED,Creature-0-3778-2923-117308-243996-0002150BD5,"Lost Sethrak",'
    '0xa48,0x80000000,Player-162-0C1ECF61,"Player10-TestRealm-US",0x511,0x80000000,'
    'Player-162-0C1ECF61,0000000000000000,966107,1074420,3095,341,6679,279,0,0,6,130,'
    '1250,0,4461.03,-423.09,2574,0.6074,304,108313,281297,-1,1,0,0,3797,nil,nil,nil'
)


def _stats(b: LogBuilder):
    (run,) = list(segment_runs(iter_events(b.lines)))
    return compute_stats(run.events, detect_pulls(run.events))


class TestTheRealLogShape:
    def test_every_melee_line_in_the_fixture_has_the_documented_owner(self):
        """Pins the premise: SWING_DAMAGE's block is the attacker's and
        SWING_DAMAGE_LANDED's the victim's, for every line in the fixture."""
        owners = {"SWING_DAMAGE": set(), "SWING_DAMAGE_LANDED": set()}
        for ev in parse_file(REAL_LOG):
            if ev.name in owners:
                adv = advanced_info(ev)
                owners[ev.name].add(
                    "src" if adv.info_guid == ev.source_guid
                    else "dst" if adv.info_guid == ev.dest_guid else "other")
        assert owners == {"SWING_DAMAGE": {"src"}, "SWING_DAMAGE_LANDED": {"dst"}}

    def test_builder_matches_the_real_shape(self):
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.melee_hit(1.0, npc, "Felwyrm", HOSTILE, TANK[0], TANK[1], TANK[2], 5000)
        swing, landed = iter_events(b.lines)
        assert (swing.name, advanced_info(swing).info_guid) == ("SWING_DAMAGE", npc)
        assert (landed.name, advanced_info(landed).info_guid) == (
            "SWING_DAMAGE_LANDED", TANK[0])


class TestStatsReadsTheVictimOffLanded:
    def test_real_pair_fills_recap_hp_after_and_counts_damage_once(self):
        b = LogBuilder()
        b.start(0)
        b.raw(5.0, REAL_SWING)
        b.raw(5.0, REAL_LANDED)
        b.unit_died(5.5, VICTIM, "Player10-TestRealm-US", 0x511)
        b.end(20)

        stats = _stats(b)
        (death,) = stats.deaths
        (hit,) = death.recap
        assert hit["spell"] == "Melee"
        assert hit["hp_after"] == 966107
        # the LANDED line repeats the damage and must not total it again
        assert stats.players[VICTIM].damage_taken == 108313
        # the victim's own position came off the LANDED block
        assert stats.position_samples[VICTIM][0][1:3] == [4461.0, -423.1]

    def test_a_melee_hit_into_danger_is_a_close_call(self):
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        b.combatant(0.5, TANK)
        b.melee_hit(5, npc, "Felwyrm", HOSTILE, TANK[0], TANK[1], TANK[2],
                    60000, attacker_hp=900000, victim_hp=150000)  # 15%
        b.end(20)

        stats = _stats(b)
        (cc,) = stats.close_calls
        assert cc["spell"] == "Melee"
        assert cc["hp_pct"] == pytest.approx(15.0, abs=0.1)
        assert cc["source"] == "Felwyrm"
        assert cc["amount"] == 60000

    def test_attacker_block_on_swing_damage_is_not_read_as_the_victims(self):
        """A healthy attacker's HP on SWING_DAMAGE must not become the
        victim's reading."""
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        b.combatant(0.5, TANK)
        b.swing_damage(5, npc, "Felwyrm", HOSTILE, TANK[0], TANK[1], TANK[2],
                       60000, hp=100000)  # attacker at 10%, alone
        b.end(20)
        assert _stats(b).close_calls == []


class TestSnapshotTankSeriesSeesMelee:
    def test_landed_feeds_the_hp_series_and_the_hit(self):
        events = list(iter_events([
            f"8/30/2026 22:06:40.132-7  {REAL_SWING}",
            f"8/30/2026 22:06:40.132-7  {REAL_LANDED}",
        ]))
        scan = _scan(events, events[0].ts - 1, events[-1].ts + 1)
        assert len(scan.hits) == 1, "LANDED must not be a second hit"
        assert scan.hp[VICTIM] == [(events[1].ts, pytest.approx(100 * 966107 / 1074420))]
        assert scan.hits[0]["hp_pct"] == pytest.approx(89.9, abs=0.05)
