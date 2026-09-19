#!/usr/bin/env python3
"""Read each Mythic+ dungeon's timer back out of real combat logs.

    python3 scripts/derive_timers.py "/path/to/World of Warcraft/_retail_/Logs"
    python3 scripts/derive_timers.py <logs> --write   # update the packaged table

Why this works: CHALLENGE_MODE_END's fifth field is the score Blizzard
awarded the run, and that score is a function of the timer:

    timed:      score = base(level) + 15 * min(1, (par - t) / par / 0.4)
    over time:  score = base(level) - 15 + 15 * max(-1, (par - t) / par / 0.4)

so every run that is neither at the +15 cap nor at the floor pins ``par``
exactly. ``base(level)`` is read from the logs too (the highest score seen
at a level is base + 15 once anyone has capped it), not hard-coded -- it
changes with the season's affix levels.

Nothing is trusted that does not check out: a dungeon is reported only
when every one of its runs gives the same whole number of seconds. On the
2026-09-19 run (108 keys, 8 dungeons) all of them agreed to the
millisecond. A dungeon with too few usable runs is simply left out, and
the analyzer then gives it no timed/over-timer verdict rather than a
wrong one.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import sys

TABLE = os.path.join(os.path.dirname(__file__), "..", "src", "postmortem", "data", "timers.json")


def read_runs(logs_dir: str):
    for path in sorted(glob.glob(os.path.join(logs_dir, "WoWCombatLog*.txt"))):
        start = None
        with open(path, errors="ignore") as fh:
            for line in fh:
                if "CHALLENGE_MODE_" not in line:
                    continue
                fields = line.split("  ", 1)[-1].strip().split(",")
                if fields[0] == "CHALLENGE_MODE_START" and len(fields) > 4:
                    start = (fields[1].strip('"'), int(fields[3]), int(fields[4]))
                elif fields[0] == "CHALLENGE_MODE_END" and start and len(fields) > 5:
                    total_ms = int(fields[4])
                    if total_ms > 0:  # 0 = the phantom END before every START
                        yield (*start, total_ms, float(fields[5]))
                    start = None


def derive(runs):
    runs = list(runs)
    cap = collections.defaultdict(float)
    for _zone, _cm, level, _ms, score in runs:
        cap[level] = max(cap[level], score)
    # A level's cap is only a real cap if someone actually hit it: capped
    # scores are whole multiples of 5 (base is, and the bonus is 15).
    base = {lvl: s - 15 for lvl, s in cap.items() if abs(s / 5 - round(s / 5)) < 1e-6}
    candidates = collections.defaultdict(list)
    for zone, cm, level, ms, score in runs:
        if level not in base:
            continue
        delta = score - base[level]
        if delta >= 14.999 or delta <= -29.999:
            continue  # at the cap / the floor: says nothing about par
        frac = (delta if delta >= 0 else delta + 15) / 15 * 0.4
        candidates[(cm, zone)].append(ms / (1 - frac))
    timers, rejected = {}, {}
    for (cm, zone), values in sorted(candidates.items()):
        rounded = {round(v / 1000) for v in values}
        spread = max(values) - min(values)
        if len(values) >= 3 and len(rounded) == 1 and spread < 50:
            timers[cm] = (zone, rounded.pop() * 1000, len(values))
        else:
            rejected[cm] = (zone, len(values), spread)
    return timers, rejected


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("logs_dir")
    ap.add_argument("--write", action="store_true", help="merge into the packaged timers.json")
    ap.add_argument("--season", default=None, help="label stored with --write")
    args = ap.parse_args()

    runs = list(read_runs(args.logs_dir))
    timers, rejected = derive(runs)
    print(f"{len(runs)} completed keys")
    for cm, (zone, par_ms, n) in sorted(timers.items()):
        print(f"  {cm:>5}  {zone:<28} {par_ms // 60000}:{par_ms // 1000 % 60:02d}   ({n} runs agree)")
    for cm, (zone, n, spread) in sorted(rejected.items()):
        print(f"  {cm:>5}  {zone:<28} NOT DERIVED ({n} usable runs, spread {spread:.0f} ms)")
    if not args.write:
        return 0 if timers else 1

    with open(TABLE, encoding="utf-8") as fh:
        table = json.load(fh)
    for cm, (zone, par_ms, _n) in timers.items():
        table.setdefault("timers", {})[str(cm)] = par_ms
        table.setdefault("names", {})[str(cm)] = zone
    table["generated_at"] = datetime.date.today().isoformat()
    table["generated_by"] = f"scripts/derive_timers.py over {len(runs)} completed keys"
    if args.season:
        table["season"] = args.season
    with open(TABLE, "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {os.path.normpath(TABLE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
