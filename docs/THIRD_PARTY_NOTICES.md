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

Refreshing this data for a new Mythic+ season is a manual step: download
the latest `mplus_interrupts.json` from that repo and run
`postmortem build-interrupt-data <path> -o interrupt_data.json`, which
also copies the result into `src/postmortem/data/` by default. See that
command's `--help` and `analysis/interruptibility.py`'s module docstring
for the data's shape and its one real limitation: it only ever confirms a
spell as interruptible, never as confirmed-uninterruptible (Method's
dungeon guides document what's worth doing, not an exhaustive per-spell
ground truth) -- see that docstring for how that interacts with the
existing "kicked at least once" heuristic.
