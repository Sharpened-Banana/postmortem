"""Learning interruptibility from real logs (analysis/interrupt_learning.py).

The two signals are asymmetric on purpose: a landed interrupt is proof a
spell is interruptible, while "an interrupt was used on it mid-cast and
nothing happened, repeatedly, and it has never once been interrupted" is
strong evidence it is not. These pin down both, plus the cases that must
NOT produce a verdict.
"""

from __future__ import annotations

import json

import pytest
from conftest import DPS1, LogBuilder

from postmortem.analysis.interrupt_learning import (
    DEFAULT_MIN_ATTEMPTS,
    InterruptObservations,
    observe_events,
    update_from_events,
)
from postmortem.combatlog.parser import iter_events

MOB = LogBuilder.npc_guid(555000, "0001")
KICK, KICK_NAME = 2139, "Counterspell"


def _events(build):
    b = LogBuilder()
    b.start(0)
    b.combatant(0.5, DPS1)
    build(b)
    b.end(300)
    return list(iter_events(b.text().splitlines()))


def _kick_attempt(b, t, target=MOB):
    """A player using an interrupt ability on `target` (no interrupt follows
    unless the caller also logs one)."""
    guid, name, flags, _ = DPS1
    adv = LogBuilder._advanced(guid)
    b.raw(t, f'SPELL_CAST_SUCCESS,{guid},"{name}",{flags:#06x},0x0,'
             f'{target},"Test Mob",0x0a48,0x0,{KICK},"{KICK_NAME}",0x1,{adv}')


class TestPositiveSignal:
    def test_a_landed_interrupt_proves_interruptible(self):
        def build(b):
            b.npc_cast_start(10, MOB, "Test Mob", 900800, "Kickable Bolt")
            b.interrupt(11, DPS1, MOB, "Test Mob", KICK, KICK_NAME, 900800, "Kickable Bolt")
        obs = observe_events(_events(build))
        assert obs.spells[900800]["interrupted"] == 1
        assert obs.to_interrupt_data()["spells"]["900800"]["interruptible"] is True


class TestNegativeSignal:
    def _survived(self, attempts):
        def build(b):
            t = 10.0
            for _ in range(attempts):
                b.npc_cast_start(t, MOB, "Test Mob", 900801, "Immune Bolt")
                _kick_attempt(b, t + 0.5)
                b.npc_cast_success(t + 1.0, MOB, "Test Mob", 900801, "Immune Bolt")
                t += 10
        return observe_events(_events(build))

    def test_repeated_failed_attempts_mark_it_uninterruptible(self):
        obs = self._survived(DEFAULT_MIN_ATTEMPTS)
        assert obs.spells[900801]["survived_attempts"] == DEFAULT_MIN_ATTEMPTS
        assert obs.spells[900801]["interrupted"] == 0
        assert obs.to_interrupt_data()["spells"]["900801"]["interruptible"] is False

    def test_too_few_attempts_yields_no_verdict(self):
        obs = self._survived(DEFAULT_MIN_ATTEMPTS - 1)
        assert "900801" not in obs.to_interrupt_data()["spells"]

    def test_one_success_outweighs_many_failures(self):
        """Two players kicking the same cast means one 'fails' every time.
        Requiring zero successes is what keeps that from ever reading as
        uninterruptible."""
        def build(b):
            t = 10.0
            for _ in range(6):
                b.npc_cast_start(t, MOB, "Test Mob", 900802, "Contested Bolt")
                _kick_attempt(b, t + 0.5)
                b.npc_cast_success(t + 1.0, MOB, "Test Mob", 900802, "Contested Bolt")
                t += 10
            b.npc_cast_start(t, MOB, "Test Mob", 900802, "Contested Bolt")
            b.interrupt(t + 0.5, DPS1, MOB, "Test Mob", KICK, KICK_NAME,
                        900802, "Contested Bolt")
        obs = observe_events(_events(build))
        assert obs.spells[900802]["interrupted"] == 1
        assert obs.to_interrupt_data()["spells"]["900802"]["interruptible"] is True

    def test_kicking_a_mob_that_isnt_casting_proves_nothing(self):
        def build(b):
            for t in (10, 20, 30, 40):
                _kick_attempt(b, t)
        obs = observe_events(_events(build))
        assert obs.to_interrupt_data()["spells"] == {}


class TestAccumulation:
    def test_merge_adds_evidence_across_runs(self):
        a = InterruptObservations()
        a.add_survived(1, "Spell One")
        b = InterruptObservations()
        b.add_survived(1, "Spell One", 2)
        b.add_interrupted(2, "Spell Two")
        a.merge(b)
        assert a.spells[1]["survived_attempts"] == 3
        assert a.spells[2]["interrupted"] == 1

    def test_round_trips_through_disk(self, tmp_path):
        obs = InterruptObservations()
        obs.add_interrupted(7, "Kickable")
        obs.add_survived(8, "Immune", 4)
        path = tmp_path / "learned.json"
        obs.save(path)
        again = InterruptObservations.load(path)
        assert again.spells == obs.spells

    def test_missing_file_loads_empty_rather_than_raising(self, tmp_path):
        assert InterruptObservations.load(tmp_path / "nope.json").spells == {}

    def test_corrupt_file_loads_empty_rather_than_raising(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        assert InterruptObservations.load(path).spells == {}

    def test_update_from_events_accumulates_on_disk(self, tmp_path):
        path = tmp_path / "learned.json"

        def build(b):
            b.npc_cast_start(10, MOB, "Test Mob", 900803, "Kickable Bolt")
            b.interrupt(11, DPS1, MOB, "Test Mob", KICK, KICK_NAME, 900803, "Kickable Bolt")

        events = _events(build)
        update_from_events(events, path)
        merged = update_from_events(events, path)   # same run folded in twice
        assert merged.spells[900803]["interrupted"] == 2
        assert json.loads(path.read_text())["spells"]["900803"]["interrupted"] == 2


class TestThreshold:
    def test_raising_min_attempts_trades_coverage_for_confidence(self):
        obs = InterruptObservations()
        obs.add_survived(1, "Weak Evidence", 3)
        obs.add_survived(2, "Strong Evidence", 20)
        lenient = obs.to_interrupt_data(min_attempts=3)["spells"]
        strict = obs.to_interrupt_data(min_attempts=10)["spells"]
        assert set(lenient) == {"1", "2"}
        assert set(strict) == {"2"}
