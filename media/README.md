# Screenshots

Promotional screenshots of Postmortem: the desktop app, the public site at
[postmortem-mplus.fly.dev](https://postmortem-mplus.fly.dev), and the setup
a new user goes through. All PNG, palette-quantized to keep the folder small
(about 5 MB). Captured 2026-09-17 against `main` at the time (the collapsible
report sections from PR #47, site at Fly v73).

Nothing here shows a logged-in account, an email address, or an account's
character list. Player names visible in reports are in-game character names
from runs that are already public on the site.

## How they were captured

- **Site** (`site-*`, `setup-site-*`, `setup-guide-*`, `setup-release-page`):
  Playwright's Chromium headless shell, from a scratch virtualenv outside the
  repo. Desktop shots use a 1440x900 viewport at 1x; phone shots use
  390x844 at 2x device pixel ratio with a mobile user agent (so a 780x1688
  PNG is one phone screen). "Full page" means the whole scrolled page,
  "viewport" means the first screen. Section crops are element screenshots.
- **Desktop app** (`app-*`): the app launched from source
  (`src/postmortem/desktop/`) on macOS with `HOME` pointed at an empty
  scratch directory, so every screen is in first-run state and none of the
  capturing machine's settings, history or tokens are involved. Screens were
  switched with the shell's own `showScreen()` and each one captured with
  `screencapture -x -o -l <window id>` (window id from Quartz). Retina
  captures (2560x1720) were downscaled to 1600x1075. The report shown is a
  real key (The Blinding Vale +10) analyzed in-app from a recorded per-run
  log; History was populated by analyzing five such logs in the sandbox.
  Three things were staged for the picture and are called out below: the
  paths shown in text boxes, the update banner, and the account-link code.
- **Addon folder**: a Finder list-view window of `addon/Postmortem/`,
  cropped to the file list.

## Desktop app

| File | Size | What it shows |
|---|---|---|
| `app-home-first-run.png` | 1600x1075 | Home screen exactly as a fresh install opens: the four entry cards (New Analysis, Watch Live, History, Settings). |
| `app-new-analysis-ready.png` | 1600x1075 | New Analysis with a combat log chosen and "Analyze run" enabled. The path in the box was typed in for the shot (a real `WoWCombatLog-*.txt` name under `/Applications/World of Warcraft/_retail_/Logs`). |
| `app-report.png` | 1600x1075 | The in-app report right after analysis: timed badge, summary tiles (forces, deaths, combat time, kick efficiency, timer lost to deaths), Collapse all / Expand all, pull timeline, start of the Players table. |
| `app-report-players.png` | 1600x1075 | Same report scrolled to the Players table (DPS/HPS, damage, healing, kicks, kick value, dispels). |
| `app-report-route-map.png` | 1600x1075 | Route map section: planned enemies, player paths, deaths and markers over the dungeon map art. |
| `app-report-collapsed.png` | 1600x1075 | The collapsible sections in use: Players, Avoidable damage and Pulls collapsed, Pull timeline and Route map open. |
| `app-report-deaths.png` | 1600x1075 | Deaths (killing blow, biggest hit, last 5 s, defensive used), Close calls and the start of the Utility timeline. |
| `app-watch-live-setup.png` | 1600x1075 | Watch Live before starting: Logs folder box, optional MDT route / dungeon data / avoidable-damage fields. |
| `app-watch-live.png` | 1600x1075 | Watch Live running: "Recording The Blinding Vale +10..." status and the activity log. It was started on a truncated copy of a real log (a key in progress that never ends) so nothing was uploaded; the box shows the real WoW Logs path in place of the sandbox path. |
| `app-history.png` | 1600x1075 | History screen loaded from a folder of saved reports: totals, trend sparklines (timed rate, deaths per run, kick efficiency) and the run table. |
| `app-settings.png` | 1600x1075 | Top of Settings: MDT addon folder, Raider.io region, avoidable-damage file, default routes with Keystone.guru sync, output folder. The two app-data placeholders read `/Users/you/...` instead of the sandbox path. |
| `app-settings-update-channel.png` | 1600x1075 | Lower Settings: snapshot window and hotkey, the **Update channel** selector (Stable / Beta), Save settings, version line, Extract dungeon data. |
| `app-settings-link-account.png` | 1600x1075 | The sign-in step: "Link this app to your account" pressed, showing the device code the site asks for. The code is a real one issued by the site for this capture and was never entered, so no account was linked and it has since expired. |
| `app-update-banner.png` | 1600x1075 | The "A new version (beta-desktop-49.4) is available" banner with Update now. A source checkout reports version `dev` and never sees an update, so the banner was raised through the shell's own `showUpdateBanner()` with the then-current beta tag; the layout is the real one. |

## Website

| File | Size | What it shows |
|---|---|---|
| `site-landing-hero.png` | 1440x900 | Landing page, first screen: headline, Upload a run / Getting started / Browse runs, run counters. |
| `site-landing-desktop.png` | 1440x2057 | Landing page, full page, including "How it works". |
| `site-runs-desktop.png` | 1440x1600 | Browse Runs (`/runs`), top of the list. |
| `site-report-hero.png` | 1440x900 | Run report (`/runs/108`, The Blinding Vale +10), first screen: Download HTML, summary tiles, Collapse all / Expand all, pull timeline, Players. |
| `site-report-desktop.png` | 1440x11530 | The same report as one full-page image, every section expanded. |
| `site-report-collapsed-desktop.png` | 1440x900 | Collapsible sections in use: Players, Avoidable damage and Pulls collapsed between the open Pull timeline and Route map. |
| `site-report-timeline.png` | 1392x135 | Crop of the Pull timeline section (trash / boss bars, deaths, bloodlust). |
| `site-report-route-map.png` | 1392x638 | Crop of the Route map section. |
| `site-report-players.png` | 1392x551 | Crop of the Players table. |
| `site-report-kicks-table.png` | 1392x639 | Crop of "Enemy casts: kicked vs got through". |
| `site-kicks-desktop.png` | 1440x1600 | Kicks page (`/kicks`), top. |
| `site-players-desktop.png` | 1440x1600 | Players page (`/players`), top. |
| `site-about-desktop.png` | 1440x1181 | About page, full page. |
| `site-guide-desktop.png` | 1440x7389 | Guide (`/guide`), full page. |
| `site-landing-phone.png` | 780x1688 | Landing page on a phone (390 px wide). |
| `site-landing-phone-menu.png` | 780x1688 | Landing page on a phone with the Menu open. |
| `site-runs-phone.png` | 780x1688 | Browse Runs on a phone (card layout). |
| `site-report-phone.png` | 780x1688 | Run report on a phone: tiles, Collapse all / Expand all and the section jump chips. |
| `site-report-collapsed-phone.png` | 780x1688 | Run report on a phone with Players and Avoidable damage collapsed. |
| `site-report-phone-route-map.png` | 780x1688 | Run report on a phone scrolled to the Route map. |
| `site-kicks-phone.png` | 780x1688 | Kicks page on a phone. |
| `site-players-phone.png` | 780x1688 | Players page on a phone. |
| `site-guide-phone.png` | 780x1688 | Guide on a phone. |

## Setup process

| File | Size | What it shows |
|---|---|---|
| `setup-release-page.png` | 1440x900 | The GitHub release page a new user downloads from (`/releases/latest`, alpha-desktop-48 at capture time): macOS zip, Windows installer, portable zip, SHA256SUMS files. |
| `setup-guide-addon.png` | 624x1123 | Guide, Part 1: install the addon from CurseForge, check it loaded, turn on Advanced Combat Logging. |
| `setup-guide-app-install.png` | 624x882 | Guide, Part 2: download the build, get past the "unknown app" warning. |
| `setup-guide-app-settings.png` | 624x1285 | Guide, Part 3: set the app up once (site URL, Logs folder, optional account link, auto-start). |
| `setup-guide-first-run.png` | 624x876 | Guide, Part 4: first live run, from Start watching to "report appears". |
| `setup-guide-manual-upload.png` | 624x1254 | Guide, manual upload path with no app: check the key was logged, find the log, upload it. |
| `setup-site-upload.png` | 1440x943 | The site's `/upload` page (plain file picker). |
| `setup-site-upload-phone.png` | 780x1688 | The same upload page on a phone. |
| `setup-addon-folder.png` | 1405x1335 | The addon folder (`addon/Postmortem/`) as it should look inside `Interface/AddOns/`: the `.toc`, `Bindings.xml`, the Lua modules, `libs/` and `media/`. |
| `app-home-first-run.png` | (above) | The app window on first launch. |
| `app-settings-update-channel.png` | (above) | The in-app Settings with the update channel. |
| `app-update-banner.png` | (above) | The update-available banner (staged, see above). |

### Not captured

- **macOS Gatekeeper / Windows SmartScreen dialogs on first launch.** Builds
  since alpha-desktop-48 are signed and notarized, so the current build
  shows no warning on the capturing Mac, and no Windows machine was
  available. The guide's Part 2 crop (`setup-guide-app-install.png`)
  documents the older warning flow in text.
- **The Windows installer wizard (Inno Setup).** No Windows machine.
- **The in-game Options panel and overlay.** No in-game screenshot exists in
  the repo and WoW was not run for this; see `addon/Postmortem/Options.lua`
  and the `/pm` window described in the top-level README.
- **A real update check finding a build.** Not reproducible from a source
  checkout (version `dev`); the banner was staged as noted.
- **The Battle.net sign-in and character pages.** Deliberately skipped: no
  page showing a logged-in account was captured.
