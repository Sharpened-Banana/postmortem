"""Tank death post-mortem: was a defensive ready and unused when the tank
died, and did the group still have an external up.

The distinction these tests are really guarding is the one in
tank_death.py's header: a spell that was *cast at least once* this run is
provably in the player's build, so "ready and unused" is a real finding;
a spell never cast at all is indistinguishable from one that was never
talented, and must never be reported as a mistake.
"""

import pytest
from conftest import FELWYRM, HEALER, LogBuilder, TANK

from postmortem.analysis.pulls import detect_pulls
from postmortem.analysis.stats import compute_stats
from postmortem.analysis.tank_death import (
    READY_MARGIN_S,
    annotate_deaths,
    load_bundled_tank_defensives,
)
from postmortem.combatlog.parser import iter_events
from postmortem.combatlog.segmenter import segment_runs

# Protection Paladin (TANK is spec 66) entries from tank_defensives.json.
ARDENT_DEFENDER = (31850, "Ardent Defender")   # 120s cooldown
SHIELD_OF_THE_RIGHTEOUS = (53600, "Shield of the Righteous")  # resource-gated
DIVINE_PROTECTION = (498, "Divine Protection")  # 60s cooldown
SPIRIT_LINK_TOTEM = (98008, "Spirit Link Totem")  # resto shaman, 180s

BIG_HIT_SPELL = 999001
DEATH_T = 400.0


def _run(build) -> object:
    """Drive a LogBuilder through the real pipeline and return the single
    annotated death."""
    b = LogBuilder()
    npc = b.npc_guid(FELWYRM, "0001")
    b.start(0)
    b.combatant(0.5, TANK)
    b.combatant(0.5, HEALER)
    build(b, npc)
    b.npc_damage(DEATH_T, npc, "Felwyrm", TANK, BIG_HIT_SPELL, "Big Hit",
                 500000, hp=0)
    b.unit_died(DEATH_T + 0.5, TANK[0], TANK[1], TANK[2])
    b.end(DEATH_T + 10)

    (run,) = list(segment_runs(iter_events(b.lines)))
    stats = compute_stats(run.events, detect_pulls(run.events))
    annotate_deaths(stats, load_bundled_tank_defensives(), True)
    (death,) = stats.deaths
    return death


def _names(entries):
    return {e["name"] for e in entries}


class TestAvailableUnused:
    def test_defensive_off_cooldown_at_death_is_reported(self):
        # Ardent Defender (120s) pressed at t=100; by the death at t=400
        # it has been back for minutes. The player demonstrably has it,
        # so this is a real "you had it and didn't press it".
        death = _run(lambda b, npc: b.cast(100, TANK, *ARDENT_DEFENDER))

        tank = death.tank_analysis
        assert tank["scored"] is True
        assert tank["is_tank"] is True
        assert "Ardent Defender" in _names(tank["available_unused"])
        (entry,) = [e for e in tank["available_unused"] if e["spell_id"] == 31850]
        # log ts values are absolute (see LogBuilder._stamp), so this is
        # checked as an offset from the death rather than against t=100.
        assert death.ts - entry["last_used_ts"] == pytest.approx(300.5, abs=0.01)
        # ready_for_s is measured from when the cooldown ended (cast+120),
        # not from the cast itself.
        assert entry["ready_for_s"] == pytest.approx(180.5, abs=0.1)

    def test_defensive_still_on_cooldown_is_not_reported(self):
        # Pressed 30s before death; a 120s cooldown has not recovered, so
        # claiming it was "available" would be flatly wrong.
        death = _run(lambda b, npc: b.cast(DEATH_T - 30, TANK, *ARDENT_DEFENDER))

        tank = death.tank_analysis
        assert "Ardent Defender" not in _names(tank["available_unused"])

    def test_margin_keeps_a_borderline_cooldown_out(self):
        # Cast so that the cooldown ends inside READY_MARGIN_S of the
        # death. Real cooldowns move with talents and haste, so the
        # borderline case is dropped rather than asserted.
        cast_at = (DEATH_T + 0.5) - 120.0 - (READY_MARGIN_S / 2)
        death = _run(lambda b, npc: b.cast(cast_at, TANK, *ARDENT_DEFENDER))

        assert "Ardent Defender" not in _names(death.tank_analysis["available_unused"])

    def test_defensive_active_at_death_is_never_called_unused(self):
        # Divine Protection (8s) cast 4.5s before death was still up when
        # the tank died. It is NOT in gamedata.DEFENSIVES, so the older
        # defensives_used_before_death path does not know about it -- this
        # module has to catch it from its own duration, or it would accuse
        # a tank of sitting on a button they were actively holding.
        death = _run(lambda b, npc: b.cast(DEATH_T - 4, TANK, *DIVINE_PROTECTION))

        assert "Divine Protection" not in {
            u["name"] for u in death.defensives_used_before_death
        }
        assert "Divine Protection" not in _names(
            death.tank_analysis["available_unused"]
        )
        assert "Divine Protection" in _names(death.tank_analysis["active_at_death"])


class TestNeverUsed:
    def test_never_cast_is_an_observation_not_an_accusation(self):
        # Nothing cast all run. Every spec defensive lands in never_used
        # (which the renderers caveat as "may not be talented") and none
        # in available_unused, because the log cannot prove the player
        # actually has any of them.
        death = _run(lambda b, npc: None)

        tank = death.tank_analysis
        assert tank["available_unused"] == []
        assert "Ardent Defender" in _names(tank["never_used"])

    def test_resource_gated_button_is_never_scored_as_unused(self):
        # Shield of the Righteous is Holy Power-gated and carries
        # cooldown_s: 0. Even though it was cast early in the run (so the
        # player provably has it), it must never be claimed as "available",
        # because availability depends on resource the log does not carry.
        death = _run(lambda b, npc: b.cast(100, TANK, *SHIELD_OF_THE_RIGHTEOUS))

        assert "Shield of the Righteous" not in _names(
            death.tank_analysis["available_unused"]
        )


class TestMitigationGap:
    def test_gap_measured_from_last_active_mitigation_press(self):
        death = _run(lambda b, npc: b.cast(DEATH_T - 4, TANK, *SHIELD_OF_THE_RIGHTEOUS))

        # death lands 0.5s after DEATH_T, so the press was 4.5s earlier.
        assert death.tank_analysis["mitigation_gap_s"] == pytest.approx(4.5, abs=0.1)

    def test_no_gap_reported_when_last_press_is_outside_the_window(self):
        # A press five minutes ago says nothing useful about this death.
        death = _run(lambda b, npc: b.cast(100, TANK, *SHIELD_OF_THE_RIGHTEOUS))

        assert death.tank_analysis["mitigation_gap_s"] is None


class TestExternals:
    def test_groupmate_external_off_cooldown_is_reported(self):
        # The healer used Spirit Link Totem (180s) early, so they have it;
        # by the death it is long back up and nobody pressed it.
        death = _run(lambda b, npc: b.cast(100, HEALER, *SPIRIT_LINK_TOTEM))

        externals = death.tank_analysis["externals_available"]
        assert "Spirit Link Totem" in _names(externals)
        assert externals[0]["caster"] == HEALER[1]

    def test_external_never_cast_is_not_assumed(self):
        # The healer never pressed it. They may not have it; silence is
        # not evidence, so nothing is claimed.
        death = _run(lambda b, npc: None)

        assert death.tank_analysis["externals_available"] == []

    def test_external_still_on_cooldown_is_not_reported(self):
        death = _run(lambda b, npc: b.cast(DEATH_T - 20, HEALER, *SPIRIT_LINK_TOTEM))

        assert death.tank_analysis["externals_available"] == []


class TestHonestFallbacks:
    def test_unscored_without_cast_data(self):
        # full_cast_timeline off means cast_timeline is empty, so an empty
        # result proves nothing and the whole section must decline to
        # score rather than report a clean sheet.
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        b.combatant(0.5, TANK)
        b.npc_damage(DEATH_T, npc, "Felwyrm", TANK, BIG_HIT_SPELL, "Big Hit",
                     500000, hp=0)
        b.unit_died(DEATH_T + 0.5, TANK[0], TANK[1], TANK[2])
        b.end(DEATH_T + 10)

        (run,) = list(segment_runs(iter_events(b.lines)))
        stats = compute_stats(run.events, detect_pulls(run.events),
                              full_cast_timeline=False)
        annotate_deaths(stats, load_bundled_tank_defensives(), False)

        (death,) = stats.deaths
        assert death.tank_analysis["scored"] is False
        assert death.tank_analysis["available_unused"] == []

    def test_missing_table_leaves_deaths_untouched(self):
        # load_bundled_tank_defensives() returns None if the packaged file
        # is unreadable; every caller invokes annotate_deaths
        # unconditionally, so that has to be a clean no-op.
        b = LogBuilder()
        npc = b.npc_guid(FELWYRM, "0001")
        b.start(0)
        b.combatant(0.5, TANK)
        b.npc_damage(DEATH_T, npc, "Felwyrm", TANK, BIG_HIT_SPELL, "Big Hit",
                     500000, hp=0)
        b.unit_died(DEATH_T + 0.5, TANK[0], TANK[1], TANK[2])
        b.end(DEATH_T + 10)

        (run,) = list(segment_runs(iter_events(b.lines)))
        stats = compute_stats(run.events, detect_pulls(run.events))
        annotate_deaths(stats, None, True)

        (death,) = stats.deaths
        assert death.tank_analysis is None


class TestTable:
    def test_all_six_tank_specs_are_covered(self):
        data = load_bundled_tank_defensives()
        assert data is not None
        for spec_id in data.tank_spec_ids:
            assert data.for_spec(spec_id), f"spec {spec_id} has no defensives"

    def test_every_tank_spec_has_a_scorable_major(self):
        # A spec whose entries were all resource-gated could never produce
        # a finding, which would silently make the feature useless for it.
        data = load_bundled_tank_defensives()
        for spec_id in data.tank_spec_ids:
            assert any(d.scorable for d in data.for_spec(spec_id)), spec_id
