# Handoff note

Written when the human moved from Claude Code on the web (cloud sandbox)
to Claude Code running locally, so a fresh local session has full context
in one read. If you're picking this up: read this file, then
`docs/IMPLEMENTATION_PLAN.md`, then jump in.

## What this project is

A Mythic+ route post-mortem companion: import an MDT (Mythic Dungeon
Tools) route export, parse `WoWCombatLog.txt`, and compare the plan
against what actually happened — pulls, deviations, damage, healing,
deaths, kicks, cooldowns, forces, downtime. Pure Python 3.10+, **stdlib
only at runtime** (pytest for dev). CLI entry point: `postmortem`
(`src/postmortem/cli.py`).

## State as of this note

`main` (`5ef5c7c`) has everything merged via PR #1
(https://github.com/Sharpened-Banana/Postmortem/pull/1). 86 tests
pass (`python3 -m pytest tests -q`).

Shipped:
- **MDT import** (`mdt/`): all three export wire formats decoded/encoded
  from scratch — current `!~MDT2~` (Base64/Deflate/CBOR), legacy `!`
  (LibDeflate print-encoding + AceSerializer-3.0), old LibCompress.
  `extract.py` is a tolerant Lua-literal parser that turns the MDT
  addon's own dungeon data files into `mdt_data.json` (NPC ids, names,
  forces counts) — this is what resolves planned pulls to real mobs.
- **Combat log parsing** (`combatlog/`): both timestamp formats, GUIDs,
  advanced-logging blocks, damage/heal suffix layouts (old and new),
  `CHALLENGE_MODE_START/END` run segmentation.
- **Analysis** (`analysis/`): pull detection from engagement windows,
  route-vs-actual comparison (early/late/off-route/missed packs, route
  adherence %), a full stats engine — damage/healing/taken per player
  and per pull, DPS/HPS/CPM, interrupts with **estimated damage/healing
  prevented** (priced from the run's own data, including DoT/HoT
  components and unpriceable zero-damage debuffs), kick efficiency
  (kicked vs. got-through vs. died-mid-cast per enemy spell), dispels
  vs. purges, killing blows, deaths with killing-blow + recap + biggest
  hit + last-5s burst, death cost in timer seconds, boss
  encounters/wipes, consumables, buff uptimes, approximate movement,
  bloodlust/battle-res timelines, downtime between pulls.
- **Reports** (`report/`): terminal text, JSON, self-contained dark-mode
  HTML with an inline pull timeline (deviations outlined, deaths/lust
  marked). `report/index.py` builds a static, sortable run-history page
  over a folder of saved reports (per-dungeon bests included).
- **Live recording** (`recorder.py`): tails `WoWCombatLog.txt`, saves
  each run to its own file slice, auto-analyzes on completion, and
  fires shell hooks at run start/end (`--on-run-start`/`--on-run-end`,
  with `MA_ZONE`/`MA_LEVEL`/`MA_PATH` in the environment) — point them
  at `obs-cmd` for free per-run video capture.
- **Raider.io** (`raiderio.py`): optional `analyze --raiderio <region>`
  enrichment (current M+ score, season best), stdlib `urllib`,
  failure-tolerant, off by default.

Everything was validated against the *real* MythicDungeonTools addon
source (cloned during development to verify wire formats and dungeon
data structure) as well as synthetic combat logs built for the test
suite. Both HTML pages were rendered in headless Chromium and checked
for JS errors and correct output.

## What's next

`docs/IMPLEMENTATION_PLAN.md` has the full breakdown. The user asked for
this delegation model when running it:

> Opus as delegator and quality control; Sonnet agents dispatched by
> Opus for the code.

Concretely: an Opus orchestrator per phase dispatches Sonnet coding
agents per work package, reviews their diffs against the acceptance
criteria in the plan, runs `pytest`, and iterates until green. Phases
run **serially** (A→B→C→D→E) because they share `report/html.py`,
`cli.py`, and the README; work packages *inside* a phase may run in
parallel only when their file sets are disjoint (the plan calls this
out per-phase). No agent should commit — the orchestrator/top-level
session does that, once per phase, after tests are green.

Phases, in order: **A** analysis depth (optimal pull matching w/
confidence scores, avoidable-damage tagging, defensive-usage-on-death,
inline-SVG route map overlay) → **B** history/storage (SQLite, trend
charts, `serve` command) → **C** Raider.io depth (disk cache, dungeon
timer table, official run matching) → **D** video tooling (stdlib OBS
WebSocket v5 client, chapters sidecar, ffmpeg clip cutter) → **E**
integration/polish.

Each work package in the plan already has its acceptance criteria and
required tests spelled out — that's the brief to hand each Sonnet agent
almost verbatim.

## Conventions to keep

- No new runtime dependencies without explicit sign-off — this stays
  stdlib-only. Test-only deps (pytest) are fine.
- Every change ships tests; `python3 -m pytest tests -q` must stay
  green.
- Optional integrations (network, OBS, ffmpeg) degrade gracefully:
  clear message, never a crash, never blocking the core report.
- Match existing style: dataclasses, typed signatures, tolerant
  parsing/decoding, snake_case report keys. New report data appears in
  JSON *and* at least one renderer (HTML or text), with tests for both
  the data and the rendered output.
- `tests/conftest.py` has a `LogBuilder` that constructs synthetic
  combat logs line-by-line in chronological order — extend it rather
  than hand-writing raw log lines when a new test needs new event
  types.
- Real MDT/dungeon reference data lives outside this repo (it was
  cloned from `Nnoggie/MythicDungeonTools` during development to verify
  formats) — don't assume it's vendored here; the extractor and its
  tests use synthetic Lua snippets instead.

## Quick start for a new local session

```bash
git clone https://github.com/Sharpened-Banana/Postmortem
cd Postmortem
git checkout main
python3 -m pip install -e ".[dev]"
python3 -m pytest tests -q          # should show 86 passed
cat docs/IMPLEMENTATION_PLAN.md     # the work order
```
