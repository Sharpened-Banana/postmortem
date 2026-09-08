# Updating Postmortem for a new patch or season

Most of this is one command:

```bash
./scripts/update-for-patch.sh --toc-interface 120200
```

It rebuilds every data file that goes stale, runs the tests, and prints
what changed. It never commits, pushes or deploys — those stay
deliberate, and it prints the exact commands at the end.

Re-running is safe. Steps whose prerequisites are missing are **skipped
with a reason**, never silently, so a partial run tells you exactly what
is still stale.

---

## What actually goes stale, and why

| What | When it matters | Refreshed by |
|---|---|---|
| **Dungeon/enemy data** | Every season (new dungeon pool, changed forces values). Route comparison is meaningless without it. | `extract-data` — reads your installed MDT addon |
| **Talent map** | Every patch that touches talent trees. Without it, a run's talent picks show as node ids instead of names. | `build-talent-data` — Blizzard's API |
| **Interrupt database** | Every season (new dungeon pool = new kickable spells). | `build-interrupt-data` — a community file you download |
| **Dispellable-debuff list** | Every season, from the same Method-derived file as the interrupt database (its dispel rows). Drives the dispel-efficiency section. | `build-dispel-data` — run automatically alongside `build-interrupt-data` |
| **Avoidable-damage list** | Every season (new dungeons = new avoidable spells). Grows with every key you play; a refresh just merges what the addon captured. | `extract-avoidable` — reads the addon's SavedVariables (auto-found under `WTF/`) |
| **Spell damage** | Occasionally. Only the kick-value *fallback* when a spell never landed in your own run. | `build-spell-damage` — samples Warcraft Logs |
| **Addon `## Interface:`** | Every patch, or WoW marks the addon out of date. | `--toc-interface` |

Not on this list, deliberately:

- **Interrupt data captured in-game** is dead — patch 12.0.0 made the
  `notInterruptible` flag unreadable by addons (Blizzard's Secret
  Values). See `addon/Postmortem/InterruptDatabase.lua`, kept as a
  documented stub. The database is maintained on the Python side now.
  (Avoidable damage is the opposite case: Blizzard's damage meter
  exposes its avoidable flag to addons, so that list *is* captured
  in-game — `addon/Postmortem/AvoidableDatabase.lua`.)
- **Raider.io, gear and live talents** need no refresh: they're fetched
  live from Raider.io and Blizzard at page-view time.

---

## Before you run it

1. **Update MDT in WoW** (the addon itself). The script reads whatever
   is installed at
   `/Applications/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools`
   — pass `--mdt PATH` if yours lives elsewhere. Out-of-date MDT means
   out-of-date dungeon data, and the script can't tell.
2. **Export credentials** for the API-backed steps:
   ```bash
   export BNET_CLIENT_ID=... BNET_CLIENT_SECRET=...   # develop.battle.net/access/
   export WCL_CLIENT_ID=... WCL_CLIENT_SECRET=...     # only for --with-spell-damage
   ```
3. **Download an interrupt source file** if the dungeon pool changed —
   an `albvar/mplus-interrupts`-schema JSON. That repo goes stale between
   seasons; re-run its own workflow against Method.gg's dungeon guides to
   regenerate one, then pass `--interrupts-source PATH`. The command
   reads a local file and never fetches.

## Running it

```bash
# the usual: new patch, same season
./scripts/update-for-patch.sh --toc-interface 120200

# new season as well -- dungeon pool changed
./scripts/update-for-patch.sh --toc-interface 120200 \
    --interrupts-source ~/Downloads/mplus-interrupts.json

# occasionally, and it takes hours (resumable, so a cut-off run continues)
./scripts/update-for-patch.sh --with-spell-damage
```

## Checking the result

The script runs the full suite (`tests` + `site/tests`) and stops if
anything fails. Beyond that, the one thing worth eyeballing yourself:

```bash
git diff --stat src/postmortem/data/
```

A new season should show **the dungeon list actually changing** in
`dungeon_data.json`. If it only shows a `generated_at` timestamp, your
MDT install didn't have the new season's data and step 1 above didn't
really happen.

Then verify against a real key:

```bash
postmortem analyze <a recent log> --format text | head -40
```

Route adherence in a sensible range and talents showing **names** rather
than bare node ids means the dungeon data and talent map both took.

## Shipping it

```bash
git checkout -b chore/patch-<version>
git add src/postmortem/data addon/Postmortem/Postmortem.toc
git commit && gh pr create --base main
# after merging:
fly deploy                                        # the site bundles this data
git tag alpha-desktop-N && git push origin alpha-desktop-N   # desktop app
rsync -a addon/Postmortem/ "/Applications/World of Warcraft/_retail_/Interface/AddOns/Postmortem/"
```

The addon is **not** symlinked into your WoW install — that `rsync` is
how changes get there, and you need a `/reload` in game afterwards.

---

## Or hand it to Claude Code

Paste this at the start of a session:

> Run the patch update for Postmortem. Read `docs/PATCH_UPDATE.md`
> first, then:
>
> 1. Check whether MDT in my WoW install has this season's dungeons
>    before running anything — tell me if it looks stale instead of
>    building from it.
> 2. Run `./scripts/update-for-patch.sh` with the right flags. The patch
>    interface version is `<NNNNNN>`; I have/haven't got an interrupt
>    source file at `<path>`. Skip spell damage unless I say otherwise.
> 3. Show me `git diff --stat` for the data files and tell me whether the
>    dungeon list actually changed — if only timestamps moved, say so
>    plainly rather than treating it as a successful refresh.
> 4. Analyze a recent log and confirm route adherence looks sane and
>    talents resolve to names, not node ids.
> 5. Open a PR. Don't deploy or tag a release without asking me.
