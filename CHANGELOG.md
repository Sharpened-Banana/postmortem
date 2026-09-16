# Postmortem addon

## 0.3.4 (2026-09-16)

- The "Mark a snapshot" keybind now actually appears under Key Bindings >
  AddOns and fires: Bindings.xml was listed in the .toc, which the client
  does not allow (it loads that file itself), so the binding never
  registered in 0.3.2/0.3.3. `/pm snapshot` was unaffected.

## 0.3.3 (2026-09-15)

- Fixes "Interrupts.lua:89: attempted to index a table that cannot be
  indexed with secret keys", which fired every frame of a key on builds
  before 0.3.1 and could still be reached on 12.1: the kick counter no
  longer keys any table by a meter-provided player name or GUID, the
  secrecy check also honours the 12.1 `canaccessvalue()`, and a key the
  client refuses is skipped for that tick with the last good values kept.
  Pull-progress tracking gets the same guard.

## 0.3.2 (2026-09-15)

- "Mark a snapshot" keybind (Key Bindings > AddOns > Postmortem) and
  `/pm snapshot`: press it mid-key when something just went wrong and the
  desktop app writes a snapshot report of the minutes around it, focused
  on your role -- a healer gets healing, mana, who took what and cooldown
  use; a tank gets damage intake, mitigation uptime and the biggest hits.
  The window (2:00 before / 1:00 after) is adjustable in `/pm options`.
  Marks are kept in your saved variables too.
- The keybind plants its marker by briefly toggling combat logging a few
  times; the desktop app (alpha-desktop-46 and later) also offers a
  system-wide hotkey that needs no addon and never touches logging.
- HUD: the status row can flash a short message during a key.

## 0.3.1 (2026-09-13)

- Finishing a key after a mid-key `/reload` no longer throws, and the
  reload brings the whole addon back (countdown, splits, pull counter,
  combat-logging re-assert) instead of half of it.
- Blizzard's secret damage-meter values can no longer take down the
  per-second tick with a wall of errors.
- Run history records real keys only -- no more blank entries from a
  reset with no key running, or duplicates from a completion followed by
  a reset.
- The "stats loaded" line prints once per results file, not on every
  login.
- The pull counter stops at the plan's last pull instead of reading
  "pull 19 of 16".
- Overlay: the forces row formats fractional values safely, and the row
  chain hangs off an unambiguous anchor.

## 0.3.0 (2026-09-11)

- Overall dungeon countdown on the HUD, with +2/+3 chest timers and
  boss/objective splits; run history (`/pm history`); completion summary
  to chat; precise death-cause tagging; a Blizzard Settings panel with
  every feature toggleable.

The addon is released alongside the desktop app on every `alpha-desktop-N`
tag. What changed in each build is on the GitHub release page:

https://github.com/Sharpened-Banana/postmortem/releases

The addon needs the companion desktop app (or a log upload on the site) to
produce a full post-mortem. See https://postmortem-mplus.fly.dev/guide.
