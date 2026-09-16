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
consecutive header events whose ts is within 1.5 s of the cluster's
first; a cluster of 2 is a healer marker, 3 a tank marker, 4 or more a
general one, 1 is nothing. `Marker(ts, role, count)`; `ts` is the first
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
  `on_snapshot_marker(run, ts, role)`; the same clustering rule as the
  analyzer, applied to the raw lines' timestamps.
- `start_watch` schedules the build for `ts + after_s` (or the run's
  end, whichever comes first): slice the run's recorded lines, build,
  write `<out_dir>/<run>-snapshot-<n>.html`, emit
  `snapshot_ready {zone, level, role, t, path}`. The Watch screen shows
  "Snapshot (healer) at 12:34 -- Open"; opening loads the page in the
  report screen. Settings gain "Snapshot window: before / after".
- Snapshots are local only; nothing is uploaded.
