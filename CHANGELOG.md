# Postmortem addon

## Unreleased

- The tank post-mortem now has an Options toggle of its own, keeps every
  death in a key rather than only the most recent, and records what it
  found into a new `PostmortemTankDB` saved-variables table.
- That capture includes which defensives the client confirms you have
  talented — the one thing a combat log can never carry. Point the
  analyzer at it with `postmortem analyze --tank-db <SavedVariables>` and
  the report stops hedging: spells you never talented disappear from it
  entirely, and ones you had but never pressed all run become real
  findings instead of a "may not be talented" note.
- Tank death post-mortem: when you die, the overlay now names the
  defensives that were off cooldown and unpressed at that moment. It only
  ever reports spells the client confirms you actually know, so an
  untalented button is never held against you, and resource-gated buttons
  (Ignore Pain, Shield of the Righteous, Ironfur) are tracked but never
  scored -- the cooldown API calls those "ready" whether or not you had
  the rage for them.
- The same analysis runs over the combat log afterwards, so the report and
  the site now show, per death, what was ready and unused, what you were
  holding, how long since your last active-mitigation press, and which
  group externals were back up.

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
