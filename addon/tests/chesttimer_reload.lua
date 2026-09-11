-- Finishing a key after a /reload must not throw.
--
-- A /reload (or a disconnect and relog) during a key sends Tracker.lua down
-- its recovery path, and that path rebuilds MA.state from scratch. Every
-- other module's slice of the state goes with it, including
-- MA.state.chestTimer and MA.state.splits -- and CHALLENGE_MODE_START has
-- already fired, so it never fires again to rebuild them.
--
-- Completing the key then reached RecordSplit() with chestTimer nil and
-- threw "ChestTimer.lua:102: attempt to index a nil value (local 'ct')"
-- (2026-09-11). Run this from the repo root:
--
--     lua addon/tests/chesttimer_reload.lua
--
-- See addon/tests/README.md for how these harnesses work.

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

time = os.time

-- The scenario API: one boss, completed, so a split is due.
C_Scenario = { GetStepInfo = function() return "step", 1, 1 end }
C_ScenarioInfo = {
  GetCriteriaInfo = function(i)
    if i ~= 1 then return nil end
    return { description = "First Boss", completed = true, isWeightedProgress = false }
  end,
}
C_ChallengeMode = {
  GetActiveChallengeMapID = function() return 501 end,
  GetMapUIInfo = function() return "Murder Row", nil, 1800 end,
  GetActiveKeystoneInfo = function() return 10, {} end,
  GetChallengeCompletionInfo = function() return { time = 1200 * 1000 } end,
}

local db = { bestSplits = {} }

-- A /reload restarts the whole addon: every file is loaded again and every
-- file-local starts empty. Loading the module fresh per scenario is what
-- makes this a faithful reload -- an earlier version of this test reused
-- one load, so ChestTimer.lua's own `recordedCriteria` still held the
-- split from the previous scenario, RecordSplit() was skipped, and the
-- test passed against the unfixed code.
local function LoadModule()
  frames = {}
  local MA = {
    GetDB = function() return db end,
    Debug = function() end,
    RegisterKeyEventFrame = function() end,
    state = {},
  }
  local chunk = assert(loadfile("addon/Postmortem/ChestTimer.lua"))
  chunk("Postmortem", MA)
  local eventFrame
  for _, f in ipairs(frames) do
    if f.events["CHALLENGE_MODE_START"] then eventFrame = f end
  end
  assert(eventFrame, "ChestTimer.lua registered no key-event frame")
  return MA, eventFrame
end

-- --- 1. the normal path still records a split -------------------------
local MA, eventFrame = LoadModule()
MA.state = { active = true, elapsed = 0, splits = {} }
eventFrame.handler(eventFrame, "CHALLENGE_MODE_START")
assert(MA.state.chestTimer, "chest timer was not set up on key start")
assert(MA.state.chestTimer.timeLimit == 1800, "wrong time limit")
MA.state.elapsed = 600
eventFrame.handler(eventFrame, "CHALLENGE_MODE_COMPLETED")
assert(#MA.state.splits == 1, "expected one split on a normal key, got " .. #MA.state.splits)
assert(MA.state.splits[1].name == "First Boss", "split recorded under the wrong name")
print("ok: a normal key records its split")

-- --- 2. /reload mid-key, then finish ----------------------------------
-- The addon has just reloaded, so this file has no state at all, and
-- Tracker.lua's recovery has rebuilt MA.state without the keys this file
-- owns. CHALLENGE_MODE_START is long gone and will not fire again.
MA, eventFrame = LoadModule()
MA.state = { active = true, elapsed = 900 }
local ok, err = pcall(function()
  eventFrame.handler(eventFrame, "CHALLENGE_MODE_COMPLETED")
end)
assert(ok, "finishing a key after /reload threw: " .. tostring(err))
print("ok: finishing a key after /reload does not throw")

-- --- 3. state present but the splits table missing --------------------
MA, eventFrame = LoadModule()
MA.state = {
  active = true, elapsed = 900,
  chestTimer = { timeLimit = 1800, t2 = 1440, t3 = 1080, mapID = 501, level = 10 },
}
ok, err = pcall(function()
  eventFrame.handler(eventFrame, "CHALLENGE_MODE_COMPLETED")
end)
assert(ok, "a missing splits table threw: " .. tostring(err))
print("ok: a half-rebuilt state does not throw either")

-- --- 4. the per-second tick is safe on a wiped state ------------------
MA, eventFrame = LoadModule()
MA.state = { active = true, elapsed = 120 }
ok, err = pcall(function() MA:ChestTimer_OnTick() end)
assert(ok, "the per-second tick threw on a wiped state: " .. tostring(err))
print("ok: the per-second tick survives a wiped state")

print("all chest-timer reload checks passed")
