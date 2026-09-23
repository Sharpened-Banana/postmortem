"""An enemy that resets and is engaged again later is two engagements.

One engagement per GUID used to span first touch to death, so a mob that
evaded and was killed pulls later merged every pull in between into one.
"""

from conftest import DPS1, HOSTILE, LogBuilder

from postmortem.analysis.pulls import collect_engagements, detect_pulls
from postmortem.combatlog.parser import iter_events

SLASH = (2139, "Fireball")


def _hit(b, t, guid, name="Trash"):
    b.player_damage(t, DPS1, guid, name, *SLASH, 1000)


def _kill(b, t, guid, name="Trash"):
    _hit(b, t, guid, name)
    b.unit_died(t + 0.1, guid, name, HOSTILE)


def _pulls(b):
    b.lines.sort(key=lambda line: line.split("  ", 1)[0])
    return detect_pulls(list(iter_events(b.lines)))


class TestEvadedEnemy:
    def test_reset_enemy_does_not_fuse_the_pulls_in_between(self):
        b = LogBuilder()
        runner = b.npc_guid(1001, "0001")
        pack_b = b.npc_guid(1002, "0002")
        pack_c = b.npc_guid(1003, "0003")
        # the runner is hit, then evades and is left alone
        for t in (10, 11, 12, 13):
            _hit(b, t, runner, "Runner")
        # an unrelated pack, fought and killed well after
        for t in (60, 62, 64):
            _hit(b, t, pack_b)
        _kill(b, 66, pack_b)
        # the runner is re-pulled with the next pack and dies there
        for t in (120, 122):
            _hit(b, t, pack_c)
            _hit(b, t, runner, "Runner")
        _kill(b, 124, pack_c)
        _kill(b, 125, runner, "Runner")

        pulls = _pulls(b)
        assert len(pulls) == 3, [p.summary() for p in pulls]
        assert [len(p.units) for p in pulls] == [1, 1, 2]
        assert pulls[0].units[0].guid == runner and not pulls[0].units[0].killed
        assert any(u.guid == runner and u.killed for u in pulls[2].units)

    def test_engagements_are_keyed_per_reset(self):
        b = LogBuilder()
        mob = b.npc_guid(1001, "0001")
        _hit(b, 10, mob)
        _hit(b, 12, mob)
        _kill(b, 100, mob)
        engs = collect_engagements(iter_events(b.lines))
        assert list(engs) == [mob, f"{mob}#2"]
        assert [e.guid for e in engs.values()] == [mob, mob]
        assert engs[mob].died_at is None and engs[f"{mob}#2"].killed

    def test_a_long_cc_inside_one_pull_stays_one_unit(self):
        """40s untouched (sheeped) while the rest of the pack is fought:
        the split engagement comes back inside the same pull and is
        re-joined, not counted as a second mob."""
        b = LogBuilder()
        sheeped = b.npc_guid(1001, "0001")
        tank_mob = b.npc_guid(1002, "0002")
        _hit(b, 10, sheeped)
        _hit(b, 11, sheeped)
        for t in range(10, 56, 1):
            _hit(b, t, tank_mob)
        _kill(b, 55, tank_mob)
        _kill(b, 55.5, sheeped)
        (pull,) = _pulls(b)
        assert len(pull.units) == 2
        assert all(u.killed for u in pull.units)


class TestWipe:
    def test_a_wiped_boss_repull_is_its_own_pull(self):
        b = LogBuilder()
        boss = b.npc_guid(2001, "0001")
        b.encounter_start(100)
        for t in range(100, 111):
            _hit(b, t, boss, "Big Boss")
        b.encounter_end(111, success=0)
        b.encounter_start(115)
        for t in range(115, 140):
            _hit(b, t, boss, "Big Boss")
        _kill(b, 140, boss, "Big Boss")
        b.encounter_end(141, success=1)

        pulls = _pulls(b)
        assert len(pulls) == 2, [p.summary() for p in pulls]
        assert [p.encounter_name for p in pulls] == ["Big Boss", "Big Boss"]
        assert not pulls[0].units[0].killed and pulls[1].units[0].killed

    def test_a_silent_intermission_does_not_split_the_boss(self):
        """No interaction for 40s while the encounter is open (boss
        untargetable) is still one engagement."""
        b = LogBuilder()
        boss = b.npc_guid(2001, "0001")
        b.encounter_start(100)
        _hit(b, 100, boss, "Big Boss")
        _hit(b, 101, boss, "Big Boss")
        _kill(b, 145, boss, "Big Boss")
        b.encounter_end(146, success=1)
        engs = collect_engagements(iter_events(b.lines))
        assert len(engs) == 1
