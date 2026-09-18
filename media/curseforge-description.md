<!--
CurseForge project description for the Postmortem addon (project 1676571).
CurseForge renders Markdown; images are hotlinked from this repo's media/
folder on main (raw.githubusercontent.com). Paste everything below the
line into the project's Description tab.

Screenshots still wanted (none exist in the repo yet -- WoW must be run
for them): the in-game overlay mid-key, the /pm results window, and
Escape > Options > AddOns > Postmortem. Slots are marked TODO below.
-->
---

# Postmortem — see exactly what happened on your Mythic+ key

**Postmortem** is two halves that work together:

- an **in-game addon** (this page) that makes sure every key is logged, shows live stats while you play, and captures a few things only the client can see; and
- a free **desktop companion app + website** that turns the combat log into a full post-mortem the moment the key ends: route vs. plan, every death with a killing-blow recap, kick efficiency, avoidable damage, dispels, downtime, and timer pace, in one shareable link.

No account is needed. The addon works on its own; the app and site are what turn "we wiped on the third pull" into "here is the cast that killed the healer, who could have kicked it, and what it cost the timer."

![Run report on the site](https://raw.githubusercontent.com/Sharpened-Banana/postmortem/main/media/site-report-hero.png)

## What the addon does in-game

**Never forget /combatlog again.** Combat logging (with advanced logging) switches on at the moment the key starts and off when it ends or resets. Nothing to remember before the countdown.

**Live overlay, only during a key.** A small draggable window with forces progress, the timer, a **+2 / +3 chest countdown**, boss and objective splits, deaths with the time they cost, and the group's interrupt count.

**Pull progress against your MDT route.** With Mythic Dungeon Tools installed and a route selected, the overlay shows "Pull N / M" and flags a pull that looks bigger or smaller than planned.

**Death cause tagging.** When someone dies, the recap says what hit them.

**Tank death post-mortem and incoming panel.** For tanks, a short breakdown after a death of what came in and what mitigation was up.

**Snapshot keybind.** Bind "Mark a snapshot" (Key Bindings > AddOns > Postmortem) or type `/pm snapshot` the moment something goes wrong. The desktop app then writes a role-focused report of the two minutes before and one minute after: a healer gets healing, mana, who took what and cooldown use; a tank gets intake, mitigation uptime and the biggest hits.

**Results and history in-game.** `/pm results` shows the run's stats, `/pm history` lists your last keys, and an optional party message announces the completion and chest level.

**Avoidable-damage capture.** At the end of each key the addon reads which spells Blizzard's own damage meter classed as avoidable, which feeds the app's avoidable-damage section for everyone.

**Options panel.** Escape > Options > AddOns > Postmortem, or `/pm options`. Every overlay row, the history size, the snapshot window and the announcements are toggles.

**Minimap icon.** Left-click for a window showing what is live in the addon and what the companion app adds. Right-click copies the download link.

*TODO screenshot: the overlay mid-key (`addon-overlay.png`).*
*TODO screenshot: the `/pm results` window (`addon-results.png`).*
*TODO screenshot: the Options panel (`addon-options.png`).*

## What the companion app adds

Leave the desktop app watching your Logs folder while you play. Every finished key is analyzed and, if you want, uploaded to the site automatically. No clicks per run.

![Watch Live in the desktop app](https://raw.githubusercontent.com/Sharpened-Banana/postmortem/main/media/app-watch-live.png)

The report opens in the app and on the site:

- **Pull timeline** with route deviations outlined, deaths and bloodlust marked.
- **Route map**: planned enemies from your MDT route over the actual player paths.
- **Players**: DPS, HPS, damage taken, kicks, and damage prevented by kicks.
- **Enemy casts, kicked vs. got through**, with what each missed kick cost.
- **Deaths and close calls** with killing-blow recaps.
- **Avoidable damage taken**, **dispel efficiency**, **utility timeline**, and **longest downtime**.
- Every section collapses, and the whole thing works on a phone.

![Report in the desktop app](https://raw.githubusercontent.com/Sharpened-Banana/postmortem/main/media/app-report.png)

![Route map](https://raw.githubusercontent.com/Sharpened-Banana/postmortem/main/media/site-report-route-map.png)

![Deaths section in the app](https://raw.githubusercontent.com/Sharpened-Banana/postmortem/main/media/app-report-deaths.png)

**History** keeps every run you have analyzed, and the site's **Browse Runs**, **Players** and **Kicks** pages let your group compare keys, look up a player, and see which enemy casts get kicked the least across every uploaded run.

![Browse runs on the site](https://raw.githubusercontent.com/Sharpened-Banana/postmortem/main/media/site-runs-desktop.png)

![Report on a phone](https://raw.githubusercontent.com/Sharpened-Banana/postmortem/main/media/site-report-phone.png)

Sign in with Battle.net on the site and link the app once, and your uploads show up on your character pages. Signing in is optional; reports are public links either way.

## Setup in three steps

1. **Install this addon** through the CurseForge app, as usual.
2. **Get the desktop app** for Windows or macOS from the [releases page](https://github.com/Sharpened-Banana/postmortem/releases/latest). Both builds are signed, and the app updates itself.
3. **Open the app, point it at your Logs folder, and turn on Watch Live.** The [guide](https://postmortem-mplus.fly.dev/guide) walks through it with screenshots. No app? Upload the log by hand at [postmortem-mplus.fly.dev/upload](https://postmortem-mplus.fly.dev/upload).

![First run of the desktop app](https://raw.githubusercontent.com/Sharpened-Banana/postmortem/main/media/app-home-first-run.png)

![App settings](https://raw.githubusercontent.com/Sharpened-Banana/postmortem/main/media/app-settings.png)

## Links

- Website and live reports: [postmortem-mplus.fly.dev](https://postmortem-mplus.fly.dev)
- Desktop app downloads: [GitHub releases](https://github.com/Sharpened-Banana/postmortem/releases/latest)
- Source, issues and roadmap: [github.com/Sharpened-Banana/postmortem](https://github.com/Sharpened-Banana/postmortem)

Postmortem is open source and free. The addon never sends anything anywhere; uploading is something the app or you do, and only for runs you choose.
