"""Combat log parsing: tokenizer, event accessors, segmentation."""

from conftest import DPS1, HOSTILE, TANK, LogBuilder, build_run_log

from postmortem.combatlog.events import (
    advanced_info,
    is_group_player,
    is_hostile_npc,
    parse_damage,
    parse_heal,
    spell_info,
)
from postmortem.combatlog.guid import parse_guid
from postmortem.combatlog.parser import iter_events, parse_line, split_params
from postmortem.combatlog.segmenter import segment_runs


class TestSplitParams:
    def test_plain(self):
        assert split_params("a,b,c") == ["a", "b", "c"]

    def test_quoted_comma(self):
        assert split_params('x,"Foo, the Bar",y') == ["x", '"Foo, the Bar"', "y"]

    def test_brackets(self):
        assert split_params("a,[1,2,3],b") == ["a", "[1,2,3]", "b"]

    def test_nested_and_parens(self):
        assert split_params("g,(1,2),[(3,4),[5,6]],z") == \
            ["g", "(1,2)", "[(3,4),[5,6]]", "z"]


class TestTimestamps:
    def test_new_format(self):
        e = parse_line("8/30/2026 20:01:02.500-4  SPELL_CAST_SUCCESS,a,b,c")
        assert e is not None
        assert e.name == "SPELL_CAST_SUCCESS"
        assert e.utc_offset == "-4"

    def test_old_format_no_year(self):
        events = list(iter_events([
            "12/31 23:59:59.900  SWING_DAMAGE,a,b,c",
            "1/1 00:00:01.000  SWING_DAMAGE,a,b,c",
        ], base_year=2025))
        assert len(events) == 2
        # year must roll over so time still moves forward
        assert events[1].ts > events[0].ts

    def test_garbage_line(self):
        assert parse_line("not a combat log line") is None
        assert parse_line("") is None


class TestEventAccessors:
    def _damage_event(self):
        b = LogBuilder()
        b.player_damage(10, DPS1, b.npc_guid(236085, "000A"), "Felwyrm",
                        133, "Fireball", 50000, overkill=100, crit=True)
        (e,) = list(iter_events(b.lines))
        return e

    def test_damage(self):
        e = self._damage_event()
        assert is_group_player(e.source_flags)
        assert is_hostile_npc(e.dest_flags)
        sp = spell_info(e)
        assert sp.spell_id == 133 and sp.spell_name == "Fireball"
        d = parse_damage(e)
        assert d.amount == 50000 and d.overkill == 100 and d.critical

    def test_advanced_block(self):
        e = self._damage_event()
        adv = advanced_info(e)
        assert adv is not None
        assert adv.info_guid == e.dest_guid
        assert adv.max_hp == 1000000
        assert adv.pos_x == 100.0 and adv.pos_y == -200.0
        assert adv.ui_map_id == 2200

    def test_heal(self):
        from conftest import HEALER
        b = LogBuilder()
        b.heal(5, HEALER, TANK, 8004, "Healing Surge", 30000, overheal=12000)
        (e,) = list(iter_events(b.lines))
        h = parse_heal(e)
        assert h.amount == 30000 and h.overhealing == 12000
        assert h.effective == 18000

    def test_guid(self):
        g = parse_guid("Creature-0-1465-2830-12345-236085-000A")
        assert g.is_npc and g.npc_id == 236085 and g.spawn_uid == "000A"
        p = parse_guid("Player-1403-0000000A")
        assert p.is_player and p.npc_id is None
        z = parse_guid("0000000000000000")
        assert z.is_none


class TestSegmenter:
    def test_full_run(self):
        events = list(iter_events(build_run_log().lines))
        runs = list(segment_runs(events))
        assert len(runs) == 1
        run = runs[0]
        assert run.zone_name == "Murder Row"
        assert run.challenge_map_id == 587
        assert run.keystone_level == 10
        assert run.affixes == [160, 9, 10]
        assert run.completed and run.success
        assert run.duration_ms == 600000
        assert run.wall_duration == 140.0
        assert not run.likely_abandoned

    def test_abandoned_run(self):
        b = LogBuilder()
        b.start(0)
        b.player_damage(5, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        b.start(100, zone="Other Zone", cm=250, lvl=12)
        b.end(150, instance=2830)
        runs = list(segment_runs(iter_events(b.lines)))
        assert len(runs) == 2
        assert not runs[0].completed
        assert runs[1].completed

    def test_reload_same_key_merges(self):
        b = LogBuilder()
        b.start(0)
        b.player_damage(5, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        b.start(50)  # /reload re-logs the same key
        b.end(100)
        runs = list(segment_runs(iter_events(b.lines)))
        assert len(runs) == 1
        assert runs[0].completed

    def test_max_run_events_truncates_an_oversized_run(self):
        """A single continuous run that grows past max_run_events is cut
        off and yielded early instead of accumulating unboundedly -- the
        real fix for postmortem_site's per-run memory ceiling (a first
        attempt at this wrongly capped the whole *upload's* byte size
        instead, which punished perfectly safe multi-key logs)."""
        b = LogBuilder()
        b.start(0)
        for i in range(20):
            b.player_damage(i + 1, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        b.end(100)
        runs = list(segment_runs(iter_events(b.lines), max_run_events=5))
        assert len(runs) == 1
        run = runs[0]
        assert run.truncated
        assert not run.completed
        assert len(run.events) == 5

    def test_max_run_events_does_not_affect_a_run_under_the_cap(self):
        events = list(iter_events(build_run_log().lines))
        runs = list(segment_runs(events, max_run_events=1_000_000))
        assert len(runs) == 1
        assert runs[0].completed
        assert not runs[0].truncated

    def test_run_after_a_truncated_one_is_still_segmented_normally(self):
        b = LogBuilder()
        b.start(0)
        for i in range(20):
            b.player_damage(i + 1, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        b.end(100)  # dropped -- current is None by the time this arrives
        b.start(200, zone="Other Zone", cm=250, lvl=12)
        b.end(250, instance=2830)
        runs = list(segment_runs(iter_events(b.lines), max_run_events=5))
        assert len(runs) == 2
        assert runs[0].truncated and not runs[0].completed
        assert runs[1].completed and not runs[1].truncated


class TestLikelyAbandoned:
    """RunSegment.likely_abandoned: best-effort inference for a run with
    no CHALLENGE_MODE_END. Built from a real report (2026-09-01): a
    genuinely abandoned key's real log had no CHALLENGE_MODE_END at all,
    but ended with the group leaving the instance (one player hearthing
    out, another taking fall damage at open-world coordinates) -- exactly
    the kind of departure a ZONE_CHANGE to a different zone captures.
    """

    def test_zone_change_out_of_the_instance_flags_it(self):
        b = LogBuilder()
        b.start(0)  # default instance=2830
        b.player_damage(5, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        b.raw(20, 'ZONE_CHANGE,1519,"Stormwind City",1')
        (run,) = list(segment_runs(iter_events(b.lines)))
        assert not run.completed
        assert run.likely_abandoned

    def test_no_zone_change_is_not_flagged(self):
        # log just stops (crash, still in progress, cut off) -- no signal
        # either way, so this stays an honest "don't know", not a guess.
        b = LogBuilder()
        b.start(0)
        b.player_damage(5, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        (run,) = list(segment_runs(iter_events(b.lines)))
        assert not run.completed
        assert not run.likely_abandoned

    def test_zone_change_to_the_same_instance_is_not_flagged(self):
        # e.g. a multi-floor dungeon's own internal transition that still
        # carries the instance's own zone id -- not a real departure, and
        # must not false-positive on an ordinary floor change mid-key.
        b = LogBuilder()
        b.start(0)  # default instance=2830
        b.player_damage(5, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        b.raw(20, 'ZONE_CHANGE,2830,"Murder Row",8')
        (run,) = list(segment_runs(iter_events(b.lines)))
        assert not run.completed
        assert not run.likely_abandoned


class TestMismatchedEndDoesNotCloseAWrongRun:
    """Real bug (2026-09-01), found via real reported combat logs -- not
    a hypothetical: WoW fires a CHALLENGE_MODE_END with all-zero stats
    ("...END,<id>,0,0,0,0.000000,0.000000") immediately before *every*
    single CHALLENGE_MODE_START, always carrying that upcoming key's own
    instance id -- confirmed against 13 real keys across 7 real logs
    from one account, 100% consistent. Harmless in the ordinary case (no
    run is open between keys, so segment_runs()'s own `current is None`
    check already ignores it) -- but when the previous key was genuinely
    abandoned (never got its own real END) and a new key starts right
    after, this phantom event's instance doesn't match the still-open
    run at all. The old code closed the run with it anyway, reporting a
    genuinely abandoned key as a false "depleted" completion
    (completed=True, success=False) instead of correctly leaving it open
    for the next START's own "different key" branch to yield it as
    abandoned.
    """

    def test_phantom_end_with_a_different_instance_does_not_close_the_open_run(self):
        b = LogBuilder()
        b.start(0, zone="Voidscar Arena", instance=2923, cm=585, lvl=10)
        b.player_damage(5, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        # the real, always-zeroed phantom event -- instance 2859 belongs
        # to the *next* key below, not this one.
        b.raw(20, "CHALLENGE_MODE_END,2859,0,0,0,0.000000,0.000000")
        b.start(21, zone="The Blinding Vale", instance=2859, cm=584, lvl=10)
        b.end(600, instance=2859, lvl=10, ms=580000)

        runs = list(segment_runs(iter_events(b.lines)))
        assert len(runs) == 2
        assert not runs[0].completed  # correctly abandoned, not falsely "failed"
        assert runs[0].success is None
        assert runs[1].completed and runs[1].success

    def test_a_real_end_with_a_matching_instance_still_closes_normally(self):
        # Regression guard: the instance-match check must not break the
        # ordinary, correct case (every existing fixture already relies
        # on this, but this makes the specific behavior explicit).
        b = LogBuilder()
        b.start(0)  # default instance=2830
        b.player_damage(5, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        b.end(60)  # default instance=2830, matches
        (run,) = list(segment_runs(iter_events(b.lines)))
        assert run.completed and run.success

    def test_phantom_end_with_the_SAME_instance_still_does_not_close(self):
        # The gap the first instance-only fix (commit e32b32c) missed,
        # found by a later debug sweep: an abandoned key immediately
        # followed by another key in the *same dungeon* (a re-run after
        # depleting, or someone else's key of the same map). The phantom
        # END's instance then matches the abandoned run's own instance,
        # so an instance-mismatch check alone wouldn't fire -- but the
        # phantom's totalTimeMs is still 0, which is what actually
        # identifies it.
        b = LogBuilder()
        b.start(0, zone="Altar of Fangs", instance=2993, cm=588, lvl=10)
        b.player_damage(5, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        b.raw(20, "CHALLENGE_MODE_END,2993,0,0,0,0.000000,0.000000")  # phantom, same instance
        b.start(21, zone="Altar of Fangs", instance=2993, cm=588, lvl=10)  # re-run, same dungeon
        b.end(600, instance=2993, lvl=10, ms=580000)

        runs = list(segment_runs(iter_events(b.lines)))
        assert len(runs) == 2
        assert not runs[0].completed  # abandoned, not a false depleted completion
        assert runs[0].success is None
        # (likely_abandoned is a separate ZONE_CHANGE-based heuristic, not
        # exercised here -- this test is about the phantom not falsely
        # closing the run.)
        assert runs[1].completed and runs[1].success

    def test_a_real_depletion_still_closes_as_completed_but_not_timed(self):
        # The phantom signal is totalTimeMs==0, NOT success==0 -- a real
        # depleted (completed-but-not-timed) key has success=0 with a
        # genuinely nonzero totalTimeMs, and must still be reported as a
        # real completion, not left open as if abandoned.
        b = LogBuilder()
        b.start(0)  # default instance=2830
        b.player_damage(5, DPS1, b.npc_guid(1, "1"), "X", 1, "S", 10)
        b.raw(60, "CHALLENGE_MODE_END,2830,0,10,1800000,0.000000,0.000000")  # depleted: not timed, real duration
        (run,) = list(segment_runs(iter_events(b.lines)))
        assert run.completed
        assert run.success is False
        assert run.duration_ms == 1800000
