# Snapshot: "what just happened" for healers and tanks

A healer or tank presses a keybind in-game. Later (or, with Watch Live,
a minute later) they get a **snapshot report**: a detailed postmortem of
the window around that press -- by default 2 minutes before and 1 minute
after, both adjustable -- focused on their role.

This document is the contract between the three parts. Keep them in sync.

## 1. The addon side (`addon/Postmortem/Snapshot.lua`, `Bindings.xml`)

**Keybind** `POSTMORTEM_SNAPSHOT` ("Mark a snapshot") under a
`BINDING_HEADER_POSTMORTEM` header. Also `/pm snapshot`.

**What a press does** (only while a key is active and combat logging is
on; otherwise it prints why and does nothing):

1. Plants a **marker in the combat log**. WoW writes a timestamped
   `COMBAT_LOG_VERSION` header line every time combat logging is switched
   on (verified: real logs carry 4-12 of them). The addon toggles logging
   off/on N times, 0.25 s apart (off at t, on at t+0.1 s), so the log
   gets N header lines inside ~1.5 s. **N encodes the presser's role**,
   from `UnitGroupRolesAssigned("player")`:
   `HEALER = 2`, `TANK = 3`, anything else = `4`. Roles are unique in a
   key, so the role identifies the player; nothing needs to know the
   local character's name. A single header (a normal toggle by this or
   another addon, or the 2 s-apart login pair) is never a marker.
2. Appends `{at = time(), role = "healer"|"tank"|"general", zone, level,
   elapsed}` to `PostmortemDB.global.snapshotMarks` (capped at 50) -- the
   offline record the CLI can read from SavedVariables.
3. Prints `Postmortem: snapshot marked (healer) -- 2:00 before / 1:00
   after` to chat and flashes the HUD status row.
4. Ignores presses within 5 s of the previous one.

Logging state is restored to ON at the end of the sequence, and the
module's own `CombatLogging_SetState` cache is left agreeing with it.
The 0.1 s "off" gaps lose at most a handful of events; the window's
data does not depend on them.

**Settings** (Options panel, section "Snapshot"): `snapshotBeforeS`
(default 120, 30-600) and `snapshotAfterS` (default 60, 10-300). These
travel with SavedVariables and are what the in-game message shows. The
desktop app has its own copy of the two numbers in Settings (that copy is
what Watch Live uses); the CLI takes flags.

## 2. The analyzer side (`analysis/snapshot.py`, `report/snapshot.py`, CLI)

**Marker detection** `find_markers(events) -> list[Marker]`:
`COMBAT_LOG_VERSION` events are ordinary events in a RunSegment. Cluster
consecutive header events whose ts is within 0.6 s of the previous
header (gap between neighbours, not span from the first: the addon's
own re-assert at key start writes a pair exactly 1.0 s apart); a
cluster of 2 is a healer marker, 3 a tank marker, 4 or more a general
one, 1 is nothing. `Marker(ts, role, count)`; `ts` is the first
header's timestamp.

**Building** `build_snapshot(segment, marker_ts, *, before_s=120,
after_s=60, role="auto", store=None, ...) -> dict`:

- Window `[marker_ts - before_s, marker_ts + after_s]`, clipped to the
  run. Events sliced by ts; `detect_pulls` + `compute_stats` re-run on
  the slice (their inputs are just events + pulls), so every existing
  per-player number, death, close call, dispel, kick and enemy cast is
  window-limited for free.
- `role`: `"healer"`, `"tank"`, `"general"`, or `"auto"` (= the marker's
  role, else general). The **focus player** is the group member whose
  role that is (from COMBATANT_INFO / spec); for general there is none.
- **Per-second series**, for every player, from the events' advanced
  params and amounts: `hp_pct`, `damage_taken`, `healing_done`,
  `healing_received`, and `mana_pct` where the power type is mana.
  `series = {"step_s": 1, "t0": <rel start>, "players": {name: {...}}}`
  with `t` relative to run start like every other report list.
- **Focus sections** (only the one for the role):
  - healer: healing by target, by spell, overheal %, casts and GCD use
    (casts / (window / 1.5 s)), mana at start/min/end, cooldowns used
    (gamedata.DEFENSIVES + the healer's major cooldowns seen as casts),
    externals given, dispels, who took the damage and from what.
  - tank: damage taken per second (peak, mean), by spell and by source,
    physical vs magic (school from the damage event), active mitigation
    uptime and casts (a small per-spec table in `gamedata.py`:
    Shield Block, Ignore Pain, Ironfur, Demon Spikes, Shield of the
    Righteous, Bone Shield/Death Strike, Shuffle/Celestial Brew,
    Obsidian Scales), self-healing, cooldowns used, the biggest hits.
- **Around the marker**: deaths, close calls and the ten largest hits
  inside +/-10 s of the press, so the report opens on the moment itself.
- Output dict: `{"snapshot": {marker_ts, t_marker, before_s, after_s,
  start_ts, end_ts, role, focus_player, source}, "run": segment
  summary, "players", "pulls", "deaths", "close_calls", "dispels",
  "interrupts", "enemy_casts", "cc", "consumables", "series", "focus",
  "around_marker"}`. Every `ts` also gets a run-relative `t`.

**Rendering** `render_snapshot_text(report)` and
`render_snapshot_html(report)` (self-contained page, brand CSS, inline
SVG charts for the series; no external resources, same CSP posture as
report/html.py).

**CLI** `postmortem snapshot LOG [--at MM:SS | --at-ts EPOCH] [--role
healer|tank|general|auto] [--before 120] [--after 60] [--format
text|html|json] [-o OUT]`. Without `--at`, every marker in every run of
the log is rendered (`OUT` becomes a directory / a suffix). `--at` is
relative to the run start (first run unless `--run N`).
`_write_recorded_reports` also writes `<run>-snapshot-<n>.html` next to
the run's reports for every marker found.

## 3. The desktop side (`recorder.py`, `desktop/api.py`, shell)

- `Recorder` notices header clusters while tailing (`_feed`) and calls
  `on_snapshot_marker(run, ts, role)`; the same 0.6 s neighbour-gap rule
  as the analyzer, applied to the raw lines' timestamps.
- `start_watch` schedules the build for `ts + after_s` (or the run's
  end, whichever comes first): slice the run's recorded lines, build,
  write `<out_dir>/<run>-snapshot-<n>.html`, emit
  `snapshot_ready {zone, level, role, t, path}`. The Watch screen shows
  "Snapshot (healer) at 12:34 -- Open"; opening loads the page in the
  report screen. Settings gain "Snapshot window: before / after".
- Snapshots are local only; nothing is uploaded.

### The desktop's own hotkey (the primary trigger)

`desktop/hotkey.py`. Since the app already tails the log while the user
plays, it can take the keypress itself: a system-wide hotkey (setting
`snapshot_hotkey`, default `ctrl+alt+s`, empty disables) is registered
when Watch Live starts -- `RegisterHotKey` on Windows, an `NSEvent`
global key-down monitor on macOS (needs the Input Monitoring
permission; the app asks once and reports the hint in the watch log).
A press is timestamped on this machine's clock, which is the clock WoW
stamps the log with, and goes through the same `_on_snapshot_marker`
path with `source="hotkey"`. **Nothing about combat logging changes**;
this is why it is the primary trigger and the addon keybind the
fallback.

No marker in the log carries a role for this path, so the focus is a
setting: `snapshot_focus` (`healer` / `tank` / `general`), or
`snapshot_character` -- a character name that, when set, wins: the
role is taken from that player's spec in the run
(`build_snapshot(focus_name=...)`). The watch log shows
`snapshot_hotkey {ok, message}` at start so a hotkey another program
already owns, or a missing permission, is visible rather than silent.

## 4. Where snapshots show up

A snapshot is not a loose file any more: every marker's full
`build_snapshot` dict rides inside the run report as
`report["snapshots"]` -- a list in marker order, each entry also given
`"n": 1..` -- and every consumer reads it from there. It is attached
before the report's JSON/HTML are written (`cli.attach_snapshots`, called
by `_write_recorded_reports`, `cmd_analyze` and the desktop `analyze()`),
so the `.json` on disk, the history DB row, the site upload and the
addon's results file all carry it without a second build. Best-effort
throughout: a snapshot that fails to build is skipped and the survivors
are renumbered, so `snapshots[n-1]` and `<run>-snapshot-<n>.html` always
mean the same window; a run with no markers has `"snapshots": []`.

`analysis.snapshot.snapshot_headline(dict) -> {n, role, t, focus, line}`
is the one-sentence summary every list of snapshots shares, per role
(healer: effective healing, overheal %, lowest mana, deaths/close calls
in the window; tank: peak/mean DTPS, best mitigation uptime, biggest
hit; general: deaths, close calls, biggest hit), degrading to whatever
fields the dict has.

- **App, report screen**: a "Snapshots:" strip above the report frame
  (`[healer 1:06] [tank 1:50]`, headline in the tooltip) when the loaded
  report has any; a button swaps the frame to that snapshot page
  (`open_report_snapshot(n)`, rendered from the report the bridge holds
  -- the last `analyze()` or `open_history_run()`), "Back to run report"
  restores the run. `open_history_snapshot(ref, n)` does the same for a
  listed History run. Hidden entirely for a run without markers.
- **App, History**: a small "2 snapshots" tag on the run's row (row key
  `snapshots`, the count, from both the directory scan and the DB).
- **App, Watch Live**: unchanged -- "Snapshot (healer) at 12:34 -- Open"
  the moment the window closes, opening the loose file; the same
  snapshot is in the run report once the key ends.
- **In game, `/pm results`**: a "Snapshots" block, `12:34  healer --
  <line>`, from `snapshots` in PostmortemResults.lua (headlines only,
  capped at six); no block at all when the run had none.
- **Site, run page**: reads `report["snapshots"]` from the uploaded
  report (built separately in the site repo).

## Notes from the implementation (2026-09-15)

- `build_snapshot` also takes `stealable`, `pull_gap_seconds`, `marker`
  (the detected `Marker`, so callers need not re-search) and
  `source="marker"|"manual"` (what fills `snapshot.source`). The
  `snapshot` block also carries `t_start`, `t_end`, `window_s`,
  `active_s` and `marker_count`; each `players` entry gets `dps`/`hps`/
  `dtps` over the window's combat-active time; `series` has `n` (bin
  count) and hp/mana bins with no advanced-block sample are `null`, which
  the chart draws as a gap rather than a made-up value.
- COMBATANT_INFO is logged once at the key's start, so the latest
  pre-window line per player is carried into the slice (re-stamped to the
  window start); otherwise a later window would know nobody's spec.
- The healer section's cooldown and external tables are
  `gamedata.HEALER_COOLDOWNS` / `gamedata.EXTERNALS`; the tank section's
  is `gamedata.ACTIVE_MITIGATION`. Spell ids there are long-stable live
  values; Ironfur/Barkskin, Divine Hymn/Guardian Spirit and mana
  (powerType 0) were confirmed on a real Kings' Rest log the day they
  were written, the rest still want a real-log check.
- CLI: with `--at`, `-o` is a file (an existing directory gets
  `<logstem>-snapshot-1.<ext>` inside it); without `--at`, `-o` is the
  directory and markers are numbered across the whole log. `--run`
  defaults to 1 with `--at` and limits the sweep without it.
- The snapshot page has no script at all; every log-derived string is
  escaped, and its (empty) hash list is registered with report/csp.py so a
  script added later is covered automatically.
- Addon: presses inside the 5 s cooldown are ignored silently (debug
  trace only); the sequence routes through `CombatLogging_SetState` so the
  module's 2 s re-assert ticker cannot add a header and shift the role; a
  key ending mid-sequence cancels the remaining toggles.
- Desktop: the build timer waits `after_s + 5 s` of wall-clock time and
  re-reads the run's slice file; `stop_watch` cancels pending timers, and
  the run-end path writes the same files again (same content) for any
  marker whose timer had not fired.
