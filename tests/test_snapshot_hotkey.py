"""The desktop app's global snapshot hotkey (desktop/hotkey.py and its
wiring in desktop/api.py): parsing, the listener contract with a fake
backend, and the press -> scheduled snapshot path."""

from __future__ import annotations

import pytest

from postmortem.desktop import config as desktop_config
from postmortem.desktop.api import DesktopAPI
from postmortem.desktop.hotkey import Backend, Combo, HotkeyListener, parse_combo


class TestParseCombo:
    def test_common_forms(self):
        assert parse_combo("ctrl+alt+s") == Combo(frozenset({"ctrl", "alt"}), "s")
        assert parse_combo(" Control + Option + F8 ") == Combo(frozenset({"ctrl", "alt"}), "f8")
        assert parse_combo("cmd-shift-space") == Combo(frozenset({"cmd", "shift"}), "space")
        assert str(parse_combo("shift+ctrl+x")) == "ctrl+shift+x"

    def test_shift_and_punctuation_and_space_separators(self):
        # what a person types for "hold shift, press backtick" (2026-09-18)
        assert parse_combo("shift+`") == Combo(frozenset({"shift"}), "`")
        assert parse_combo("shift `") == Combo(frozenset({"shift"}), "`")
        assert parse_combo("ctrl + shift + `") == Combo(frozenset({"ctrl", "shift"}), "`")
        assert parse_combo("shift+-") == Combo(frozenset({"shift"}), "-")
        assert parse_combo("ctrl++") == Combo(frozenset({"ctrl"}), "+")

    def test_mac_unshift_maps_a_shifted_press_back_to_its_key(self):
        from postmortem.desktop.hotkey import _unshift
        assert _unshift("~") == "`"
        assert _unshift("S") == "s"
        assert _unshift("`") == "`"
        assert _unshift("f8") == "f8"

    @pytest.mark.parametrize("bad", ["", "s", "ctrl+", "hyper+s", "ctrl+enterprise", "~"])
    def test_rejects_unusable_combos(self, bad):
        with pytest.raises(ValueError):
            parse_combo(bad)


class FakeBackend(Backend):
    def __init__(self, ok=True, message="fake active"):
        self.ok, self.message = ok, message
        self.started_with = None
        self.stopped = False
        self._on_press = None

    def start(self, combo, on_press):
        self.started_with = combo
        self._on_press = on_press
        return self.ok, self.message

    def stop(self):
        self.stopped = True

    def press(self):
        self._on_press()


class TestListener:
    def test_start_press_stop(self):
        presses = []
        backend = FakeBackend()
        listener = HotkeyListener("ctrl+alt+s", presses.append and (lambda: presses.append(1)),
                                  backend=backend)
        assert listener.start() == (True, "fake active")
        assert str(backend.started_with) == "ctrl+alt+s" and listener.active
        backend.press()
        assert presses == [1]
        listener.stop()
        assert backend.stopped and not listener.active

    def test_bad_combo_never_reaches_the_backend(self):
        backend = FakeBackend()
        listener = HotkeyListener("s", lambda: None, backend=backend)
        ok, message = listener.start()
        assert ok is False and "modifier" in message and backend.started_with is None

    def test_backend_failure_is_a_message_not_an_exception(self):
        class Boom(Backend):
            def start(self, combo, on_press):
                raise RuntimeError("no display")
            def stop(self):
                pass
        ok, message = HotkeyListener("ctrl+alt+s", lambda: None, backend=Boom()).start()
        assert ok is False and "no display" in message


@pytest.fixture()
def api() -> DesktopAPI:
    return DesktopAPI()


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_config, "config_dir", lambda: tmp_path / "cfg")


class TestApiWiring:
    def test_press_with_no_run_reports_and_press_with_a_run_schedules(self, api, monkeypatch, tmp_path):
        from postmortem.recorder import RecordedRun

        events = []
        api._emit_watch_event = lambda ev: events.append(ev)
        backend = FakeBackend()
        monkeypatch.setattr("postmortem.desktop.hotkey.default_backend", lambda: backend)
        scheduled = []
        monkeypatch.setattr(api, "_on_snapshot_marker",
                            lambda *a, **kw: scheduled.append((a, kw)))

        settings = {"snapshot_hotkey": "ctrl+alt+s", "snapshot_focus": "tank",
                    "snapshot_character": "Dasfloof"}
        api._start_snapshot_hotkey(settings, 120, 60, None, None, None, None)
        assert events[-1] == {"type": "snapshot_hotkey", "ok": True, "message": "fake active",
                              "focus": "Dasfloof"}
        assert api._hotkey is not None

        api._watch_recorder = None
        backend.press()
        assert events[-1]["type"] == "snapshot_failed" and "no key" in events[-1]["error"]

        class Rec:  # only what on_press reads, plus what stop_watch calls
            _current = RecordedRun(path=tmp_path / "r.txt", zone="Z", keystone_level=8,
                                   started_at=0.0)

            def request_stop(self):
                pass
        api._watch_recorder = Rec()
        backend.press()
        assert len(scheduled) == 1
        args, kwargs = scheduled[0]
        assert args[0] is Rec._current and args[2] == "tank"
        assert kwargs == {"focus_name": "Dasfloof", "source": "hotkey"}

        api.stop_watch()
        assert backend.stopped and api._hotkey is None

    def test_empty_hotkey_setting_disables_it(self, api):
        events = []
        api._emit_watch_event = lambda ev: events.append(ev)
        api._start_snapshot_hotkey({"snapshot_hotkey": ""}, 120, 60, None, None, None, None)
        assert events == [] and api._hotkey is None

    def test_a_backend_that_fails_is_reported_and_watch_goes_on(self, api, monkeypatch):
        events = []
        api._emit_watch_event = lambda ev: events.append(ev)
        monkeypatch.setattr("postmortem.desktop.hotkey.default_backend",
                            lambda: FakeBackend(ok=False, message="taken"))
        api._start_snapshot_hotkey({"snapshot_hotkey": "ctrl+alt+s"}, 120, 60,
                                   None, None, None, None)
        assert events[-1] == {"type": "snapshot_hotkey", "ok": False, "message": "taken",
                              "focus": "healer"}
        assert api._hotkey is None

    def test_the_event_names_the_focus_a_press_will_build(self, api, monkeypatch):
        events = []
        api._emit_watch_event = lambda ev: events.append(ev)
        monkeypatch.setattr("postmortem.desktop.hotkey.default_backend", lambda: FakeBackend())
        api._start_snapshot_hotkey({"snapshot_hotkey": "ctrl+`", "snapshot_focus": "tank"},
                                   120, 60, None, None, None, None)
        assert events[-1]["ok"] and events[-1]["focus"] == "tank"
        api.stop_watch()


class TestSettingsGuard:
    """A bare "`" saved fine and only failed as a log line when Watch
    Live started (2026-09-17); now Settings refuses it up front."""

    def test_validate_hotkey(self, api):
        assert api.validate_hotkey("ctrl+`") == {"ok": True, "combo": "ctrl+`"}
        assert api.validate_hotkey(" Control + Option + S ") == {"ok": True, "combo": "ctrl+alt+s"}
        assert api.validate_hotkey("") == {"ok": True, "combo": ""}
        assert api.validate_hotkey(None) == {"ok": True, "combo": ""}
        bad = api.validate_hotkey("`")
        assert bad["ok"] is False and "modifier" in bad["error"]

    def test_save_settings_rejects_a_bare_key_and_keeps_the_rest(self, api):
        result = api.save_settings({"snapshot_hotkey": "`", "snapshot_focus": "tank"})
        assert result["ok"] is False and "modifier" in result["error"]
        assert api.get_settings()["snapshot_focus"] != "tank"   # nothing was written
        assert api.save_settings({"snapshot_hotkey": "ctrl+`", "snapshot_focus": "tank"}) == {"ok": True}
        saved = api.get_settings()
        assert saved["snapshot_hotkey"] == "ctrl+`" and saved["snapshot_focus"] == "tank"
        assert api.save_settings({"snapshot_hotkey": ""}) == {"ok": True}   # empty = disabled


class TestFocusByName:
    def test_character_name_picks_the_role(self):
        from conftest import DUNGEON_DATA, build_run_log
        import json
        from postmortem.analysis.snapshot import build_snapshot
        from postmortem.combatlog.parser import iter_events
        from postmortem.combatlog.segmenter import segment_runs

        (seg,) = list(segment_runs(iter_events(build_run_log().lines)))
        healer_name = next(
            p["name"] for p in _players_with_roles(seg) if p["role"] == "healer")
        report = build_snapshot(seg, seg.start_ts + 30, before_s=20, after_s=10,
                                role="tank", focus_name=healer_name.split("-")[0],
                                source="hotkey")
        assert report["snapshot"]["role"] == "healer"
        assert report["snapshot"]["focus_player"] == healer_name
        assert report["snapshot"]["source"] == "hotkey"
        unknown = build_snapshot(seg, seg.start_ts + 30, before_s=20, after_s=10,
                                 role="tank", focus_name="Nobody")
        assert unknown["snapshot"]["role"] == "tank"   # name unknown: the setting stands


def _players_with_roles(seg):
    from postmortem.analysis.run_analyzer import analyze_run
    from postmortem.mdt.dungeon_data import DungeonDataStore
    import json, tempfile, pathlib
    from conftest import DUNGEON_DATA
    d = pathlib.Path(tempfile.mkdtemp()) / "dd.json"
    d.write_text(json.dumps(DUNGEON_DATA), encoding="utf-8")
    return analyze_run(seg, store=DungeonDataStore.load(d))["players"]
