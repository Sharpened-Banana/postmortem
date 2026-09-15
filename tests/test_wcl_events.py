"""build-event-data: what public Warcraft Logs events say about enemy
spells (wcl_events.py), the event fetching that feeds it (wcl.py), and
the CLI's offline aggregation path."""

from __future__ import annotations

import json

import pytest

from postmortem.analysis.dispels import DispelData
from postmortem.analysis.interruptibility import InterruptibilityData
from postmortem.analysis.stealable import StealableData
from postmortem.cli import main
from postmortem.wcl import (API_URL, TOKEN_URL, WCLClient, fetch_fight_events,
                            fetch_report_names)
from postmortem.wcl_events import (aggregate_dispels, aggregate_interrupt_evidence,
                                   aggregate_killing_blows, aggregate_stealable,
                                   summarize_fight)

NAMES = {30449: "Spellsteal", 370: "Purge", 2908: "Soothe", 527: "Purify",
         475: "Remove Curse", 88423: "Nature's Cure", 1766: "Kick",
         900001: "Arcane Barrier", 900002: "Frenzy", 900003: "Curse of Doom",
         900004: "Shadow Word", 900005: "Big Bolt", 900006: "Instant Slam",
         900007: "Death Ray"}
ACTORS = {7: {"name": "Rotforged Mage", "game_id": 55, "type": "NPC"}}


def _dispel(remover, removed, target_friendly, target_id=7, is_buff=None):
    ev = {"type": "dispel", "sourceID": 1, "sourceIsFriendly": True,
          "targetID": target_id, "targetIsFriendly": target_friendly,
          "abilityGameID": remover, "extraAbilityGameID": removed}
    if is_buff is not None:
        ev["isBuff"] = is_buff
    return ev


def _fight_events():
    dispels = [
        _dispel(30449, 900001, False), _dispel(30449, 900001, False),
        _dispel(370, 900001, False),
        _dispel(2908, 900002, False),
        _dispel(475, 900003, True, target_id=1),
        _dispel(527, 900004, True, target_id=2), _dispel(88423, 900004, True, target_id=3),
        {"type": "dispel", "abilityGameID": "garbage"},  # tolerated
    ]
    interrupts = [
        {"type": "interrupt", "sourceIsFriendly": True, "targetIsFriendly": False,
         "abilityGameID": 1766, "extraAbilityGameID": 900005},
        {"type": "interrupt", "sourceIsFriendly": False, "targetIsFriendly": True,
         "abilityGameID": 12345, "extraAbilityGameID": 1766},  # enemy kicked us
    ]
    begincasts = [
        {"type": "begincast", "sourceIsFriendly": False, "abilityGameID": 900005},
        {"type": "begincast", "sourceIsFriendly": False, "abilityGameID": 900005},
        {"type": "begincast", "sourceIsFriendly": False, "abilityGameID": 900006},
        {"type": "cast", "sourceIsFriendly": False, "abilityGameID": 900006},  # not a begincast
    ]
    deaths = [
        {"type": "death", "targetIsFriendly": True, "killingAbilityGameID": 900007},
        {"type": "death", "targetIsFriendly": False, "killingAbilityGameID": 1},  # enemy died
        {"type": "death", "targetIsFriendly": True, "abilityGameID": 900007},  # older shape
    ]
    return dispels, interrupts, begincasts, deaths


class TestSummarizeFight:
    def test_sorts_events_into_the_five_tables(self):
        s = summarize_fight(*_fight_events(), names=NAMES, actors=ACTORS)
        assert s["stolen"] == {"900001": {"30449": 2, "370": 1}, "900002": {"2908": 1}}
        assert s["dispelled"] == {"900003": {"475": 1}, "900004": {"527": 1, "88423": 1}}
        assert s["interrupted"] == {"900005": 1}
        assert s["begincasts"] == {"900005": 2, "900006": 1}
        assert s["killed_by"] == {"900007": 2}
        assert s["npcs"] == {"900001": "Rotforged Mage", "900002": "Rotforged Mage"}
        assert s["names"]["900001"] == "Arcane Barrier" and "1766" not in s["names"]

    def test_target_side_falls_back_to_isbuff_on_old_rows(self):
        s = summarize_fight([_dispel(30449, 900001, None, is_buff=True),
                             _dispel(527, 900004, None, is_buff=False)], [], [], [], NAMES)
        assert "900001" in s["stolen"] and "900004" in s["dispelled"]


def _samples(n_fights=3, level=10):
    fights = {}
    for i in range(n_fights):
        s = summarize_fight(*_fight_events(), names=NAMES, actors=ACTORS)
        s.update({"level": level + i, "encounter_id": 1, "encounter": "Ara-Kara", "kill": True})
        fights[f"CODE{i}:1"] = s
    return {"zone_id": 45, "zone_name": "Mythic+ S2", "fights": fights}


class TestAggregateStealable:
    def test_spellstolen_purged_and_soothed_are_told_apart(self):
        out = aggregate_stealable(_samples(), min_removals=2)
        (barrier,) = out["spells"]
        assert barrier["id"] == 900001 and barrier["stealable"] is True
        assert barrier["removals"] == 9 and "spellstolen 6x" in barrier["note"]
        assert "purged 3x" in barrier["note"] and "Rotforged Mage" in barrier["note"]
        (frenzy,) = out["enrages"]
        assert frenzy["id"] == 900002 and "soothed 3x" in frenzy["note"]

    def test_min_removals_drops_one_offs(self):
        out = aggregate_stealable(_samples(n_fights=1), min_removals=2)
        assert [s["id"] for s in out["spells"]] == [900001]
        assert out["enrages"] == []  # soothed once only

    def test_output_loads_as_stealable_data(self, tmp_path):
        out = aggregate_stealable(_samples())
        path = tmp_path / "stealable_spells.json"
        path.write_text(json.dumps({"spells": out["spells"]}), encoding="utf-8")
        data = StealableData.load(path)
        assert data.is_stealable(900001) and not data.is_stealable(900002)


class TestAggregateInterruptEvidence:
    def test_proven_and_never(self):
        out = aggregate_interrupt_evidence(_samples(n_fights=12), min_casts=10, min_fights=10)
        assert out["proven"] == {"900005": {"name": "Big Bolt", "interrupts": 12}}
        assert out["never"] == {"900006": {"name": "Instant Slam", "casts": 12, "fights": 12}}

    def test_the_bar_for_never_is_high(self):
        out = aggregate_interrupt_evidence(_samples(n_fights=3))  # defaults: 200 casts, 10 fights
        assert out["never"] == {}


class TestAggregateDispels:
    def test_school_is_the_intersection_of_removers(self):
        out = aggregate_dispels(_samples(), min_removals=2)
        assert out["spells"]["900003"]["school"] == "curse"      # Remove Curse only
        assert out["spells"]["900004"]["school"] == "magic"      # Purify ∩ Nature's Cure
        assert out["spells"]["900004"]["seen_dispelled"] is True
        assert out["ambiguous"] == {}

    def test_ambiguous_is_reported_not_guessed(self):
        s = _samples(n_fights=2)
        for f in s["fights"].values():
            f["dispelled"] = {"900004": {"527": 1}}  # Purify alone: magic or disease
        out = aggregate_dispels(s, min_removals=2)
        assert "900004" not in out["spells"]
        assert out["ambiguous"]["900004"]["candidates"] == ["disease", "magic"]

    def test_output_loads_as_dispel_data(self, tmp_path):
        out = aggregate_dispels(_samples())
        path = tmp_path / "dispel_data.json"
        path.write_text(json.dumps({"spells": out["spells"]}), encoding="utf-8")
        assert DispelData.load(path).school_of(900003) == "curse"


class TestKillingBlows:
    def test_counts_per_spell_and_level(self):
        out = aggregate_killing_blows(_samples())
        assert out["900007"]["deaths"] == 6
        assert out["900007"]["levels"] == {"10": 2, "11": 2, "12": 2}


class FakeEventsTransport:
    """Token, then master data, then a two-page events walk."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body))
        if url == TOKEN_URL:
            return {"access_token": "tok"}
        q = json.loads(body)
        query, variables = q["query"], q["variables"]
        rl = {"limitPerHour": 3600, "pointsSpentThisHour": 5, "pointsResetIn": 100}
        if "masterData" in query:
            return {"data": {"reportData": {"report": {"masterData": {
                "abilities": [{"gameID": 30449, "name": "Spellsteal"}, {"gameID": "x"}],
                "actors": [{"id": 7, "gameID": 55, "name": "Rotforged Mage", "type": "NPC"}],
            }}}, "rateLimitData": rl}}
        assert "dataType: Dispels" in query and "hostilityType: Friendlies" in query
        if "start" not in variables:
            return {"data": {"reportData": {"report": {"events": {
                "data": [{"type": "dispel", "abilityGameID": 30449}],
                "nextPageTimestamp": 1234.0}}}, "rateLimitData": rl}}
        assert variables["start"] == 1234.0 and "startTime: $start" in query
        return {"data": {"reportData": {"report": {"events": {
            "data": json.dumps([{"type": "dispel", "abilityGameID": 370}]),  # string form
            "nextPageTimestamp": None}}}, "rateLimitData": rl}}


class TestFetching:
    def test_report_names_and_paged_events(self):
        t = FakeEventsTransport()
        c = WCLClient("id", "secret", transport=t)
        names = fetch_report_names(c, "ABC")
        assert names["abilities"] == {30449: "Spellsteal"}
        assert names["actors"][7]["name"] == "Rotforged Mage"
        events = fetch_fight_events(c, "ABC", 3, "Dispels", "Friendlies")
        assert [e["abilityGameID"] for e in events] == [30449, 370]
        assert t.calls[-1][0] == API_URL

    def test_filter_expression_is_passed_as_a_variable(self):
        seen = {}

        def transport(url, body, headers):
            if url == TOKEN_URL:
                return {"access_token": "tok"}
            q = json.loads(body)
            seen.update(q["variables"])
            assert "filterExpression: $filt" in q["query"]
            return {"data": {"reportData": {"report": {"events": {"data": [], "nextPageTimestamp": None}}}}}
        c = WCLClient("id", "secret", transport=transport)
        assert fetch_fight_events(c, "ABC", 1, "Casts", "Enemies",
                                  filter_expression="type = 'begincast'") == []
        assert seen["filt"] == "type = 'begincast'"


class TestCLIOffline:
    def test_offline_build_writes_all_three_files_and_merges(self, tmp_path, capsys):
        samples = tmp_path / "samples.json"
        samples.write_text(json.dumps(_samples(n_fights=12)), encoding="utf-8")
        curated_interrupts = tmp_path / "curated_interrupts.json"
        curated_interrupts.write_text(json.dumps({
            "spells": {"900006": {"name": "Instant Slam", "interruptible": True},
                       "900005": {"name": "Big Bolt", "interruptible": True}},
            "source": "method.gg", "season": "S2"}), encoding="utf-8")
        curated_dispels = tmp_path / "curated_dispels.json"
        curated_dispels.write_text(json.dumps({
            "spells": {"900004": {"name": "Shadow Word", "school": "magic", "note": None,
                                  "seen_dispelled": False}},
            "source": "method.gg", "season": "S2"}), encoding="utf-8")
        out = tmp_path / "out"
        rc = main(["build-event-data", "--offline", "--samples", str(samples),
                   "--output-dir", str(out), "--no-bundle",
                   "--interrupt-data", str(curated_interrupts),
                   "--dispel-data", str(curated_dispels),
                   "--min-casts", "10", "--min-fights", "10", "--mark-uninterruptible"])
        assert rc == 0
        text = capsys.readouterr().out
        assert "stealable: 1 spellstolen" in text and "1 enrage(s)" in text

        stealable = json.loads((out / "stealable_spells.json").read_text())
        assert stealable["spells"][0]["id"] == 900001 and stealable["enrages"][0]["id"] == 900002
        assert stealable["source"] == "warcraftlogs.com"

        interrupts = json.loads((out / "interrupt_data.json").read_text())
        assert interrupts["spells"]["900005"]["wcl_interrupts"] == 12  # curated, confirmed
        # curated says Instant Slam is kickable: absence of kicks never overrides that
        assert interrupts["spells"]["900006"]["interruptible"] is True
        assert interrupts["source"] == "method.gg + warcraftlogs.com"
        assert InterruptibilityData.load(out / "interrupt_data.json").get(900005) is True

        dispels = json.loads((out / "dispel_data.json").read_text())
        assert dispels["spells"]["900004"]["seen_dispelled"] is True    # curated, confirmed
        assert dispels["spells"]["900003"]["school"] == "curse"           # added
        assert DispelData.load(out / "dispel_data.json").school_of(900003) == "curse"

    def test_never_interrupted_is_recorded_only_when_asked(self, tmp_path, capsys):
        samples = tmp_path / "samples.json"
        samples.write_text(json.dumps(_samples(n_fights=12)), encoding="utf-8")
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"spells": {}}), encoding="utf-8")
        common = ["build-event-data", "--offline", "--samples", str(samples), "--no-bundle",
                  "--interrupt-data", str(empty), "--dispel-data", str(empty),
                  "--min-casts", "10", "--min-fights", "10"]
        assert main(common + ["--output-dir", str(tmp_path / "a")]) == 0
        listed = json.loads((tmp_path / "a" / "interrupt_data.json").read_text())
        assert "900006" not in listed["spells"]
        assert "never interrupted: Instant Slam" in capsys.readouterr().out
        assert main(common + ["--output-dir", str(tmp_path / "b"), "--mark-uninterruptible"]) == 0
        marked = json.loads((tmp_path / "b" / "interrupt_data.json").read_text())
        assert marked["spells"]["900006"]["interruptible"] is False

    def test_offline_needs_samples(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["build-event-data", "--offline", "--samples", str(tmp_path / "none.json"),
                  "--no-bundle"])


class TestBundledStealableDefault:
    def test_no_bundled_file_means_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr("postmortem.bundled.bundled_stealable_data_path",
                            lambda: tmp_path / "missing.json")
        assert StealableData.load_bundled() is None

    def test_bundled_file_is_the_default_for_analyze(self, monkeypatch, tmp_path):
        path = tmp_path / "stealable_spells.json"
        path.write_text(json.dumps({"spells": [{"id": 900001, "name": "Arcane Barrier"}]}))
        monkeypatch.setattr("postmortem.bundled.bundled_stealable_data_path", lambda: path)
        from postmortem.cli import _load_stealable
        data = _load_stealable(None)
        assert data is not None and data.is_stealable(900001)
