"""The LOW audit findings in this repo (2026-09-12).

One class per finding id, so a reverted fix names itself when it fails.
See memory/audit_2026_09.md for the triage board these ids come from.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from postmortem.combatlog.parser import parse_file
from postmortem.mdt import cbor
from postmortem.analysis.mapping import MAX_DEATH_SAMPLE_GAP_S, _nearest_sample
from postmortem.raiderio import _extract_par_ms
from postmortem.report.text import _pull_label
from postmortem.addon_results import to_lua_literal

from conftest import HOSTILE, LogBuilder, PLAYERS, TANK


class TestParse8YearAcrossNewYear:
    """The base year comes from the file's mtime -- the year the log
    FINISHED. A log that starts in December therefore has to begin a year
    earlier; before the fix those lines took January's year and the
    rollover detector advanced them again, landing both sides of the
    boundary a year in the future."""

    def _log(self, tmp_path: Path, mtime: tuple) -> Path:
        # Year-less (pre-10.1.7) timestamps: this is the only format the
        # inference applies to.
        path = tmp_path / "WoWCombatLog.txt"
        path.write_text(
            '12/31 23:59:00.000  CHALLENGE_MODE_START,"Murder Row",2830,587,10,[160]\n'
            "1/1 00:01:00.000  CHALLENGE_MODE_END,2830,1,10,600000\n",
            encoding="utf-8",
        )
        stamp = time.mktime(mtime)
        os.utime(path, (stamp, stamp))
        return path

    def test_december_lines_keep_the_previous_year(self, tmp_path):
        # Log finished 1 January 2027, so it started on 31 December 2026.
        path = self._log(tmp_path, (2027, 1, 1, 0, 5, 0, 0, 1, -1))
        events = list(parse_file(path))
        assert [time.localtime(e.ts).tm_year for e in events] == [2026, 2027]
        assert time.localtime(events[0].ts).tm_mon == 12

    def test_a_log_inside_one_year_is_unchanged(self, tmp_path):
        path = tmp_path / "WoWCombatLog.txt"
        path.write_text(
            '4/20 21:00:00.000  CHALLENGE_MODE_START,"Murder Row",2830,587,10,[160]\n'
            "4/20 21:30:00.000  CHALLENGE_MODE_END,2830,1,10,600000\n",
            encoding="utf-8",
        )
        stamp = time.mktime((2026, 4, 20, 22, 0, 0, 0, 1, -1))
        os.utime(path, (stamp, stamp))
        events = list(parse_file(path))
        assert [time.localtime(e.ts).tm_year for e in events] == [2026, 2026]


class TestParse10TrailingBytes:
    """A payload that decodes one valid item and then carries garbage is
    not a route."""

    def test_trailing_garbage_is_refused(self):
        with pytest.raises(cbor.CBORError):
            cbor.loads(cbor.dumps([1, 2, 3]) + b"\xde\xad\xbe\xef")

    def test_an_exact_payload_still_decodes(self):
        assert cbor.loads(cbor.dumps({"a": [1, 2]})) == {"a": [1, 2]}


class TestParse11ZeroLengthRunRates:
    """A zero-length run left dps/hps/cpm off every player, so a consumer
    that indexes rather than gets raised."""

    def test_rate_keys_are_always_present(self, tmp_path):
        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.segmenter import segment_runs

        b = LogBuilder()
        b.start(0)
        for p in PLAYERS:
            b.combatant(0.0, p)
        b.end(0)  # same timestamp: wall duration 0
        path = tmp_path / "log.txt"
        path.write_text(b.text(), encoding="utf-8")

        runs = list(segment_runs(parse_file(path)))
        report = analyze_run(runs[0])
        assert report["run"]["wall_duration_s"] == 0
        for player in report["players"]:
            for key in ("dps", "hps", "cpm", "dps_wall", "hps_wall"):
                assert player[key] == 0, key


class TestAnl7NullPullLabel:
    """A death outside any pull carries pull=None, not a missing key, so
    the text report's dict.get(key, "?") default never fired."""

    def test_null_reads_as_a_question_mark(self):
        assert _pull_label(None) == "?"

    def test_a_real_pull_is_unchanged(self):
        assert _pull_label(3) == "3"


class TestAnl8LuaEscapePadding:
    """Lua's decimal escape consumes up to three digits."""

    def test_a_digit_after_a_control_char_survives(self):
        literal = to_lua_literal("a\x017b")
        assert literal == '"a\\0017b"'
        # The point of the padding: the digit is not part of the escape.
        assert literal.count("7") == 1


class TestAnl9StaleDeathMarkers:
    """Snapping a death to the nearest position sample with no time bound
    drew the marker wherever the player last was, however long ago."""

    def test_a_far_away_sample_is_dropped(self):
        samples = [[10.0, 100.0, -200.0]]
        assert _nearest_sample(samples, 400.0, MAX_DEATH_SAMPLE_GAP_S) is None

    def test_a_recent_sample_is_used(self):
        samples = [[398.0, 100.0, -200.0]]
        assert _nearest_sample(samples, 400.0, MAX_DEATH_SAMPLE_GAP_S) == samples[0]

    def test_no_bound_still_means_nearest(self):
        samples = [[10.0, 1.0, 2.0]]
        assert _nearest_sample(samples, 400.0) == samples[0]


class TestAnl10ParTimePlausibility:
    """An ambiguous key was multiplied to milliseconds unconditionally, so
    a payload already publishing milliseconds graded every run as a
    three-chest with an enormous margin."""

    def test_a_real_par_time_in_ms_is_taken(self):
        assert _extract_par_ms({"par_time_ms": 1_980_000}) == 1_980_000

    def test_seconds_are_multiplied(self):
        assert _extract_par_ms({"time_limit": 1980}) == 1_980_000

    def test_milliseconds_under_a_seconds_key_are_not_multiplied_again(self):
        # 1,980,000 "seconds" is 22 days; read as ms it is a real timer.
        assert _extract_par_ms({"time_limit": 1_980_000}) == 1_980_000

    def test_nonsense_is_refused_rather_than_guessed(self):
        assert _extract_par_ms({"par_time_ms": 42_000}) is None
        assert _extract_par_ms({"time_limit": 0}) is None


class TestAnl12InterruptAndExpiryAgree:
    """A cast could be counted in both the kicked and the expired column
    (caster death logged before the interrupt), and a kick naming a
    different spell than the open cast dropped that cast entirely."""

    def _outcomes(self, tmp_path, build) -> dict:
        from postmortem.analysis.run_analyzer import analyze_run
        from postmortem.combatlog.segmenter import segment_runs

        b = LogBuilder()
        b.start(0)
        for p in PLAYERS:
            b.combatant(0.5, p)
        build(b)
        b.end(60)
        path = tmp_path / "log.txt"
        path.write_text(b.text(), encoding="utf-8")
        runs = list(segment_runs(parse_file(path)))
        report = analyze_run(runs[0])
        return {s["name"]: s for s in report["enemy_casts"]["spells"]}

    def test_a_kick_after_the_casters_death_is_not_also_expired(self, tmp_path):
        caster = LogBuilder.npc_guid(900001, "00A1")

        def build(b):
            b.npc_cast_start(10, caster, "Duskblade", 777, "Shadow Bolt")
            # The client logged the death first, then the interrupt.
            b.unit_died(11, caster, "Duskblade", HOSTILE)
            b.interrupt(11.2, TANK, caster, "Duskblade", 96231, "Rebuke",
                        777, "Shadow Bolt")

        spells = self._outcomes(tmp_path, build)
        shadow_bolt = spells["Shadow Bolt"]
        assert shadow_bolt["kicked"] == 1
        # One cast, one outcome: it used to be counted twice.
        assert shadow_bolt["kicked"] + shadow_bolt["got_through"] == 1

    def test_a_kick_naming_another_spell_does_not_lose_the_open_cast(self, tmp_path):
        caster = LogBuilder.npc_guid(900002, "00A2")

        def build(b):
            b.npc_cast_start(10, caster, "Duskblade", 778, "Curse")
            b.interrupt(11, TANK, caster, "Duskblade", 96231, "Rebuke",
                        779, "Shadow Bolt")

        spells = self._outcomes(tmp_path, build)
        assert spells["Shadow Bolt"]["kicked"] == 1
        # The open cast used to vanish from the table entirely.
        assert "Curse" in spells
        # Neither landed nor expired before the fix: it vanished entirely.
        assert spells["Curse"]["expired"] == 1


class TestDesk12FormatValidation:
    """Validation inside the write loop wrote the valid formats and then
    exited with an error; html always went to a file, clobbering it."""

    def test_a_bad_format_writes_nothing(self, tmp_path, log_file):
        from postmortem.cli import main

        out = tmp_path / "out"
        with pytest.raises(SystemExit) as exc:
            main(["analyze", str(log_file), "--format", "json,bogus",
                  "--out", str(out)])
        assert "unknown format" in str(exc.value)
        assert not out.exists() or not list(out.glob("*.json"))

    def test_html_goes_to_stdout_without_out(self, tmp_path, monkeypatch,
                                             capsys, log_file):
        from postmortem.cli import main

        monkeypatch.chdir(tmp_path)
        assert main(["analyze", str(log_file), "--format", "html"]) == 0
        assert "<!doctype html>" in capsys.readouterr().out.lower()
        assert not list(tmp_path.glob("*.html"))


class TestDesk20TokenTransport:
    """The upload token and the whole report went out in the clear when
    the site URL was plain HTTP."""

    def test_plain_http_is_refused(self):
        from postmortem.upload import upload_report

        result = upload_report({"run": {}}, "http://example.test")
        assert result["ok"] is False
        assert "https" in result["error"]

    def test_a_local_site_is_still_allowed(self, monkeypatch):
        from postmortem import upload as upload_mod

        seen = {}

        def fake_urlopen(request, **kwargs):
            seen["url"] = request.full_url
            raise OSError("stop here -- the transport check already passed")

        monkeypatch.setattr(upload_mod.urllib.request, "urlopen", fake_urlopen)
        upload_mod.upload_report({"run": {}}, "http://localhost:8000")
        assert seen["url"] == "http://localhost:8000/api/runs"


class TestDesk15VanishingReport:
    """A report deleted between the walk and the stat raised out of the
    request handler."""

    def test_a_file_that_disappears_is_skipped(self, tmp_path, monkeypatch):
        from postmortem.history import serve

        index = tmp_path / "index.html"
        index.write_text("x", encoding="utf-8")
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")

        real_stat = Path.stat

        def flaky_stat(self, *args, **kwargs):
            if self.name == "a.json":
                raise FileNotFoundError(self)
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        assert serve._needs_rebuild(tmp_path, index) is False
