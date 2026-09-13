# Postmortem addon

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
