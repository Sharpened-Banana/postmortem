# Addon tests

WoW addon Lua can't be fully verified outside a live client, but the parts
that are plain data and layout logic can. These scripts stub the handful of
WoW APIs a file touches, load it for real, and assert on what it produced.

Run them from the repo root with any Lua 5.1+ interpreter:

    lua addon/tests/overlay_layout.lua

`reload_recovery.lua` covers the whole reload path: a `/reload` mid-key
must bring every module back, not just `Tracker.lua`, because `StartRun()`
replaces `MA.state` wholesale.

`chesttimer_reload.lua` covers `ChestTimer.lua`: finishing a key after a
`/reload` wiped the shared state. It reloads the module per scenario, which
is what makes it faithful -- an addon `/reload` restarts every file, so any
file-local carried between scenarios hides the bug.

`run_history.lua` covers `RunHistory.lua` and Bootstrap's event
dispatcher: only real keys are archived, exactly once each. It reloads the
addon per scenario for the same reason `chesttimer_reload.lua` does.

`tank_death.lua` covers `TankDeath.lua`: the live tank death post-mortem
must never tell a tank they sat on a button they could not press, so it
covers the three ways that could go wrong (an untalented spell, one still
on cooldown, and a resource-gated one the cooldown API calls "ready"
regardless of resource), plus the spellbook snapshot it persists for the
analyzer.

Both of those, and `overlay_layout.lua`'s incoming-panel scenario, run
under simulated Secret Values: `issecretvalue()` answers from a set the
harness controls, a secret throws on arithmetic, ordering, concatenation
and `string.format`, and -- the part that matters -- it reports its
*underlying* type to `type()`, the way a live secret does. A plain-table
stand-in answers `"table"`, which lets a `type(x) ~= "number"` guard bail
out quietly in the harness while the same code throws in a key; that is
exactly how the first version of these modules passed here. The stubs
follow the API names and secret annotations in Blizzard's generated
documentation (checked 2026-09-23, build 12.1.0.69933).

`incoming.lua` covers `Incoming.lua`: the encounter timeline may be
absent, quiet, or hand back fields the client will not answer for, and
every one of those has to produce no panel rather than a broken one.

`overlay_layout.lua` covers `Overlay.lua`: the overall dungeon countdown
(including overtime and the no-time-limit fallback), and the order the rows
actually reflow into, both during a key and in the post-key recap window.

`feedback_link.lua` covers `Info.lua`'s "Send feedback" link: an addon
cannot open a browser, so the button hands over a link to the site's form
to copy, and that link must carry the addon's version and nothing that
breaks a pasted URL.

`snapshot_mark.lua` covers `Snapshot.lua` (docs/SNAPSHOT.md): a keybind
press must toggle combat logging off/on exactly N times for the presser's
role (2 healer / 3 tank / 4 other), 0.25 s apart, ending ON, record one
capped `snapshotMarks` entry, honour the 5 s cooldown, and do nothing but
print when no key is active or logging is off. A key ending mid-burst must put
logging straight back ON (an OFF already applied would otherwise swallow
the CHALLENGE_MODE_END line). `LoggingCombat` is stubbed
as a call recorder and `C_Timer.After` as a hand-advanced scheduler; the
real `CombatLogging.lua` is loaded underneath so its state rules apply.

`results_window.lua` covers the "Snapshots" block of `Results.lua`
(docs/SNAPSHOT.md section 4): one `t  role -- line` per headline the
desktop app wrote, capped, and no block at all when the run had none.

Syntax-check every file alongside this:

    for f in addon/Postmortem/*.lua; do luac -p "$f"; done

Note that `luac` on a dev machine is usually Lua 5.5, not WoW's 5.1 VM, so a
clean parse does not prove client compatibility on its own -- `luacheck
--std lua51` is the closer check (its "accessing undefined variable" list is
all WoW globals and expected).

`interrupts_secret.lua` -- Interrupts.lua under Secret Values: names/GUIDs
the client refuses as table keys (simulated by overriding `rawset`/`rawget`),
`canaccessvalue()` as a secrecy signal, and the "keep last good values"
behaviour on a bad tick.

`key_abandoned.lua` covers leaving a key without finishing it (leave
group, hearth, kicked): no COMPLETED/RESET arrives, so `Tracker.lua` must
notice the challenge is gone and dispatch a synthetic
`CHALLENGE_MODE_RESET`, which is what stops `CombatLogging.lua` forcing
logging back on. A loading screen back into the same key must not end it.

`avoidable_harvest.lua` covers `AvoidableDatabase.lua`: one harvest per
key even when COMPLETED and RESET both arrive, no harvest ticker left
running afterwards, and only the amount a key added on top of the Overall
session's key-start snapshot is recorded (a meter reset in between makes
the whole session this key's).
