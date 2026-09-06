"""analysis/spell_damage.py, its use as the kick-value fallback in
stats.py/run_analyzer.py, and the Warcraft Logs client + build command
that produce the bundled community file (wcl.py, cli.py) -- the latter
against a canned transport, never the network."""

import json

from conftest import DPS1, TANK, LogBuilder, build_run_log

from postmortem import bundled as bundled_module
from postmortem.analysis.run_analyzer import analyze_run
from postmortem.analysis.spell_damage import SpellDamageData, observe_stats, update_from_stats
from postmortem.cli import aggregate_spell_damage_samples, main
from postmortem.combatlog.parser import parse_file
from postmortem.combatlog.segmenter import segment_runs
from postmortem.wcl import (
    API_URL, TOKEN_URL, WCLClient, WCLError, fetch_fight_tables, find_mplus_zone,
    iter_keystone_fights,
)


class TestSpellDamageData:
    def test_totals_merge_and_average(self):
        d = SpellDamageData()
        d.add(1, "Bolt", 10, damage=300, casts=3)
        d.add(1, "Bolt", 10, damage=100, casts=1)
        assert d.estimate(1, 10) == {"damage": 100, "healing": 0, "level": 10, "casts": 4}

    def test_nearest_level_ties_go_lower(self):
        d = SpellDamageData()
        d.add(1, "Bolt", 9, damage=90, casts=1)
        d.add(1, "Bolt", 11, damage=110, casts=1)
        assert d.estimate(1, 10)["level"] == 9      # tie -> lower, never over-credit
        assert d.estimate(1, 12)["level"] == 11
        assert d.estimate(1, None)["level"] == 9    # unknown level -> nearest to 0
        assert d.estimate(2, 10) is None

    def test_zero_casts_never_added(self):
        d = SpellDamageData()
        d.add(1, "Bolt", 10, damage=500, casts=0)
        assert not d

    def test_round_trip_and_tolerant_load(self, tmp_path):
        d = SpellDamageData(source="x", season="s", version="v")
        d.add(1, "Bolt", 10, damage=300, healing=0, casts=3)
        d.save(tmp_path / "f.json", comment="hi")
        back = SpellDamageData.load(tmp_path / "f.json")
        assert back.to_dict() == d.to_dict()
        assert back.source == "x"
        # a missing or corrupt file is an empty dataset, not an error
        assert not SpellDamageData.load(tmp_path / "nope.json")
        (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
        assert not SpellDamageData.load(tmp_path / "bad.json")
        # junk entries are skipped, good ones kept
        weird = {"spells": {"abc": {}, "5": "nope", "7": {"name": "Ok",
                 "levels": {"3": {"damage": 10, "casts": 2}, "x": {}}}}}
        got = SpellDamageData.from_dict(weird)
        assert got.estimate(7, 3) == {"damage": 5, "healing": 0, "level": 3, "casts": 2}

    def test_name_links_cast_id_to_its_damage_id(self):
        d = SpellDamageData()
        d.add(1216571, "Fel Missiles", 10, damage=0, casts=10)      # the channel
        d.add(1216570, "Fel Missiles", 10, damage=5000, casts=10)   # its damage
        assert d.estimate(1216571, 10)["damage"] == 0
        assert d.estimate(1216571, 10, name="Fel Missiles")["damage"] == 500
        assert d.estimate(999, 10, name="Fel Missiles")["damage"] == 500
        assert d.estimate(999, 10, name="Nothing") is None

    def test_later_log_can_improve_placeholder_name(self):
        d = SpellDamageData()
        d.add(1, "spell:1", 10, damage=1, casts=1)
        d.add(1, "Real Name", 10, damage=1, casts=1)
        assert d.spells[1]["name"] == "Real Name"


def _run_stats():
    log = build_run_log()
    from postmortem.analysis.pulls import detect_pulls
    from postmortem.analysis.stats import compute_stats
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(log.text())
    seg = list(segment_runs(parse_file(path)))[0]
    return seg, compute_stats(seg.events, detect_pulls(seg.events))


class TestObserveStats:
    def test_landed_casts_become_history(self):
        seg, stats = _run_stats()
        obs = observe_stats(stats, seg.keystone_level)
        # Dark Bolt landed twice in the fixture (150k + 250k)
        by_name = {e["name"]: e for e in obs.spells.values()}
        bolt = by_name["Dark Bolt"]["levels"][seg.keystone_level or 0]
        assert bolt == {"damage": 400000, "healing": 0, "casts": 2}

    def test_update_from_stats_accumulates(self, tmp_path):
        seg, stats = _run_stats()
        path = tmp_path / "hist.json"
        update_from_stats(stats, seg.keystone_level, path)
        merged = update_from_stats(stats, seg.keystone_level, path)
        by_name = {e["name"]: e for e in merged.spells.values()}
        assert by_name["Dark Bolt"]["levels"][seg.keystone_level or 0]["casts"] == 4


class TestNameLinkedObservation:
    def test_kick_on_channel_id_uses_same_named_damage_id(self, tmp_path):
        b = build_run_log()
        tail = b.lines
        end_idx = next(i for i, line in enumerate(tail) if "CHALLENGE_MODE_END" in line)
        b.lines = tail[:end_idx]
        mob = "Creature-0-1-2-3-4-778"
        # channel 5001 kicked once; damage from the same-named 5000 landed twice
        b.npc_cast_start(30.0, mob, "Mage", 5001, "Fel Missiles")
        b.interrupt(30.5, DPS1, mob, "Mage", 2139, "Counterspell", 5001, "Fel Missiles")
        b.npc_damage(35.0, mob, "Mage", TANK, 5000, "Fel Missiles", 40000)
        b.npc_damage(38.0, mob, "Mage", TANK, 5000, "Fel Missiles", 60000)
        b.lines += tail[end_idx:]
        path = tmp_path / "run.txt"
        path.write_text(b.text(), encoding="utf-8")
        seg = list(segment_runs(parse_file(path)))[0]
        report = analyze_run(seg)
        ev = next(i for i in report["interrupts"] if i["interrupted_spell"] == "Fel Missiles")
        assert ev["estimated_prevented_damage"] == 50000
        assert ev["estimate_source"] == "observed"
        assert ev["estimate_linked_spell_id"] == 5000


class TestKickValueFallback:
    """A kick on a spell that never landed this run is worth 0 without
    a fallback; with history/community data it takes the nearest-level
    average and says where the number came from."""

    def _log_with_only_kicked_spell(self, tmp_path):
        b = build_run_log()
        # build_run_log() already ended the run; splice the extra events
        # in before CHALLENGE_MODE_END so they belong to the segment
        tail = b.lines
        end_idx = next(i for i, line in enumerate(tail) if "CHALLENGE_MODE_END" in line)
        b.lines = tail[:end_idx]
        mob = "Creature-0-1-2-3-4-777"
        # a cast that is kicked and never lands: nothing in-run to average
        b.npc_cast_start(30.0, mob, "Kicker Bait", 424242, "Doom Nova")
        b.interrupt(31.0, DPS1, mob, "Kicker Bait", 2139, "Counterspell",
                    424242, "Doom Nova")
        b.lines += tail[end_idx:]
        path = tmp_path / "run.txt"
        path.write_text(b.text(), encoding="utf-8")
        return list(segment_runs(parse_file(path)))[0]

    def test_no_fallback_is_zero(self, tmp_path):
        seg = self._log_with_only_kicked_spell(tmp_path)
        report = analyze_run(seg)
        ev = next(i for i in report["interrupts"] if i["interrupted_spell"] == "Doom Nova")
        assert ev["estimated_prevented_damage"] is None
        assert ev["estimate_source"] is None

    def test_history_then_community(self, tmp_path):
        seg = self._log_with_only_kicked_spell(tmp_path)
        community = SpellDamageData()
        community.add(424242, "Doom Nova", (seg.keystone_level or 0) + 3, damage=900, casts=3)

        # community only
        report = analyze_run(seg, community_spell_damage=community)
        ev = next(i for i in report["interrupts"] if i["interrupted_spell"] == "Doom Nova")
        assert ev["estimated_prevented_damage"] == 300
        assert ev["estimate_source"] == "community"
        assert ev["estimate_level"] == (seg.keystone_level or 0) + 3
        assert ev["estimate_samples"] == 3
        dps = next(p for p in report["kick_value"]["by_player"] if "Zappyboi" in p["name"])
        assert dps["estimated_prevented_damage"] >= 300

        # own history wins over community when it has the spell
        hist = tmp_path / "hist.json"
        own = SpellDamageData()
        own.add(424242, "Doom Nova", seg.keystone_level, damage=500, casts=1)
        own.save(hist)
        report = analyze_run(seg, spell_damage_history_path=hist,
                             community_spell_damage=community)
        ev = next(i for i in report["interrupts"] if i["interrupted_spell"] == "Doom Nova")
        assert ev["estimated_prevented_damage"] == 500
        assert ev["estimate_source"] == "history"
        # ...and this run's own landed casts were folded into the history
        after = SpellDamageData.load(hist)
        assert any(e["name"] == "Dark Bolt" for e in after.spells.values())

    def test_observed_beats_every_fallback(self, tmp_path):
        seg, _ = _run_stats()
        community = SpellDamageData()
        # fixture: Dark Bolt landed at 150k and 250k -> observed avg 200k
        dark_bolt_id = next(
            i["interrupted_spell_id"] for i in analyze_run(seg)["interrupts"]
            if i["interrupted_spell"] == "Dark Bolt")
        community.add(dark_bolt_id, "Dark Bolt", seg.keystone_level, damage=1, casts=1)
        report = analyze_run(seg, community_spell_damage=community)
        ev = next(i for i in report["interrupts"] if i["interrupted_spell"] == "Dark Bolt")
        assert ev["estimated_prevented_damage"] == 200000
        assert ev["estimate_source"] == "observed"

    def test_text_and_html_reports_label_borrowed_estimates(self, tmp_path):
        from postmortem.report.html import render_html
        from postmortem.report.text import render_text
        seg = self._log_with_only_kicked_spell(tmp_path)
        community = SpellDamageData()
        community.add(424242, "Doom Nova", 7, damage=1200, casts=4)
        report = analyze_run(seg, community_spell_damage=community)
        text = render_text(report)
        assert "from community: avg of 4 casts at +7" in text
        html = render_html(report)
        assert "estimate_source" in html or "community" in html


# --- Warcraft Logs client + build ------------------------------------------

ZONES = {"worldData": {"zones": [
    {"id": 10, "name": "Mythic+ Dungeons", "expansion": {"id": 4, "name": "Old"}},
    {"id": 45, "name": "Mythic+ Season 2", "expansion": {"id": 7, "name": "Midnight"}},
    {"id": 44, "name": "Some Raid", "expansion": {"id": 7, "name": "Midnight"}},
]}}


def _table(entries):
    return {"data": {"entries": entries}}


class FakeTransport:
    """Canned Warcraft Logs: pages of reports, one fight each, plus
    tables per fight. Records every request so tests can count them."""

    def __init__(self, pages=None, limit=3600, spent=0.0):
        self.calls = []
        self.pages = pages or [
            {"has_more_pages": True, "data": [
                {"code": "AAA", "fights": [
                    {"id": 1, "name": "Altar of Fangs", "encounterID": 5001,
                     "keystoneLevel": 10, "kill": True},
                    {"id": 2, "name": "trash", "encounterID": 0, "keystoneLevel": None},
                ]},
                {"code": "BBB", "fights": [
                    {"id": 3, "name": "Altar of Fangs", "encounterID": 5001,
                     "keystoneLevel": 10, "kill": True},
                ]},
            ]},
            {"has_more_pages": False, "data": [
                {"code": "CCC", "fights": [
                    {"id": 1, "name": "Murder Row", "encounterID": 5002,
                     "keystoneLevel": 12, "kill": False},
                ]},
            ]},
        ]
        self.limit = limit
        self.spent = spent

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        if url == TOKEN_URL:
            assert headers["Authorization"].startswith("Basic ")
            return {"access_token": "tok", "token_type": "Bearer"}
        assert headers["Authorization"] == "Bearer tok"
        req = json.loads(body)
        q, v = req["query"], req.get("variables") or {}
        rl = {"limitPerHour": self.limit, "pointsSpentThisHour": self.spent,
              "pointsResetIn": 100}
        self.spent += 10
        if "worldData" in q:
            return {"data": {**ZONES, "rateLimitData": rl}}
        if "reports(" in q:
            page = self.pages[v["page"] - 1]
            return {"data": {"reportData": {"reports": page}, "rateLimitData": rl}}
        if "table(" in q:
            code, fight = v["code"], v["fight"]
            mult = {"AAA": 1, "BBB": 3, "CCC": 10}[code]
            return {"data": {"reportData": {"report": {
                "damage": _table([{"guid": 111, "name": "Big Bolt", "total": 1000 * mult},
                                  {"guid": 222, "name": "Zero Dmg", "total": 0}]),
                "healing": json.dumps(_table([{"guid": 333, "name": "Mend", "total": 500 * mult}])),
                "casts": _table([{"guid": 111, "name": "Big Bolt", "total": 2},
                                 {"guid": 333, "name": "Mend", "total": 1},
                                 {"guid": 444, "name": "No Damage Row", "total": 5}]),
            }}, "rateLimitData": rl}}
        raise AssertionError(f"unexpected query {q}")


class TestWCLClient:
    def test_requires_credentials(self):
        try:
            WCLClient("", "")
            assert False
        except WCLError:
            pass

    def test_token_exchange_then_bearer_and_rate_limit(self):
        t = FakeTransport()
        c = WCLClient("id", "secret", transport=t)
        zone = find_mplus_zone(c)
        assert zone == (45, "Mythic+ Season 2")   # newest expansion's Mythic+ zone
        assert t.calls[0][0] == TOKEN_URL and t.calls[1][0] == API_URL
        assert "rateLimitData" in json.loads(t.calls[1][1])["query"]
        assert c.rate_limit["limitPerHour"] == 3600
        assert c.points_left() == 3600

    def test_graphql_errors_raise(self):
        def bad(url, body, headers):
            if url == TOKEN_URL:
                return {"access_token": "tok"}
            return {"errors": [{"message": "nope"}]}
        c = WCLClient("id", "secret", transport=bad)
        try:
            c.query("worldData { zones { id } }")
            assert False
        except WCLError as exc:
            assert "nope" in str(exc)

    def test_iter_keystone_fights_pages_and_skips_non_keystone(self):
        c = WCLClient("id", "secret", transport=FakeTransport())
        fights = list(iter_keystone_fights(c, 45))
        assert [(f["code"], f["fight_id"], f["level"]) for f in fights] == [
            ("AAA", 1, 10), ("BBB", 3, 10), ("CCC", 1, 12),
        ]
        assert fights[2]["kill"] is False

    def test_fetch_fight_tables_reads_dict_or_json_string_tables(self):
        c = WCLClient("id", "secret", transport=FakeTransport())
        t = fetch_fight_tables(c, "BBB", 3)
        assert t["damage"] == {111: 3000, 222: 0}
        assert t["healing"] == {333: 1500}
        assert t["casts"] == {111: 2, 333: 1, 444: 5}
        assert t["names"][444] == "No Damage Row"


class TestAggregateSamples:
    def test_sums_totals_and_casts_per_level(self):
        samples = {"fights": {
            "A:1": {"level": 10, "damage": {"111": 1000}, "casts": {"111": 2, "444": 5},
                    "names": {"111": "Big Bolt", "444": "No Damage Row"}},
            "B:3": {"level": 10, "damage": {"111": 3000}, "casts": {"111": 2},
                    "names": {"111": "Big Bolt"}},
            "C:1": {"level": 12, "damage": {"111": 10000}, "healing": {"333": 5000},
                    "casts": {"111": 2, "333": 1}, "names": {"111": "Big Bolt", "333": "Mend"}},
            "junk": {"level": "x", "casts": {"abc": "?"}},
        }}
        d = aggregate_spell_damage_samples(samples)
        assert d.estimate(111, 10) == {"damage": 1000, "healing": 0, "level": 10, "casts": 4}
        assert d.estimate(111, 12)["damage"] == 5000
        assert d.estimate(333, 12) == {"damage": 0, "healing": 5000, "level": 12, "casts": 1}
        # cast with no damage row: counted as zero damage, still a sample
        assert d.estimate(444, 10) == {"damage": 0, "healing": 0, "level": 10, "casts": 5}
        # keep-set filters
        kept = aggregate_spell_damage_samples(samples, keep={111})
        assert set(kept.spells) == {111}

    def test_damage_logged_under_sibling_id_is_linked_by_name(self):
        samples = {"fights": {"A:1": {
            "level": 10,
            "casts": {"1216571": 4},
            "damage": {"1216570": 8000},
            "names": {"1216571": "Fel Missiles", "1216570": "Fel Missiles"},
        }}}
        d = aggregate_spell_damage_samples(samples)
        assert d.estimate(1216571, 10) == {"damage": 2000, "healing": 0, "level": 10, "casts": 4}


class TestBuildSpellDamageCommand:
    def _env(self, monkeypatch, transport):
        monkeypatch.setenv("WCL_CLIENT_ID", "id")
        monkeypatch.setenv("WCL_CLIENT_SECRET", "secret")
        import postmortem.wcl as wcl
        monkeypatch.setattr(wcl, "_default_transport", transport)

    def test_missing_credentials_is_a_clear_error(self, monkeypatch, tmp_path):
        monkeypatch.delenv("WCL_CLIENT_ID", raising=False)
        monkeypatch.delenv("WCL_CLIENT_SECRET", raising=False)
        try:
            main(["build-spell-damage", "-o", str(tmp_path / "o.json"),
                  "--samples", str(tmp_path / "s.json"), "--no-bundle"])
            assert False
        except SystemExit as exc:
            assert "WCL_CLIENT_ID" in str(exc)

    def test_builds_resumes_and_bundles(self, monkeypatch, tmp_path, capsys):
        t = FakeTransport()
        self._env(monkeypatch, t)
        bundled = tmp_path / "bundled" / "spell_damage.json"
        monkeypatch.setattr(bundled_module, "bundled_spell_damage_path", lambda: bundled)
        out, samples = tmp_path / "o.json", tmp_path / "s.json"

        assert main(["build-spell-damage", "-o", str(out), "--samples", str(samples),
                     "--all-spells"]) == 0
        printed = capsys.readouterr().out
        assert "zone 45 Mythic+ Season 2" in printed
        assert "fetched 3 new fight(s)" in printed
        data = SpellDamageData.load(out)
        assert data.source == "warcraftlogs.com"
        # AAA (1000/2 casts) + BBB (3000/2 casts) at +10 -> 4000/4 = 1000
        assert data.estimate(111, 10) == {"damage": 1000, "healing": 0, "level": 10, "casts": 4}
        assert data.estimate(333, 12)["healing"] == 5000
        assert json.loads(bundled.read_text()) == json.loads(out.read_text())
        table_calls = sum(1 for u, b, _h in t.calls if u == API_URL and "table(" in json.loads(b)["query"])
        assert table_calls == 3

        # second run: every fight already sampled -> no table queries
        t2 = FakeTransport()
        self._env(monkeypatch, t2)
        assert main(["build-spell-damage", "-o", str(out), "--samples", str(samples),
                     "--all-spells", "--no-bundle"]) == 0
        assert "fetched 0 new fight(s), 3 already sampled" in capsys.readouterr().out
        assert not any("table(" in json.loads(b)["query"] for u, b, _h in t2.calls if u == API_URL)

    def test_per_level_cap_and_budget_stop(self, monkeypatch, tmp_path, capsys):
        t = FakeTransport()
        self._env(monkeypatch, t)
        out, samples = tmp_path / "o.json", tmp_path / "s.json"
        # cap 1 per (dungeon, level): BBB is a second +10 Altar and gets skipped
        assert main(["build-spell-damage", "-o", str(out), "--samples", str(samples),
                     "--all-spells", "--no-bundle", "--per-level", "1"]) == 0
        assert "fetched 2 new fight(s)" in capsys.readouterr().out

        # budget: 3600 limit, already 3500 spent, reserve 200 -> stops before any table
        t3 = FakeTransport(spent=3500)
        self._env(monkeypatch, t3)
        assert main(["build-spell-damage", "-o", str(out), "--samples", str(tmp_path / "s2.json"),
                     "--all-spells", "--no-bundle"]) == 0
        printed = capsys.readouterr().out
        assert "stopped early" in printed
        assert not any("table(" in json.loads(b)["query"] for u, b, _h in t3.calls if u == API_URL)

    def test_default_keeps_only_bundled_interrupt_spells(self, monkeypatch, tmp_path):
        t = FakeTransport()
        self._env(monkeypatch, t)
        idb = tmp_path / "interrupt_data.json"
        idb.write_text(json.dumps({"spells": {"333": {"name": "Mend", "interruptible": True}}}))
        monkeypatch.setattr(bundled_module, "bundled_interrupt_data_path", lambda: idb)
        out = tmp_path / "o.json"
        assert main(["build-spell-damage", "-o", str(out), "--samples", str(tmp_path / "s.json"),
                     "--no-bundle"]) == 0
        assert set(SpellDamageData.load(out).spells) == {333}
