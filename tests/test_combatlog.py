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
