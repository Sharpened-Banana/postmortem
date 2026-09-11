-- Run history must archive real keys, once each, and nothing else.
--
-- RunHistory.lua's handler ignored which event it was handed, and
-- Bootstrap's dispatcher called every registered frame's handler for every
-- event regardless of what that frame asked for. Together: a bare
-- CHALLENGE_MODE_RESET with no key ever started wrote an entry with no
-- zone, level or result; a completion wrote one entry and the reset that
-- always follows wrote the same key a second time; and one debug-mode
-- toggle wrote two junk entries (2026-09-11).
--
-- Run from the repo root:  lua addon/tests/run_history.lua
-- See addon/tests/README.md.

local frames = {}
local function NewFrame()
  local f = {events = {}}
  function f:RegisterEvent(e) self.events[e] = true end
  function f:UnregisterEvent(e) self.events[e] = nil end
  function f:IsEventRegistered(e) return self.events[e] and true or false end
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
C_Timer = {After = function() end, NewTicker = function() return {Cancel = function() end} end}
C_ChallengeMode = {
  GetActiveChallengeMapID = function() return 501 end,
  GetMapUIInfo = function() return "Murder Row", nil, 1800 end,
  GetActiveKeystoneInfo = function() return 10, {} end,
  GetChallengeCompletionInfo = function() return nil end,
  GetDeathCount = function() return 0, 0 end,
}
C_Scenario = {GetStepInfo = function() return "step", 1, 1 end}
C_ScenarioInfo = {GetCriteriaInfo = function() return nil end}
GetWorldElapsedTime = function() return 1, 900 end
LoggingCombat = function() return true end
hooksecurefunc = function() end

-- A fresh addon, loaded the way the client loads it. Returns the table so
-- each scenario starts from a genuinely clean slate -- an addon /reload
-- restarts every file, so a file-local carried between scenarios would
-- hide exactly the bug this is about.
local function LoadAddon()
  frames = {}
  local MA = {keyEventFrames = {}}
  MA.db = {debugMode = false, runHistory = {}, saveRunHistory = true,
           runHistoryLimit = 50, showChestTimer = true, showBossSplits = true,
           bestSplits = {}}
  function MA:GetDB() return self.db end
  function MA:Debug() end
  function MA:GetSpellDB() return {} end
  function MA:MeterUtil_ApiAvailable() return false end
  MA.state = {}
  for _, name in ipairs({"Bootstrap", "ChestTimer", "RunHistory"}) do
    local chunk = assert(loadfile("addon/Postmortem/" .. name .. ".lua"))
    chunk("Postmortem", MA)
  end
  return MA
end

local function Fire(MA, event)
  -- The dispatcher every module's frame actually goes through -- the same
  -- path debug mode uses, which is where the junk entries came from.
  MA:DispatchKeyEvent(event)
end

-- 1. A reset with no key ever started records nothing.
local MA = LoadAddon()
Fire(MA, "CHALLENGE_MODE_RESET")
assert(#MA.db.runHistory == 0,
  "a reset with no key running recorded " .. #MA.db.runHistory .. " entry/entries")
print("ok: a reset with no key running records nothing")

-- 2. A completed key, then its reset, is ONE entry.
MA = LoadAddon()
Fire(MA, "CHALLENGE_MODE_START")
Fire(MA, "CHALLENGE_MODE_COMPLETED")
Fire(MA, "CHALLENGE_MODE_RESET")
assert(#MA.db.runHistory == 1,
  "a completed key recorded " .. #MA.db.runHistory .. " entries, expected 1")
local entry = MA.db.runHistory[1]
assert(entry.completed, "the recorded entry is not marked completed")
assert(entry.zone == "Murder Row", "the entry has no zone: " .. tostring(entry.zone))
assert(entry.level == 10, "the entry has no level: " .. tostring(entry.level))
print("ok: a completed key is recorded exactly once, with its zone and level")

-- 3. An abandoned key (start, then reset with no completion) is recorded.
MA = LoadAddon()
Fire(MA, "CHALLENGE_MODE_START")
Fire(MA, "CHALLENGE_MODE_RESET")
assert(#MA.db.runHistory == 1, "an abandoned key was not recorded")
assert(not MA.db.runHistory[1].completed, "an abandoned key was recorded as completed")
print("ok: an abandoned key is recorded once, as abandoned")

-- 4. Two keys in a row are two entries, not one deduplicated to death.
MA = LoadAddon()
for _ = 1, 2 do
  Fire(MA, "CHALLENGE_MODE_START")
  Fire(MA, "CHALLENGE_MODE_COMPLETED")
  Fire(MA, "CHALLENGE_MODE_RESET")
end
assert(#MA.db.runHistory == 2,
  "two keys recorded " .. #MA.db.runHistory .. " entries, expected 2")
print("ok: a second key is still recorded")

-- 5. The dispatcher only calls frames that asked for the event.
MA = LoadAddon()
local uninterested = CreateFrame()
uninterested:RegisterEvent("SOMETHING_ELSE")
local called = false
uninterested:SetScript("OnEvent", function() called = true end)
MA:RegisterKeyEventFrame(uninterested)
Fire(MA, "CHALLENGE_MODE_START")
assert(not called, "a frame was handed an event it never registered for")
print("ok: the dispatcher honours what each frame registered for")

print("all run-history checks passed")
