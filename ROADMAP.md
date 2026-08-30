# Roadmap

What exists today is a complete local pipeline: MDT route in, combat log
in, post-mortem out (text/JSON/HTML), live recording with per-run slices,
a historical `index.html` over all saved reports, optional Raider.io
score enrichment, and shell hooks on run start/end (enough to drive OBS
for per-run video). This file tracks where it can go next.

## Historical lookup webpage → hosted service

Today: `mythic-analyzer index reports/` builds a static, self-contained
history page (filter by dungeon, sortable, per-dungeon bests) — works from
any folder, shareable as a file.

Next steps:
- [ ] Trend charts on the index (timed rate, deaths, adherence over time
      per dungeon and per key level)
- [ ] A tiny local web server mode (`mythic-analyzer serve`) watching the
      reports folder, so the page live-updates during a play session
- [ ] Optional SQLite store instead of scanning JSON files, enabling
      cross-run queries ("all wipes on boss X", "kick efficiency trend")
- [ ] A hosted variant (upload reports, share links with the group);
      needs auth and storage — static index stays the default

## Raider.io integration

Today: `analyze --raiderio <region>` adds each player's current M+ score,
spec and season-best run to the report via the public Raider.io API
(failure-tolerant, off by default).

Next steps:
- [ ] Match the analyzed run against the group's Raider.io run history
      (same dungeon/level/time window) to link the official run record
- [ ] Pull the season's dungeon timer table (so the report can show
      +2/+3 thresholds and "seconds remaining", not just the in-game
      success flag)
- [ ] Cache lookups on disk; batch requests politely
- [ ] Affix-aware historical comparisons ("your best on this affix set")

## Video recording of runs

Today: `record --on-run-start/--on-run-end` shell hooks fire exactly at
CHALLENGE_MODE_START/END with `MA_ZONE`/`MA_LEVEL`/`MA_PATH` set — point
them at [obs-cmd](https://github.com/grigio/obs-cmd) (OBS WebSocket) and
every key records its own video next to its log slice.

Next steps:
- [ ] Native OBS WebSocket v5 client (no external CLI needed): start/stop
      recording, name the file after the run, save replay-buffer on wipes
- [ ] Timestamp sidecar: emit a `.chapters` file mapping video time →
      pull/death/boss events, so deaths are one click away in the VOD
- [ ] Optional ffmpeg post-step to cut per-pull clips from the recording

## Analysis depth

- [ ] Avoidable-damage tagging: per-dungeon lists of "don't stand in this"
      spell ids (community-maintained data file), scoring avoidable damage
      taken per player
- [ ] Defensive-usage analysis on deaths (was a defensive available and
      unused in the 5 s before death) — needs a per-spec defensive table
- [ ] Map overlay: draw actual player paths (position samples are already
      in the JSON) on MDT's dungeon maps next to the planned route
- [ ] Smarter pull matching (optimal alignment instead of greedy) and
      confidence scores on route deviations
- [ ] Warcraft Logs export/cross-check
