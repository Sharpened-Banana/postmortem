# Pace line: time budget per pull (design)

Roadmap item 2 ("What sets Postmortem apart"). Agreed with the user
2026-10-14. Post-key analysis first; a live addon version is a later,
separate project.

## Goal

After every pull, say how far ahead of or behind a benchmark the key was,
and how much that pull gained or lost. Answers "where did the time go",
pull by pull, which no in-game addon does.

## Decisions (from the user)

1. **Benchmark:** the player's own best timed run of this dungeon; when
   there is none, the dungeon timer.
2. **Matching:** by progress, not by pull number, so different routes and
   pull orders compare fairly.
3. **Key level:** fastest timed run at the **same** key level; if none, the
   **nearest** timed level (higher before lower at equal distance); if
   none, the timer. The caption always names what was used.
4. Nothing is tagged or deployed until the user has tested it.

## Calculation (`analysis/pace.py`)

Pure function over finished reports, like `verdict.py`: reads no log, so it
works on stored reports, and treats every field as untrusted (site uploads
are anonymous) -- bad or missing values degrade to "no pace", never raise.

- **Progress** p in [0, 1] = 0.5 x forces fraction + 0.5 x bosses killed /
  total bosses. Forces fraction is the run's forces killed / required,
  capped at 1, read from the cumulative `forces.timeline` at the pull's
  end. Bosses killed = distinct `encounters` with `kill: true` ended by
  then. Reports do not store a dungeon's boss count, so **total bosses** =
  distinct bosses killed in a completed run; for an unfinished run, the
  benchmark's count when it is an own-best run. Any half that is unknown
  is dropped and the other half counts fully (forces only, or bosses
  only); with neither, no pace is produced.
- **Game time** at a point = seconds since key start + deaths so far x the
  report's per-death penalty (`death_cost.per_death_s`), i.e. what the
  in-game timer read.
- **Points:** one per pull, at the pull's end: (pull number, progress,
  game time).
- **Benchmark time at progress p:**
  - *Own best:* the benchmark report's points, plus (0, 0) and
    (1, its final in-game duration), linearly interpolated.
  - *Timer:* par x p. Report also carries the +2 / +3 budgets
    (0.8 x par x p, 0.6 x par x p) for drawing.
- **Delta** per point = game time - benchmark time (positive = behind).
  **Pull change** = this pull's delta - previous point's delta (the time
  that pull gained or lost).

## Benchmark selection (`select_benchmark`)

Input: this report and candidate runs (index rows: challenge map id,
level, timed, duration, start, run reference). Candidates must be the same
dungeon, timed, not this run. Pick: same level -> nearest level (ties:
higher first) -> within the chosen level, fastest duration. None -> timer.

Where candidates come from:

| Surface | Candidates |
|---|---|
| Desktop app | local history store (all runs are the user's) |
| CLI | `analyze --history-db` |
| Site | runs uploaded by the same signed-in account; anonymous -> timer |
| Old stored reports | computed at render time, like the verdict |

## Report output

`report["pace"]`:

```json
{
  "version": 1,
  "benchmark": {"kind": "own_best" | "timer", "level": 17,
                "duration_s": 1601.2, "date": "2026-09-28",
                "same_level": true},
  "points": [{"pull": 1, "t_end": 173.3, "progress": 0.12,
              "game_s": 173.3, "bench_s": 160.0,
              "delta_s": 13.3, "change_s": 13.3}],
  "final_delta_s": -129.6
}
```

## Display

- **Pace section** under the verdict: line chart of `delta_s` per pull
  with a zero line (timer kind: also the +2 / +3 lines), caption
  "vs your best +17 (26:40, Sep 28)", "vs your best +16 (no timed +17
  yet)" or "vs the timer (no timed run of this dungeon yet)".
- **Pulls table:** a "Pace" column with `change_s`, green when gained, red
  when lost.
- Same in the desktop app (shares the report HTML) and on the site.
- Verdict integration ("pull 7 lost 48s against your best run") is a
  follow-up, not in this change.

## Testing

- Unit: progress formula (forces only, bosses only, both, capped), game
  time with deaths, interpolation incl. endpoints, timer fallback,
  benchmark selection order (same level, nearest higher-first, fastest,
  excludes self / untimed / other dungeons), hostile input.
- Real data: the 2026-10-07 Kings' Rest +17 against the timer and against
  another Kings' Rest from local history; eyeball the rendered HTML.

## Rollout

1. Analyzer PR: pace.py, report section, desktop + CLI benchmark lookup.
   User reviews the rendered report locally.
2. Site PR: account-based benchmark lookup. Deployed only on the user's go.
3. User tests in the app; beta tag only after that.
4. Live addon version: separate design later.
