"""Dispel efficiency (analysis/dispels.py + stats/run_analyzer): tagged
dispellable debuffs applied vs dispelled vs ran out, scored only for
schools someone in the group can dispel."""

import json

import pytest

from conftest import DPS1, HEALER, HOSTILE, PLAYERS, TANK, LogBuilder
from postmortem.analysis.dispels import DispelData, capable_schools
from postmortem.analysis.pulls import detect_pulls
from postmortem.analysis.run_analyzer import analyze_run
from postmortem.analysis.stats import compute_stats
from postmortem.cli import main
from postmortem.combatlog.parser import iter_events
from postmortem.combatlog.segmenter import segment_runs
from postmortem.report.html import render_html
from postmortem.report.text import render_text

MAGIC_ID, POISON_ID, CURSE_ID = 555001, 555002, 555003
SHADELING = 555_000
FELWYRM_LIKE = 11111


@pytest.fixture()
def dispel_data(tmp_path):
    path = tmp_path / "dispel_data.json"
    path.write_text(json.dumps({"spells": {
        str(MAGIC_ID): {"name": "Glacial Torment", "school": "magic"},
        str(POISON_ID): {"name": "Ritual Venom", "school": "poison"},
        str(CURSE_ID): {"name": "Feast of Misery", "school": "curse"},
    }}), encoding="utf-8")
    return DispelData.load(path)


def _log():
    """Group: prot pala (poison/disease), resto shaman (magic/curse),
    fire mage (curse). Debuffs: magic x2 (one dispelled by the shaman
    after 1.5s, one runs out), poison x1 (nobody dispels it -- and the
    paladin *could*), curse x1 (dispelled by the mage)."""
    b = LogBuilder()
    mob = b.npc_guid(SHADELING, "00E1")
    b.start(0)
    for p in PLAYERS:
        b.combatant(0.5, p)
    b.player_damage(10, DPS1, mob, "Shadeling", 133, "Fireball", 50000)
    b.player_damage(10.5, TANK, mob, "Shadeling", 31935, "Avenger's Shield", 20000)
    b.npc_debuff(11.0, mob, "Shadeling", DPS1, MAGIC_ID, "Glacial Torment")
    b.dispel(12.5, HEALER, DPS1[0], DPS1[1], DPS1[2], 77130, "Purify Spirit",
             MAGIC_ID, "Glacial Torment", kind="DEBUFF")
    b.npc_debuff(14.0, mob, "Shadeling", TANK, MAGIC_ID, "Glacial Torment")
    b.aura_removed(20.0, (mob, "Shadeling", HOSTILE, 0), TANK, MAGIC_ID, "Glacial Torment",
                   kind="DEBUFF")
    b.npc_debuff(15.0, mob, "Shadeling", HEALER, POISON_ID, "Ritual Venom")
    b.aura_removed(25.0, (mob, "Shadeling", HOSTILE, 0), HEALER, POISON_ID, "Ritual Venom",
                   kind="DEBUFF")
    b.npc_debuff(16.0, mob, "Shadeling", TANK, CURSE_ID, "Feast of Misery")
    b.dispel(17.0, DPS1, TANK[0], TANK[1], TANK[2], 475, "Remove Curse",
             CURSE_ID, "Feast of Misery", kind="DEBUFF")
    b.player_damage(30, DPS1, mob, "Shadeling", 133, "Fireball", 900000)
    b.party_kill(30.5, DPS1, mob, "Shadeling")
    b.unit_died(31, mob, "Shadeling", HOSTILE)
    b.end(60)
    return b


def _segment(builder):
    (seg,) = list(segment_runs(iter_events(builder.lines)))
    return seg


class TestCapabilities:
    def test_spec_and_class_fallbacks(self):
        assert capable_schools("Shaman", "Restoration") == {"magic", "curse"}
        assert capable_schools("Shaman", "Enhancement") == {"curse"}
        assert capable_schools("Paladin", "Protection") == {"poison", "disease"}
        assert capable_schools("Paladin", "Holy") == {"magic", "poison", "disease"}
        assert capable_schools("Mage", "Fire") == {"curse"}
        assert capable_schools("Warrior", "Protection") == frozenset()
        assert capable_schools(None, None) == frozenset()


class TestStats:
    def test_outcomes_and_per_player_schools(self, dispel_data):
        seg = _segment(_log())
        stats = compute_stats(seg.events, detect_pulls(seg.events), None, dispel_data=dispel_data)
        out = stats.dispel_outcomes
        assert out[MAGIC_ID]["applied"] == 2
        assert out[MAGIC_ID]["dispelled"] == 1
        assert out[MAGIC_ID]["expired"] == 1
        assert out[MAGIC_ID]["time_to_dispel_s"] == [1.5]
        assert out[POISON_ID] == {"name": "Ritual Venom", "school": "poison", "applied": 1,
                                  "dispelled": 0, "expired": 1, "time_to_dispel_s": []}
        assert out[CURSE_ID]["dispelled"] == 1 and out[CURSE_ID]["expired"] == 0
        healer = stats.players[HEALER[0]]
        mage = stats.players[DPS1[0]]
        assert dict(healer.dispels_by_school) == {"magic": 1}
        assert dict(mage.dispels_by_school) == {"curse": 1}
        assert healer.dispels == 1 and mage.dispels == 1

    def test_no_data_means_no_tracking(self):
        seg = _segment(_log())
        stats = compute_stats(seg.events, detect_pulls(seg.events), None)
        assert stats.dispel_outcomes == {}
        # plain dispel counts are unaffected
        assert stats.players[HEALER[0]].dispels == 1


class TestRealLogOrdering:
    def test_removed_then_dispel_is_one_dispel(self, dispel_data):
        """Real logs write SPELL_AURA_REMOVED one line BEFORE the
        SPELL_DISPEL that caused it, same timestamp. That must count as
        one dispel with a real time-to-dispel, not a 'ran out' plus a
        dispel (the double count found on a real run, 2026-09-08)."""
        b = LogBuilder()
        mob = b.npc_guid(SHADELING, "00E2")
        b.start(0)
        for p in PLAYERS:
            b.combatant(0.5, p)
        b.player_damage(10, TANK, mob, "Shadeling", 31935, "Avenger's Shield", 20000)
        b.npc_debuff(11.0, mob, "Shadeling", TANK, MAGIC_ID, "Glacial Torment")
        b.aura_removed(13.0, (mob, "Shadeling", HOSTILE, 0), TANK, MAGIC_ID, "Glacial Torment",
                       kind="DEBUFF")
        b.dispel(13.0, HEALER, TANK[0], TANK[1], TANK[2], 77130, "Purify Spirit",
                 MAGIC_ID, "Glacial Torment", kind="DEBUFF")
        # and one that genuinely runs out much later
        b.npc_debuff(20.0, mob, "Shadeling", TANK, MAGIC_ID, "Glacial Torment")
        b.aura_removed(30.0, (mob, "Shadeling", HOSTILE, 0), TANK, MAGIC_ID, "Glacial Torment",
                       kind="DEBUFF")
        b.player_damage(40, TANK, mob, "Shadeling", 31935, "Avenger's Shield", 900000)
        b.party_kill(40.5, TANK, mob, "Shadeling")
        b.unit_died(41, mob, "Shadeling", HOSTILE)
        b.end(60)
        seg = _segment(b)
        stats = compute_stats(seg.events, detect_pulls(seg.events), None, dispel_data=dispel_data)
        out = stats.dispel_outcomes[MAGIC_ID]
        assert (out["applied"], out["dispelled"], out["expired"]) == (2, 1, 1)
        assert out["time_to_dispel_s"] == [2.0]


class TestSummary:
    def test_scored_only_where_someone_can_dispel(self, dispel_data):
        report = analyze_run(_segment(_log()), dispel_data=dispel_data)
        d = report["dispel_efficiency"]
        by = {s["school"]: s for s in d["schools"]}
        assert set(by) == {"magic", "poison", "curse"}

        magic = by["magic"]
        assert (magic["applied"], magic["dispelled"], magic["expired"]) == (2, 1, 1)
        assert magic["efficiency_pct"] == 50.0
        assert magic["avg_time_to_dispel_s"] == 1.5
        assert [d["name"] for d in magic["dispellers"]] == [HEALER[1]]
        assert magic["dispellers"][0]["dispels"] == 1

        # the prot paladin CAN dispel poison and didn't: scored, 0%
        poison = by["poison"]
        assert [d["name"] for d in poison["dispellers"]] == [TANK[1]]
        assert poison["efficiency_pct"] == 0.0

        # curse: shaman + mage both capable; mage did it
        curse = by["curse"]
        assert {d["name"] for d in curse["dispellers"]} == {HEALER[1], DPS1[1]}
        assert curse["efficiency_pct"] == 100.0

        # overall: (1 + 0 + 1) dispelled of (2 + 1 + 1) scored outcomes
        assert d["overall_efficiency_pct"] == 50.0

    def test_school_nobody_can_dispel_is_listed_but_not_scored(self, tmp_path):
        path = tmp_path / "d.json"
        path.write_text(json.dumps({"spells": {
            str(POISON_ID): {"name": "Ritual Venom", "school": "disease"},
        }}), encoding="utf-8")
        data = DispelData.load(path)
        # prot paladin can dispel disease -- so make the group unable by
        # replacing the tank's spec with a warrior's
        b = _log()
        b.lines = [ln.replace(",66,", ",73,") if "COMBATANT_INFO" in ln and TANK[0] in ln else ln
                   for ln in b.lines]
        report = analyze_run(_segment(b), dispel_data=data)
        (disease,) = report["dispel_efficiency"]["schools"]
        assert disease["dispellers"] == []
        assert disease["efficiency_pct"] is None
        assert report["dispel_efficiency"]["overall_efficiency_pct"] is None

    def test_seen_dispelling_counts_as_capable(self, tmp_path):
        """A talent/pet dispel the table doesn't know about still gets
        credit once it's used."""
        path = tmp_path / "d.json"
        path.write_text(json.dumps({"spells": {
            str(CURSE_ID): {"name": "Feast of Misery", "school": "curse"},
        }}), encoding="utf-8")
        b = _log()
        # make the mage a warrior (no curse dispel by kit) but keep its Remove Curse event
        b.lines = [ln.replace(",63,", ",73,") if "COMBATANT_INFO" in ln and DPS1[0] in ln else ln
                   for ln in b.lines]
        report = analyze_run(_segment(b), dispel_data=DispelData.load(path))
        (curse,) = report["dispel_efficiency"]["schools"]
        assert DPS1[1] in {d["name"] for d in curse["dispellers"]}

    def test_renderers_include_section(self, dispel_data):
        report = analyze_run(_segment(_log()), dispel_data=dispel_data)
        html = render_html(report)
        assert "dispelEfficiency" in html and "Dispel efficiency" in html
        text = render_text(report)
        assert "DISPEL EFFICIENCY" in text
        assert "Magic" in text and "Glacial Torment" in text
        assert "nobody can dispel" not in text  # every school here has a capable player


class TestBuildDispelData:
    SOURCE = {
        "version": "t", "source": "method.gg", "season": "S",
        "dungeons": [{
            "name": "Den", "abilities": [
                {"spell_name": "Glacial Torment", "spell_id": 999999, "category": "dispel-magic",
                 "notes": "healers dispel the DoT quickly"},
                {"spell_name": "Ritual Venom", "spell_id": POISON_ID, "category": "dispel-disease"},
                {"spell_name": "Not A Dispel", "spell_id": 1, "category": "interrupt"},
                {"spell_name": "Never Seen", "spell_id": 2, "category": "dispel-curse"},
            ],
        }],
    }

    def test_resolves_ids_from_logs_and_bundles(self, tmp_path, monkeypatch, capsys):
        from postmortem import bundled
        bundled_path = tmp_path / "pkg" / "dispel_data.json"
        monkeypatch.setattr(bundled, "bundled_dispel_data_path", lambda: bundled_path)
        src = tmp_path / "source.json"; src.write_text(json.dumps(self.SOURCE))
        log = tmp_path / "WoWCombatLog.txt"; log.write_text(_log().text(), encoding="utf-8")
        out = tmp_path / "dispel_data.json"

        assert main(["build-dispel-data", str(src), "-o", str(out), "--resolve-from", str(log)]) == 0
        captured = capsys.readouterr()
        payload = json.loads(out.read_text())
        # Glacial Torment resolved to the id the log actually used, not Method's
        assert set(payload["spells"]) == {str(MAGIC_ID), str(POISON_ID)}
        assert payload["spells"][str(MAGIC_ID)]["school"] == "magic"
        assert payload["spells"][str(MAGIC_ID)]["seen_dispelled"] is True
        assert payload["spells"][str(POISON_ID)]["school"] == "disease"
        assert "Never Seen" in captured.err
        assert bundled_path.is_file()
        data = DispelData.load(out)
        assert data.school_of(MAGIC_ID) == "magic"

    def test_without_logs_uses_published_ids(self, tmp_path):
        src = tmp_path / "source.json"; src.write_text(json.dumps(self.SOURCE))
        out = tmp_path / "dispel_data.json"
        assert main(["build-dispel-data", str(src), "-o", str(out), "--no-bundle"]) == 0
        payload = json.loads(out.read_text())
        assert set(payload["spells"]) == {"999999", str(POISON_ID), "2"}


class TestLoadBundled:
    def test_none_when_missing(self, tmp_path, monkeypatch):
        from postmortem import bundled
        monkeypatch.setattr(bundled, "bundled_dispel_data_path", lambda: tmp_path / "nope.json")
        assert DispelData.load_bundled() is None
