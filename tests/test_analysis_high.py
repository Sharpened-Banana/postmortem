"""Two HIGH audit findings in the analysis layer (2026-09-11)."""

from __future__ import annotations

import pytest

from postmortem.analysis.mapping import MIN_SPREAD_RATIO, _spread_ratio, fit_transform


class TestDegenerateAnchorsRejected:
    """ANL-1. With every anchor on one line both orientations reproduce
    them exactly, so each scores RMS 0.0 and the tie falls to list order.
    The residual gate that normally rejects a bad fit sees a perfect one,
    so the check has to happen on the geometry, before fitting."""

    def test_collinear_anchors_are_refused(self):
        pairs = [((float(i), 0.0), (float(i), 0.0)) for i in range(5)]
        assert fit_transform(pairs) is None

    def test_a_mirrored_fit_is_no_longer_accepted_at_zero_residual(self):
        """The concrete reproduction: anchors on wy == 0 whose true mapping
        is (wx, -wy). Both orientations fit them perfectly, and the wrong
        one mirrors every player path and death marker across the line."""
        pairs = [((float(i), 0.0), (float(i), 0.0)) for i in range(5)]
        result = fit_transform(pairs)
        assert result is None, (
            "a fit was accepted from anchors that cannot determine orientation"
        )

    def test_near_collinear_anchors_are_refused_too(self):
        """A corridor dungeon produces these naturally, and there the
        orientation was being decided by a unit of positional noise."""
        pairs = [((float(i * 100), float(i % 2)), (float(i * 100), float(i % 2)))
                 for i in range(6)]
        assert _spread_ratio([p[0] for p in pairs]) < MIN_SPREAD_RATIO
        assert fit_transform(pairs) is None

    def test_well_spread_anchors_still_fit(self):
        """The gate must not cost a legitimate map. This set is genuinely
        reflected, so it also proves reflected fits still work."""
        pairs = [
            ((0.0, 0.0), (0.0, 0.0)),
            ((10.0, 0.0), (10.0, 0.0)),
            ((0.0, 10.0), (0.0, -10.0)),
            ((10.0, 10.0), (10.0, -10.0)),
        ]
        result = fit_transform(pairs)
        assert result is not None, "a perfectly good anchor set was rejected"
        transform, residual = result
        assert residual < 1e-6
        # And an off-anchor point lands on the correct side of the line.
        x, y = transform.apply(5.0, 20.0)
        assert y < 0, "the map came out mirrored"

    @pytest.mark.parametrize("pts,expected", [
        ([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)], 0.0),        # a line
        ([(0.0, 0.0), (0.0, 0.0)], 0.0),                    # coincident
        ([(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)], 1.0),  # a square
    ])
    def test_spread_ratio_measures_what_it_claims(self, pts, expected):
        assert _spread_ratio(pts) == pytest.approx(expected, abs=1e-9)


class TestDeathRecapDoesNotCrossLives:
    """ANL-2. The rolling damage buffer was never reset at death, so a
    player who died, was rezzed and died again reported a biggest hit and a
    recap line from the previous life."""

    def _analyze(self, build):
        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.parser import iter_events
        from postmortem.combatlog.segmenter import segment_runs

        (run,) = list(segment_runs(iter_events(build.lines)))
        return analyze_run(run)

    def test_a_second_death_does_not_inherit_the_first_lifes_biggest_hit(self):
        from conftest import LogBuilder

        b = LogBuilder()
        b.start(0.0)
        npc = b.npc_guid(100, "0001")
        victim = ("Player-1-0001", "Zappyboi", 0x511, "MAGE")
        b.combatant(0.5, victim)
        # A huge hit, then death.
        b.npc_damage(20.0, npc, "Trash", victim, 999, "Doom Bolt", 500_000)
        b.unit_died(21.0, victim[0], victim[1], victim[2])
        # Much later, in a new life, a small hit kills them again.
        b.npc_damage(400.0, npc, "Trash", victim, 998, "Pin Prick", 1_234)
        b.unit_died(401.0, victim[0], victim[1], victim[2])
        b.end(600.0)

        report = self._analyze(b)
        deaths = report["deaths"]
        assert len(deaths) == 2
        second = deaths[-1]
        assert second["biggest_hit"] == 1_234, (
            f"the second death reported {second['biggest_hit']}, which is the "
            "first life's hit"
        )
        assert all(r["spell"] != "Doom Bolt" for r in second["recap"]), (
            "the previous life's hit is still in the recap"
        )

    def test_chip_damage_from_earlier_in_the_same_life_is_trimmed(self):
        from conftest import LogBuilder

        b = LogBuilder()
        b.start(0.0)
        npc = b.npc_guid(100, "0001")
        victim = ("Player-1-0001", "Zappyboi", 0x511, "MAGE")
        b.combatant(0.5, victim)
        b.npc_damage(20.0, npc, "Trash", victim, 999, "Old Slap", 90_000)
        b.npc_damage(100.0, npc, "Trash", victim, 998, "Final Blow", 5_000)
        b.unit_died(100.5, victim[0], victim[1], victim[2])
        b.end(600.0)

        recap = self._analyze(b)["deaths"][0]["recap"]
        assert [r["spell"] for r in recap] == ["Final Blow"]
