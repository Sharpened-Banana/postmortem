# Third-party data notices

Postmortem's own code is this project's; the files below are bundled data
built from other people's work and carry their own license.

## `src/postmortem/data/interrupt_data.json`

Built by `postmortem build-interrupt-data` (see `cli.py`) from
[albvar/mplus-interrupts](https://github.com/albvar/mplus-interrupts),
which is itself sourced from [Method.gg](https://www.method.gg/)'s dungeon
guides and enriched against Wowhead's spell/NPC data.

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
