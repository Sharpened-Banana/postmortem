"""parse_damage() must pick its field layout by event family, not by count.

A modern SPELL_DAMAGE line carries eleven suffix fields; a modern
SWING_DAMAGE carries ten, because a swing has no trailing spell-type
field. The old `len(s) >= 11` test therefore routed every swing to the
pre-10.x layout and shifted every field after `amount` by one, so
baseAmount was read as overkill and absorbed was read as zero
(2026-09-11).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import HOSTILE, LogBuilder
from postmortem.combatlog.events import parse_damage
from postmortem.combatlog.parser import iter_events

REAL_LOG = Path(__file__).parent / "fixtures" / "real_logs" / "abandoned_key_instance_mismatch.txt"


def _swing_damage_events(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for event in iter_events(fh, base_year=2026):
            if event.name == "SWING_DAMAGE":
                yield parse_damage(event)


class TestAgainstRealSwingLines:
    """The fixture is the ground truth: 767 real swings from a real key."""

    @pytest.mark.skipif(not REAL_LOG.exists(), reason="real-log fixture not present")
    def test_overkill_is_not_fabricated_from_base_amount(self):
        total = sum(d.overkill for d in _swing_damage_events(REAL_LOG))
        # Under the old layout this summed to 64,756,092 -- baseAmount read
        # as overkill, a ~5,600x fabrication surfaced per player and in
        # every death recap.
        assert total == 11_622

    @pytest.mark.skipif(not REAL_LOG.exists(), reason="real-log fixture not present")
    def test_absorbed_melee_damage_is_seen_at_all(self):
        total = sum(d.absorbed for d in _swing_damage_events(REAL_LOG))
        # Read as 0 under the old layout, so stats.py's
        # `dealt = amount + absorbed` silently under-counted melee damage
        # by this much whenever an enemy carried a shield.
        assert total == 3_943_916

    @pytest.mark.skipif(not REAL_LOG.exists(), reason="real-log fixture not present")
    def test_every_swing_is_physical(self):
        """Melee is school 1. The old layout read -1 here (it was looking
        at overkill), which is not a school at all."""
        assert {d.school for d in _swing_damage_events(REAL_LOG)} == {1}


class TestSyntheticSwings:
    def _one_swing(self, **kw):
        b = LogBuilder()
        b.swing_damage(
            1.0, "Creature-0-1-1-1-100-0001", "Trash", HOSTILE,
            "Player-1-0001", "Zappyboi", 0x0511, **kw,
        )
        (event,) = [e for e in iter_events(b.lines) if e.name.startswith("SWING_")]
        return parse_damage(event)

    def test_fields_land_where_they_belong(self):
        d = self._one_swing(amount=5843, base_amount=8345, overkill=0, absorbed=99)
        assert d.amount == 5843
        assert d.base_amount == 8345, "baseAmount was read as something else"
        assert d.overkill == 0, "baseAmount leaked into overkill"
        assert d.absorbed == 99
        assert d.school == 1

    def test_real_overkill_still_comes_through(self):
        """Guard against 'fix' by simply zeroing overkill on swings."""
        d = self._one_swing(amount=5000, base_amount=8000, overkill=1200)
        assert d.overkill == 1200

    def test_swing_damage_landed_uses_the_same_layout(self):
        d = self._one_swing(amount=100, base_amount=250, overkill=0, absorbed=7, landed=True)
        assert (d.base_amount, d.overkill, d.absorbed) == (250, 0, 7)

    def test_a_short_legacy_suffix_still_parses(self):
        """Pre-10.x logs have no baseAmount. They must still work, and are
        now the only thing that reaches the legacy branch."""
        b = LogBuilder()
        b.raw(1.0, 'SWING_DAMAGE,Creature-0-1-1-1-100-0001,"Trash",0x10a48,0x0,'
                   'Player-1-0001,"Zappyboi",0x511,0x0,'
                   '1234,0,1,0,0,0,nil,nil,nil')
        (event,) = [e for e in iter_events(b.lines) if e.name.startswith("SWING_")]
        d = parse_damage(event)
        assert d.amount == 1234 and d.overkill == 0 and d.school == 1


class TestEnvironmentalDamageOffset:
    """Where the advanced block starts for ENVIRONMENTAL_DAMAGE could not
    be settled: no fixture in this repo contains one and no test covered
    one. So detection is made correct under BOTH candidate layouts rather
    than guessing, which a real falling-damage line can only confirm.
    """

    ADVANCED = [
        "Player-1-0001", "0000000000000000", "400000", "500000", "2000",
        "1000", "500", "0", "3", "100", "100", "0", "0", "0",
        "55.50", "-66.25", "2200", "1.57", "80",
    ]
    BASE = ["Environment", "Environment", "0x0", "0x0",
            "Player-1-0001", "Zappyboi", "0x511", "0x0"]
    SUFFIX = ["9999", "9999", "0", "1", "0", "0", "0", "nil", "nil", "nil"]

    def _event(self, type_before_block: bool):
        from postmortem.combatlog.events import Event

        if type_before_block:
            params = self.BASE + ["Falling"] + self.ADVANCED + self.SUFFIX
        else:
            params = self.BASE + self.ADVANCED + ["Falling"] + self.SUFFIX
        return Event(ts=1.0, name="ENVIRONMENTAL_DAMAGE", params=params)

    @pytest.mark.parametrize("type_before_block", [True, False])
    def test_the_advanced_block_is_found_either_way(self, type_before_block):
        from postmortem.combatlog.events import advanced_info

        info = advanced_info(self._event(type_before_block))
        assert info is not None, "the advanced block was not located at all"
        assert info.current_hp == 400_000
        assert (info.pos_x, info.pos_y) == (55.5, -66.25)

    @pytest.mark.parametrize("type_before_block", [True, False])
    def test_the_damage_amount_is_not_read_from_the_advanced_block(self, type_before_block):
        """The failure this guards against: an off-by-one offset makes
        parse_damage() read `level` (80) as the damage amount, so a falling
        death reports nonsense."""
        d = parse_damage(self._event(type_before_block))
        assert d.amount == 9999

    def test_two_guids_are_required_to_call_it_an_advanced_block(self):
        """One position too far along, the first field is ownerGUID -- all
        zeros, which the GUID test accepts. The second field is what
        settles it, so check that requirement directly."""
        from postmortem.combatlog.events import _is_advanced_block

        params = self.BASE + self.ADVANCED + ["Falling"] + self.SUFFIX
        assert _is_advanced_block(params, 8)
        assert not _is_advanced_block(params, 9), (
            "an offset one past the block still looked like a block"
        )


class TestAdvancedBlockInterior:
    """Where inside the advanced block the two 12.x fields sit.

    The block's LENGTH (19) was settled in August against two real lines.
    Their POSITION was not: the parser assumed they followed powerCost,
    which read absorb, powerType, currentPower and maxPower two positions
    early (2026-09-11). Position, map, facing, level and health sit after
    the insertion under either belief, which is why nothing downstream --
    map calibration least of all -- ever noticed.
    """

    @pytest.mark.skipif(not REAL_LOG.exists(), reason="real-log fixture not present")
    def test_advanced_power_fields_match_real_log(self):
        """Two facts hold for every unit in a real log: currentPower never
        exceeds maxPower, and powerType is one of a handful of small ids.
        Read two positions early, 1,850 of the fixture's 11,303 advanced
        lines break one or the other; read correctly, none do."""
        from postmortem.combatlog.events import advanced_info

        seen = impossible = 0
        with open(REAL_LOG, encoding="utf-8", errors="replace") as fh:
            for event in iter_events(fh, base_year=2026):
                if event.name not in ("SPELL_DAMAGE", "SPELL_HEAL"):
                    continue
                info = advanced_info(event)
                if info is None:
                    continue
                seen += 1
                try:
                    power_type = int(info.power_type)
                except ValueError:
                    impossible += 1
                    continue
                if not 0 <= power_type <= 20 or info.current_power > info.max_power:
                    impossible += 1

        assert seen > 10_000, f"only {seen} advanced lines found -- fixture changed?"
        assert impossible == 0

    def test_the_builder_agrees_with_the_parser_about_the_interior(self):
        """The builder used to bake in the parser's own wrong belief, so no
        synthetic test could ever contradict it. Pin the interior here."""
        from postmortem.combatlog.events import advanced_info

        log = LogBuilder()
        log.spell_damage(1.0, "Player-1-1", "Healer", 0x511,
                         LogBuilder.npc_guid(1234, "0001"), "Mob", HOSTILE,
                         42, "Zap", 100)
        events = list(iter_events(iter(log.lines), base_year=2026))
        info = advanced_info(events[0])
        assert info is not None
        assert (info.power_type, info.current_power, info.max_power) == ("3", 100, 100)
        assert info.absorb == 0
