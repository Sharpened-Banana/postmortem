"""The keystone timer's death penalty: 5s per death, 15s on keys with
Xal'atath's Guile (affix 147, +12 and up). Measured 2026-10-17 over 78
completed keys from real logs: in-game time - wall time - deaths x
penalty sat at -8.4s (the start countdown) for 73 of them with this rule;
the old flat 15s overcharged every low-key death by 10s."""

from __future__ import annotations

import pytest

from conftest import LogBuilder, TANK

from postmortem.analysis.run_analyzer import (
    analyze_run,
    apply_timed_verdict,
    death_penalty_for,
)
from postmortem.combatlog.parser import iter_events
from postmortem.combatlog.segmenter import segment_runs


@pytest.mark.parametrize("affixes, expected", [
    ([9, 10, 147], 15.0),
    ([147], 15.0),
    ([9, 10, 160], 5.0),
    ([], 5.0),
    (None, 5.0),
    ("147", 5.0),
    ([True, "x", None], 5.0),
])
def test_penalty_by_affix(affixes, expected):
    assert death_penalty_for(affixes) == expected


def _one_death_run(affixes: str) -> dict:
    b = LogBuilder()
    npc = b.npc_guid(236085, "0001")
    b.raw(0, f'CHALLENGE_MODE_START,"Murder Row",2830,587,10,[{affixes}]')
    b.combatant(0.5, TANK)
    b.npc_damage(50, npc, "Felwyrm", TANK, 900001, "Big Hit", 500000, hp=0)
    b.unit_died(50.5, TANK[0], TANK[1], TANK[2])
    b.end(100)
    (seg,) = list(segment_runs(iter_events(b.lines)))
    return analyze_run(seg)


def test_a_key_without_guile_charges_5s():
    cost = _one_death_run("9,10,160")["death_cost"]
    assert cost["per_death_s"] == 5.0
    assert cost["total_s"] == 5.0


def test_a_guile_key_charges_15s():
    assert _one_death_run("9,10,147")["death_cost"]["per_death_s"] == 15.0


def test_an_explicit_penalty_still_wins():
    b = LogBuilder()
    b.raw(0, 'CHALLENGE_MODE_START,"Murder Row",2830,587,10,[9,10]')
    b.end(100)
    (seg,) = list(segment_runs(iter_events(b.lines)))
    assert analyze_run(seg, death_penalty_s=20.0)["death_cost"]["per_death_s"] == 20.0


def _stored(affixes, per_death, deaths=3):
    return {"run": {"completed": False, "affixes": affixes},
            "deaths": [{"t": 1.0}] * deaths,
            "death_cost": {"deaths": deaths, "per_death_s": per_death,
                           "total_s": deaths * per_death}}


def test_stored_reports_with_the_old_flat_15s_are_corrected_on_read():
    r = apply_timed_verdict(_stored([9, 10], 15.0))
    assert r["death_cost"]["per_death_s"] == 5.0
    assert r["death_cost"]["total_s"] == 15.0


def test_stored_guile_reports_keep_15s():
    r = apply_timed_verdict(_stored([9, 10, 147], 15.0))
    assert r["death_cost"]["per_death_s"] == 15.0


def test_a_custom_stored_penalty_is_left_alone():
    r = apply_timed_verdict(_stored([9, 10], 20.0))
    assert r["death_cost"]["per_death_s"] == 20.0


def test_a_report_without_affixes_is_left_alone():
    r = apply_timed_verdict(_stored(None, 15.0))
    assert r["death_cost"]["per_death_s"] == 15.0
