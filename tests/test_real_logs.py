"""Tests against real (anonymized) WoWCombatLog.txt excerpts --
tests/fixtures/real_logs/, not the hand-built LogBuilder() fixtures every
other test file uses. See that directory's README for what's here and why.

These exist for a different purpose than the synthetic unit tests: proving
the real parser/tokenizer handles genuine WoW-formatted data at scale (real
ADVANCED info blocks, COMBATANT_INFO payloads, spell IDs, timestamps, ...),
not re-testing any one specific code path already covered precisely and
quickly by synthetic fixtures elsewhere.
"""

from __future__ import annotations

from pathlib import Path

from postmortem.combatlog.parser import parse_file
from postmortem.combatlog.segmenter import segment_runs

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "real_logs"


class TestAbandonedKeyInstanceMismatch:
    """The real log that surfaced a real segment_runs() bug -- see
    TestMismatchedEndDoesNotCloseAWrongRun in test_combatlog.py for the
    fast synthetic reproduction of the same bug, and this fixture's own
    README for full provenance/context.
    """

    def test_parses_two_runs_with_the_abandoned_one_correctly_flagged(self):
        path = FIXTURES_DIR / "abandoned_key_instance_mismatch.txt"
        runs = list(segment_runs(parse_file(path)))
        assert len(runs) == 2

        voidscar, blinding_vale = runs
        assert voidscar.zone_name == "Voidscar Arena"
        assert voidscar.keystone_level == 10
        assert not voidscar.completed
        assert voidscar.success is None
        assert voidscar.likely_abandoned
        # A real run's worth of actual combat, not a handful of lines --
        # confirms the real parser/tokenizer walked genuine ADVANCED
        # info / COMBATANT_INFO payloads without choking on any of it.
        assert len(voidscar.events) > 30000

        assert blinding_vale.zone_name == "The Blinding Vale"
        assert blinding_vale.keystone_level == 10
        # This fixture only captures Blinding Vale's opening lines (see
        # the README) -- it's real data proving the *transition* parses
        # correctly, not a complete run, so it's correctly still open.
        assert not blinding_vale.completed
