-- A /reload during a key must bring the WHOLE addon back, not just the tracker.
--
-- Tracker.lua recovers on PLAYER_ENTERING_WORLD by calling StartRun(),
-- which replaces MA.state wholesale -- so every other module's slice of
-- the state went with it, and those modules only rebuild on
-- CHALLENGE_MODE_START, which has already fired and never fires again.
-- For the rest of that key there was no countdown, no +2/+3 row, no
-- splits and no pull counter, and -- the one that actually costs data --
-- CombatLogging's 2-second re-assert ticker never started, so another
-- addon could switch combat logging off mid-key with nothing to turn it
-- back on (2026-09-11).
--
-- Run from the repo root:  lua addon/tests/reload_recovery.lua
-- See addon/tests/README.md.

local frames = {}
local function NewFrame()
  local f = {events = {}}
  function f:RegisterEvent(e) self.events[e] = true end
  function f:UnregisterEvent(e) self.events[e] = nil end
  function f:SetScript(kind, fn) if kind == "OnEvent" then self.handler = fn end end
  function f:GetScript(kind) return kind == "OnEvent" and self.handler or nil end
  function f:Show() end
  function f:Hide() end
  table.insert(frames, f)
  return f
end
function CreateFrame() return NewFrame() end

time = os.time
GetTime = function() return 1000 end
C_Timer = {
  After = function() end,
  NewTicker = function() return {Cancel = function() end} end,
}

-- A key is running: the client says so, but MA.state does not know yet,
-- which is exactly the post-reload situation.
C_ChallengeMode = {
  GetActiveChallengeMapID = function() return 501 end,
  GetMapUIInfo = function() return "Murder Row", nil, 1800 end,
  GetActiveKeystoneInfo = function() return 10, {} end,
  GetChallengeCompletionInfo = function() return nil end,
  GetDeathCount = function() return 0, 0 end,
}
C_Scenario = {GetStepInfo = function() return "step", 1, 1 end}
C_ScenarioInfo = {
  GetCriteriaInfo = function() return nil end,
}
GetWorldElapsedTime = function() return 1, 900 end
LoggingCombat = function() return true end
hooksecurefunc = function() end
UnitGUID = function() return "Player-1-0001" end

local MA = {keyEventFrames = {}}
MA.db = {debugMode = false, showChestTimer = true, showBossSplits = true,
         bestSplits = {}, runHistory = {}, saveRunHistory = true,
         warnLoggingConflicts = false, announceCompletion = false,
         overlayPosition = {point = "CENTER", relativePoint = "CENTER", x = 0, y = 0}}
function MA:GetDB() return self.db end
function MA:Debug() end
function MA:IsDebugMode() return false end
function MA:GetSpellDB() return {} end
function MA:RegisterKeyEventFrame(frame) table.insert(self.keyEventFrames, frame) end
function MA:MeterUtil_ApiAvailable() return false end
MA.state = {}

-- Load Bootstrap first (it owns the dispatcher), then the modules whose
-- state the reload used to lose.
local ORDER = {
  "Bootstrap", "CombatLogging", "ChestTimer", "Tracker",
}
for _, name in ipairs(ORDER) do
  local chunk = assert(loadfile("addon/Postmortem/" .. name .. ".lua"))
  chunk("Postmortem", MA)
end

-- Find the tracker's frame: the one that listens for PLAYER_ENTERING_WORLD.
local trackerFrame
for _, f in ipairs(frames) do
  if f.events["PLAYER_ENTERING_WORLD"] then trackerFrame = f end
end
assert(trackerFrame, "Tracker.lua registered no PLAYER_ENTERING_WORLD frame")

-- The reload: the client is mid-key, MA.state is empty.
MA.state = {}
trackerFrame.handler(trackerFrame, "PLAYER_ENTERING_WORLD")

assert(MA.state.active, "the tracker did not recover the run at all")
print("ok: the tracker recovers the run")

assert(MA.state.chestTimer, "chestTimer was not rebuilt -- no countdown for the rest of the key")
assert(MA.state.chestTimer.timeLimit == 1800, "the chest timer came back with the wrong limit")
print("ok: the chest timer is rebuilt after a reload")

assert(MA.state.splits, "splits table was not rebuilt")
print("ok: the splits table is rebuilt after a reload")

-- Finishing the key must still work, and must not throw.
local ok, err = pcall(function()
  trackerFrame.handler(trackerFrame, "CHALLENGE_MODE_COMPLETED")
end)
assert(ok, "finishing the recovered key threw: " .. tostring(err))
print("ok: the recovered key finishes cleanly")

-- A second start must not wipe a live run (the broadcast reaches the
-- tracker's own frame, and a duplicate from the client would too).
MA.state = {}
trackerFrame.handler(trackerFrame, "PLAYER_ENTERING_WORLD")
MA.state.chestTimer.marker = "survived"
trackerFrame.handler(trackerFrame, "CHALLENGE_MODE_START")
assert(MA.state.chestTimer and MA.state.chestTimer.marker == "survived",
  "a duplicate start wiped the live run's state")
print("ok: a duplicate start does not restart a live run")

print("all reload-recovery checks passed")
