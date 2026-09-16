-- The snapshot keybind must plant exactly N COMBAT_LOG_VERSION headers for
-- the presser's role (docs/SNAPSHOT.md section 1): 2 for a healer, 3 for a
-- tank, 4 for anyone else, each as an OFF/ON pair 0.25 s apart with ON
-- 0.1 s after OFF, ending with logging ON. LoggingCombat() is stubbed as a
-- call recorder and C_Timer.After as a scheduler this file advances by
-- hand, so the real Snapshot.lua and CombatLogging.lua run unchanged.
-- Run this from the repo root:
--
--     lua addon/tests/snapshot_mark.lua
--
-- See addon/tests/README.md for how these harnesses work.

-- --- WoW API stubs ----------------------------------------------------
local frames = {}
local function NewFrame()
  local f = { events = {} }
  function f:RegisterEvent(e) self.events[e] = true end
  function f:UnregisterEvent(e) self.events[e] = nil end
  function f:SetScript(_, fn) self.handler = fn end
  table.insert(frames, f)
  return f
end
function CreateFrame() return NewFrame() end

-- Fake clock. GetTime() is what the cooldown reads; time() only feeds the
-- SavedVariables record's `at` field.
local now = 1000
function GetTime() return now end
function time() return 1700000000 + math.floor(now) end

-- Fake scheduler: C_Timer.After entries are kept with their absolute fire
-- time and run, in order, by Advance() below.
local scheduled = {}
C_Timer = {
  After = function(delay, fn) table.insert(scheduled, { at = now + delay, fn = fn }) end,
  NewTicker = function() return { Cancel = function() end } end,
  NewTimer = function() return { Cancel = function() end } end,
}
local function Advance(seconds)
  local deadline = now + seconds
  while true do
    -- Earliest due timer first; ties keep insertion order, which is
    -- what the WoW scheduler does for equal delays.
    local bestIdx, best
    for i, entry in ipairs(scheduled) do
      if entry.at <= deadline and (not best or entry.at < best.at) then
        bestIdx, best = i, entry
      end
    end
    if not best then break end
    table.remove(scheduled, bestIdx)
    now = best.at
    best.fn()
  end
  now = deadline
end

-- LoggingCombat(): a recorder. Each call with an argument is logged with
-- the fake clock's time; no argument returns the current state, as the
-- real API does.
local loggingOn = true
local calls = {}
function LoggingCombat(state)
  if state == nil then return loggingOn end
  loggingOn = state and true or false
  table.insert(calls, { on = loggingOn, at = now })
  return loggingOn
end
local function ResetCalls() calls = {} end

-- print() is captured so the addon's chat lines can be asserted on; the
-- harness's own "ok:" progress lines still go to the real stdout.
local printed = {}
local realPrint = print
function print(...)
  local parts = {}
  for i = 1, select("#", ...) do parts[#parts + 1] = tostring(select(i, ...)) end
  local line = table.concat(parts, " ")
  if line:match("^ok: ") or line:match("^all ") then
    realPrint(line)
  else
    table.insert(printed, line)
  end
end
local function LastPrinted() return printed[#printed] or "" end

local role = "HEALER"
function UnitGroupRolesAssigned() return role end

C_ChallengeMode = {
  GetActiveChallengeMapID = function() return 501 end,
  GetMapUIInfo = function() return "Murder Row", nil, 1800 end,
  GetActiveKeystoneInfo = function() return 10, {} end,
}
C_AddOns = { IsAddOnLoaded = function() return false, false end }
function GetCVar() return "1" end
function SetCVar() end
function CopyTable(t) local c = {} for k, v in pairs(t) do c[k] = v end return c end

-- --- Addon table + real modules -----------------------------------------
local db = { snapshotBeforeS = 120, snapshotAfterS = 60, snapshotMarks = {},
  combatLoggingEnabled = true, warnLoggingConflicts = false }
local keyActive = true
local refreshes = 0
local MA = {
  GetDB = function() return db end,
  Debug = function() end,
  IsKeyActive = function() return keyActive end,
  RegisterKeyEventFrame = function() end,
  RegisterKeyEvents = function(_, frame, events) for _, e in ipairs(events) do frame:RegisterEvent(e) end end,
  Overlay_Refresh = function() refreshes = refreshes + 1 end,
  state = { active = true, elapsed = 754, chestTimer = { mapID = 501, level = 10 } },
}
assert(loadfile("addon/Postmortem/CombatLogging.lua"))("Postmortem", MA)
assert(loadfile("addon/Postmortem/Snapshot.lua"))("Postmortem", MA)

local snapshotFrame
for _, f in ipairs(frames) do
  if f.events["CHALLENGE_MODE_START"] and f.handler then snapshotFrame = f end
end
assert(snapshotFrame, "Snapshot.lua registered no key-event frame")
assert(type(Postmortem_SnapshotMark) == "function", "binding entry point Postmortem_SnapshotMark missing")
assert(BINDING_HEADER_POSTMORTEM and BINDING_NAME_POSTMORTEM_SNAPSHOT, "binding label globals missing")

local function near(a, b) return math.abs(a - b) < 1e-6 end

-- Asserts `calls` is exactly N off/on pairs with the documented timing,
-- measured from the press time `t0`.
local function AssertPairs(n, t0, label)
  assert(#calls == 2 * n, string.format("%s: expected %d LoggingCombat calls, got %d", label, 2 * n, #calls))
  for i = 1, n do
    local off, on = calls[2 * i - 1], calls[2 * i]
    assert(off.on == false, string.format("%s: call %d should be OFF", label, 2 * i - 1))
    assert(on.on == true, string.format("%s: call %d should be ON", label, 2 * i))
    local expectOff = t0 + (i - 1) * 0.25
    assert(near(off.at, expectOff), string.format("%s: OFF %d at %.2f, expected %.2f", label, i, off.at, expectOff))
    assert(near(on.at, expectOff + 0.1), string.format("%s: ON %d at %.2f, expected %.2f", label, i, on.at, expectOff + 0.1))
  end
  assert(loggingOn == true, label .. ": logging must end up ON")
  assert(MA:CombatLogging_GetCurrentState() == true, label .. ": CombatLogging's view of the state must agree (ON)")
end

-- --- 1. healer: two headers ------------------------------------------
role = "HEALER"
local t0 = now
local pressedAt = time()
Postmortem_SnapshotMark()
assert(LastPrinted():find("snapshot marked %(healer%) %-%- 2:00 before / 1:00 after"),
  "chat line wrong: " .. LastPrinted())
assert(MA.state.statusFlash and MA.state.statusFlash.text:find("healer"), "HUD status flash not set")
assert(refreshes >= 1, "overlay was not refreshed on the press")
Advance(2)
AssertPairs(2, t0, "healer")
assert(#db.snapshotMarks == 1, "expected one snapshot mark")
local mark = db.snapshotMarks[1]
assert(mark.role == "healer" and mark.zone == "Murder Row" and mark.level == 10 and mark.elapsed == 754,
  "mark fields wrong: " .. tostring(mark.role) .. "/" .. tostring(mark.zone) .. "/" .. tostring(mark.level))
assert(mark.at == pressedAt, "mark.at should be time() at the press")
print("ok: healer press -> 2 off/on pairs, one mark")

-- --- 2. press inside the 5 s cooldown is ignored ----------------------
ResetCalls()
Advance(1) -- 3 s after the press
Postmortem_SnapshotMark()
Advance(2)
assert(#calls == 0, "a press inside the cooldown toggled logging")
assert(#db.snapshotMarks == 1, "a press inside the cooldown recorded a mark")
print("ok: press inside the 5 s cooldown is ignored")

-- --- 3. tank: three headers --------------------------------------------
role = "TANK"
Advance(5)
ResetCalls()
t0 = now
Postmortem_SnapshotMark()
Advance(2)
AssertPairs(3, t0, "tank")
assert(db.snapshotMarks[2].role == "tank", "tank mark role wrong")
print("ok: tank press -> 3 off/on pairs")

-- --- 4. dps / anything else: four headers ---------------------------
role = "DAMAGER"
Advance(5)
ResetCalls()
t0 = now
Postmortem_SnapshotMark()
Advance(2)
AssertPairs(4, t0, "dps")
assert(db.snapshotMarks[3].role == "general", "dps mark role should be general")
assert(LastPrinted():find("%(general%)"), "chat line should say general: " .. LastPrinted())
print("ok: dps press -> 4 off/on pairs, role general")

-- --- 5. no key active: prints why, does nothing ----------------------
Advance(5)
ResetCalls()
keyActive = false
MA.state.active = false
local marksBefore = #db.snapshotMarks
Postmortem_SnapshotMark()
Advance(2)
assert(#calls == 0, "a press with no key active toggled logging")
assert(#db.snapshotMarks == marksBefore, "a press with no key active recorded a mark")
assert(LastPrinted():find("no Mythic%+ key is active"), "expected the no-key message, got: " .. LastPrinted())
keyActive = true
MA.state.active = true
print("ok: no active key -> message only")

-- --- 6. logging off: prints why, does nothing ------------------------
Advance(5)
ResetCalls()
loggingOn = false
Postmortem_SnapshotMark()
Advance(2)
assert(#calls == 0, "a press with logging off toggled logging")
assert(LastPrinted():find("combat logging is off"), "expected the logging-off message, got: " .. LastPrinted())
loggingOn = true
print("ok: logging off -> message only")

-- --- 7. the mark list is capped at 50 --------------------------------
role = "HEALER"
for i = 1, 60 do
  Advance(6)
  Postmortem_SnapshotMark()
end
Advance(2)
assert(#db.snapshotMarks == 50, "expected 50 marks after 63 presses, got " .. #db.snapshotMarks)
assert(db.snapshotMarks[50].role == "healer", "newest mark should be the last press")
print("ok: snapshotMarks capped at 50")

-- --- 8. a key ending mid-sequence drops the pending toggles ----------
-- Otherwise a stray ON could land after CombatLogging's post-key stop.
Advance(6)
ResetCalls()
Postmortem_SnapshotMark()
Advance(0.15) -- first OFF and ON have run
snapshotFrame.handler(snapshotFrame, "CHALLENGE_MODE_COMPLETED")
Advance(2)
assert(#calls == 2, "toggles kept running after the key ended: " .. #calls)
print("ok: key end cancels the rest of the sequence")

-- --- 9. a new key resets the cooldown -------------------------------
ResetCalls()
snapshotFrame.handler(snapshotFrame, "CHALLENGE_MODE_START")
Postmortem_SnapshotMark() -- well inside 5 s of the press in scenario 8
Advance(2)
assert(#calls == 4, "cooldown should not carry into a new key")
print("ok: a new key resets the cooldown")

print("all snapshot-mark checks passed")
