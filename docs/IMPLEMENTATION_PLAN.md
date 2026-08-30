# Implementation plan — roadmap build-out

Execution model: an **Opus orchestrator** per phase does delegation and
quality control; it dispatches **Sonnet coding agents** for each work
package (WP), reviews their diffs against the acceptance criteria, runs
the test suite, and iterates until green. Phases run serially (they share
`report/html.py`, `cli.py`, `README.md`); WPs inside a phase may run in
parallel only when their file sets are disjoint.

Ground rules for every WP (QC checklist):
- Runtime stays **stdlib-only** (dev/test deps may use pytest). No new
  runtime dependencies without explicit sign-off.
- Every WP ships tests; `python3 -m pytest tests -q` must pass afterwards.
- Optional integrations (network, OBS, ffmpeg) must degrade gracefully:
  clear message, never a crash, never blocking the core report.
- Match existing style: dataclasses, typed signatures, tolerant parsing,
  report keys in snake_case; new report sections appear in JSON + at
  least one renderer (HTML or text) with tests.
- No commits from agents; the top-level session commits per phase.

## Phase A — Analysis depth

**WP-A1: Optimal pull matching + confidence.**
`analysis/compare.py`: add a windowed alignment matcher (dynamic
programming over planned-pull order, cost = off-plan mobs) replacing the
greedy consumption when it finds a strictly better assignment; expose
`match_confidence` per actual pull (share of its mobs matched to the
primary plan pull) and keep results identical for already-clean runs.
Tests: a reordered-route scenario the greedy matcher mislabels (e.g.
plan pulls A,B pulled as B,A) now matches with correct primaries; all
existing comparison tests unchanged.

**WP-A2: Avoidable-damage tagging.**
New `analysis/avoidable.py`: loader for a user/community JSON file
(`{"spells": [{"id": 123, "name": "...", "note": "..."}], "dungeons":
{"<dungeon_idx>": [...]}}`); scoring: per player, damage taken and hit
counts from tagged spell ids. CLI: `analyze --avoidable-data FILE`.
Report: `avoidable_damage` block + per-player field; text + HTML section
("damage taken from avoidable abilities"). Ship
`docs/avoidable_spells.example.json` and format docs. Tests: fixture
tags Dark Bolt as avoidable, asserts totals per player.

**WP-A3: Defensive usage on deaths.**
`analysis/gamedata.py`: `DEFENSIVES: dict[spell_id, (name, spec_ids|None)]`
curated table of major personals/immunities per class (correctness over
completeness; ids clearly commented). Death analysis: for each death,
defensively-relevant casts by the victim in the 10 s before death (from
the cast timeline) → `defensives_used_before_death` list + boolean
`died_without_defensive` (only when the victim's spec has known
defensives). Render in death entries (text + HTML recap). Tests: one
death with a defensive cast pre-death, one without.

**WP-A4: Route map overlay (SVG).**
`run_analyzer` passes dungeon geometry into the report when dungeon data
is available: enemy clone positions (npc id, x, y, group, sublevel) and
planned-pull membership. `report/html.py` renders an inline-SVG map:
enemy dots colored by planned pull, actual player paths from
`positions`, death markers; sublevel selector if >1. No external assets.
Tests: report JSON contains `map` block; rendered HTML contains the svg
element; no-dungeon-data case omits the section cleanly.

## Phase B — History & storage

**WP-B1: SQLite store.**
New `history/store.py`: `ingest(report_dict) -> run_id` into
`runs.db` (tables: runs, players, deaths; idempotent on
zone+start_ts), `query_runs(filters)`. CLI: `index --db PATH` ingests
scanned reports into the db and builds the page from it; `analyze
--history-db PATH` appends the fresh report. JSON scanning stays the
default path. Tests: ingest twice → one row; filters work.

**WP-B2: Trend charts on the history page.**
`report/index.py`: inline-SVG line/sparkline charts (no deps): timed
rate over time, deaths per run, route adherence, kick efficiency;
respect the dungeon filter. Tests: index HTML contains the chart
elements with multiple synthetic runs.

**WP-B3: `serve` command.**
`history/serve.py`: stdlib `http.server` serving the reports directory;
regenerates `index.html` when report files changed (mtime scan per
request, no watchers); `--port`, `--bind 127.0.0.1` default. Tests:
start server on an ephemeral port in a thread, GET /index.html, assert
regeneration after adding a report.

## Phase C — Raider.io depth

**WP-C1: Disk cache.** `raiderio.py`: cache GETs in
`~/.cache/mythic-analyzer/raiderio.json` (override via env
`MYTHIC_ANALYZER_CACHE`), TTL 6 h, `--raiderio-no-cache` flag. Tests:
fetcher called once across two enrichments with cache on (tmp cache dir).

**WP-C2: Dungeon timer table.** Fetch season static data
(`/api/v1/mythic-plus/static-data?expansion_id=` — resolve current
expansion; tolerate schema drift), cache like WP-C1, map
challenge_map_id → par time; report gains `timer` block: par_ms,
+2/+3 thresholds, margin vs actual (`duration_ms`). Bundled fallback
JSON `data/timers.json` (clearly dated) when offline. Text/HTML show
"beat timer by MM:SS" and threshold reached. Tests: mocked fetcher,
margin math, offline fallback.

**WP-C3: Official run matching.** For enriched players, query
`/api/v1/mythic-plus/runs` / character recent runs; match on
challenge_map_id + keystone_level + completion time within ±10 min;
attach `raiderio_run` {score, url} to the report. Failure-tolerant.
Tests: mocked responses, match and no-match cases.

## Phase D — Video tooling

**WP-D1: Native OBS WebSocket v5 client (stdlib).**
New `obs.py`: minimal RFC6455 client (http handshake, masked frames,
text opcodes, close) + obs-websocket v5 flow: Hello → Identify (sha256
auth challenge) → Request/RequestResponse; ops: StartRecord, StopRecord
(returns output path), SaveReplayBuffer. Recorder flags: `--obs`
(ws://127.0.0.1:4455), `--obs-password`, `--obs-replay-on-death`.
Shell hooks remain and take precedence if both given. All failures are
warnings; recording of the log never stops. Tests: frame
encode/decode round-trip, auth string vector, fake in-process ws server
handshake; no real OBS needed.

**WP-D2: Chapters sidecar.**
After a recorded run completes (recorder knows wall-clock run start),
write `<run>.chapters.json` + WebVTT `<run>.vtt`: video-relative
timestamps for run start, each pull (with pack summary), deaths,
bloodlust, bosses. Offset = event wall time − recording start (hook/OBS
start moment). Tests: synthetic run → expected chapter times/labels.

**WP-D3: Per-pull clip cutter.**
CLI `clips VIDEO REPORT_JSON [--out DIR] [--pad 3]`: cut per-pull and
per-death clips with ffmpeg (`-ss/-to -c copy`), using the chapters
offset logic; skip gracefully (exit message) when ffmpeg is missing.
Tests: fake ffmpeg via PATH shim recording invocations; argument math.

## Phase E — Integration & polish

Single Sonnet pass under Opus QC: README + ROADMAP updated (tick shipped
items), CLI `--help` coherence, HTML report section ordering, regenerate
demo pages, version bump to 0.2.0, full suite green, headless-Chromium
render check of report + index.

## Explicitly out of scope (stays on the roadmap)

Hosted multi-user history service (auth/storage infra), Warcraft Logs
API integration (OAuth app credentials), full avoidable-damage spell
databases (community data — we ship the format + example).
