# Roadmap

What exists today is a complete local pipeline: MDT route in, combat log
in, post-mortem out (text/JSON/HTML), live recording with per-run slices,
a historical `index.html` over all saved reports, optional Raider.io
score enrichment, and shell hooks on run start/end (enough to drive OBS
for per-run video). This file tracks where it can go next.

## Historical lookup webpage → hosted service

Today: `postmortem index reports/` builds a static, self-contained
history page (filter by dungeon, sortable, per-dungeon bests) — works from
any folder, shareable as a file.

Next steps:
- [ ] Trend charts on the index (timed rate, deaths, adherence over time
      per dungeon and per key level)
- [ ] A tiny local web server mode (`postmortem serve`) watching the
      reports folder, so the page live-updates during a play session
- [ ] Optional SQLite store instead of scanning JSON files, enabling
      cross-run queries ("all wipes on boss X", "kick efficiency trend")
- [x] A hosted variant (upload reports, share links with the group) —
      `site/` is a FastAPI service with SQLite storage, deployable to
      Fly.io (`site/README.md` has the runbook). Reads are fully public;
      there's no account system, just a self-issued `X-Upload-Token`
      that lets an uploader update their own run later without letting
      anyone else overwrite it. Static index stays the default for local
      use — `postmortem analyze --upload <url>` opts in to the
      hosted variant per run.
- [ ] Accounts and per-character progression history on the hosted site —
      proposal in `ACCOUNTS_AND_PROGRESSION_PLAN.md` (Battle.net login,
      character pages with key-level/timed-rate/DPS trends, device-code
      sign-in from the desktop app).

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

## Tanking toolkit

Today: the tank death post-mortem (see the Analysis depth item below) plus
`addon/Postmortem/Incoming.lua`, a live panel showing what the encounter
is about to cast next to which of your majors are up.

Everything here is shaped by patch 12.0's Secret Values: enemy casts,
health, absorbs and damage taken are permanently unreadable by addon code,
so the live half can only ever display sanctioned data
(`C_EncounterTimeline`) and the player's own spec, spellbook and
cooldowns. It shows what is coming and what you have, and deliberately
stops short of ranking or recommending — "suggest the next action from
combat state" is the exact pattern those restrictions exist to prevent.

Next steps:
- [ ] Enrich the incoming panel with how hard a spell has historically hit
      *you* (the per-spell history in `analysis/spell_damage.py` already
      accumulates this from your own logs, so it needs no curated data)
- [ ] Damage school per spell, so the panel can distinguish the physical
      hits Shield Block answers from the magic ones it does not — needs
      school captured through the damage-taken path first
- [ ] Surface the per-key tank deaths in the `/pm results` window, not
      just the post-key recap
- [ ] Co-tank swap coordination: the research found no incumbent above
      ~15k downloads, and `GetPartyAssignment("MAINTANK")` still works

## Analysis depth

- [ ] Avoidable-damage tagging: per-dungeon lists of "don't stand in this"
      spell ids (community-maintained data file), scoring avoidable damage
      taken per player
- [x] Defensive-usage analysis on deaths (was a defensive available and
      unused in the 5 s before death) — needs a per-spec defensive table.
      Built as the tank death post-mortem: `analysis/tank_death.py` over
      `data/tank_defensives.json`, rendered in the text and HTML reports
      (and therefore on the site, which renders runs through the
      analyzer's own `render_html`), with a live in-game half in
      `addon/Postmortem/TankDeath.lua`. Both sides share one table — the
      addon's copy is generated by `scripts/build_tank_defensives_lua.py`
      and CI fails if it is stale. Availability is only claimed where it
      can be earned: the analyzer needs proof the player has the spell
      (they cast it at least once that run), the addon asks the spellbook
      directly, and resource-gated buttons are never scored at all.
- [ ] Map overlay: draw actual player paths (position samples are already
      in the JSON) on MDT's dungeon maps next to the planned route
- [ ] Smarter pull matching (optimal alignment instead of greedy) and
      confidence scores on route deviations
- [ ] Warcraft Logs export/cross-check
- [ ] More from public Warcraft Logs (`build-event-data` ships the first
      three: stealable buffs, kick proof, dispel evidence): a "deadliest
      casts" table from killing blows (already in the samples), route and
      pace benchmarks per dungeon/level from fights' `dungeonPulls`, and
      kick values for every enemy spell rather than the interrupt list only

## Mobile app

A phone companion to the desktop app and the site, for looking at runs
away from the PC — after a key on the couch, or on the bus to work.

- [ ] Read-only first: sign in with the same Battle.net account, see your
      characters' run history and open any report (the site already renders
      every report page; the app can wrap those views and add push
      notifications when a Watch Live upload lands)
- [ ] Route planning on the go: browse and pin MDT routes, mark the one the
      group will run, have the desktop app pick it up for the next key
- [ ] Group review: share a run with the party from the phone, comments per
      death/pull that show up on the report page
- [ ] Platform: one cross-platform codebase (iOS + Android) talking to the
      existing site API; no combat-log parsing on the phone
