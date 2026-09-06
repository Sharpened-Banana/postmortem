# Third-party data notices

Postmortem's own code is this project's; the files below are bundled data
built from other people's work and carry their own license.

## `src/postmortem/data/interrupt_data.json`

Built by `postmortem build-interrupt-data` (see `cli.py`) from
`src/postmortem/data/method_interrupts_source.json`, which records which
enemy abilities [Method.gg](https://www.method.gg/)'s dungeon guides mark
as requiring an interrupt, for the current Mythic+ dungeon pool. Only
ability *names* are taken from those guides; the spell ids are resolved
from real combat logs by `--resolve-from`, because guide-published ids
routinely point at an ability's damage component rather than its
interruptible cast (Fel Missiles is listed as 1216570, the damage; the
cast that can actually be interrupted is 1216571). Anything whose name
cannot be resolved against a real log is omitted rather than guessed.

An earlier version of this file was built from
[albvar/mplus-interrupts](https://github.com/albvar/mplus-interrupts)
(MIT, notice retained below). That database covers a previous season's
dungeon pool -- verified 2026-09-05 against 13 real runs, it matched zero
of 264 observed casts -- so it is no longer the source, but its schema is
what `build-interrupt-data` still reads.

```
MIT License

Copyright (c) 2026 Alberto Vargas

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to
deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
```

Refreshing this data for a new Mythic+ season is a manual step (last done
2026-09-06 for Midnight Season 2, all eight dungeons):

1. Rebuild `method_interrupts_source.json` by running the
   albvar/mplus-interrupts `SKILL.md` workflow: for each dungeon on
   https://www.method.gg/guides/dungeons, fetch both the guide page and
   its `/ability-tracker` page, take every row flagged Interrupt (plus
   Stop/dispel/soothe rows for context) and union them. The two pages
   sometimes publish different ids for one ability; keep the tracker's
   and record the guide's in `guide_spell_id`. Do not invent rows or ids.
2. Run `postmortem build-interrupt-data src/postmortem/data/method_interrupts_source.json
   -o interrupt_data.json --resolve-from <your real combat logs>`, which
   also copies the result into `src/postmortem/data/` by default. Only
   ids the logs show being interrupted at least once are kept: a name
   the logs have never seen, or one that was cast but never kicked, is
   omitted with a warning. Play that dungeon, land a kick, and rerun to
   pick it up. This matters because one guide name often maps to several
   ids, only some of them kickable (Arc Lightning: 1297778 kicked 12
   times, 1305810 cast 32 times and never kicked), and because guides
   flag some casts as "interrupt" whose own prose says "use a defensive",
   which in every observed case meant nobody has ever kicked it.
3. Cross-check the diff against `learned_interrupts.json` in the app's
   data directory: an added spell with `interrupted > 0` there is
   independently confirmed, and a removed spell with only
   `survived_attempts` was never kickable anyway.

See that
command's `--help` and `analysis/interruptibility.py`'s module docstring
for the data's shape and its one real limitation: it only ever confirms a
spell as interruptible, never as confirmed-uninterruptible (Method's
dungeon guides document what's worth doing, not an exhaustive per-spell
ground truth) -- see that docstring for how that interacts with the
existing "kicked at least once" heuristic.
