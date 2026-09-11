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

`overlay_layout.lua` covers `Overlay.lua`: the overall dungeon countdown
(including overtime and the no-time-limit fallback), and the order the rows
actually reflow into, both during a key and in the post-key recap window.

Syntax-check every file alongside this:

    for f in addon/Postmortem/*.lua; do luac -p "$f"; done

Note that `luac` on a dev machine is usually Lua 5.5, not WoW's 5.1 VM, so a
clean parse does not prove client compatibility on its own -- `luacheck
--std lua51` is the closer check (its "accessing undefined variable" list is
all WoW globals and expected).
