-- Leaving a key without finishing it (leave group, hearth, kicked) must end
-- the run like a reset. No CHALLENGE_MODE_COMPLETED/_RESET reaches the
-- client in that case, only a loading screen; before the fix MA.state.active
-- stayed true for the rest of the session, the HUD froze, and
-- CombatLogging's 2 s re-assert ticker kept forcing combat logging ON
-- everywhere the player went next.
--
-- The converse matters as much: a loading screen back INTO the same key
-- (the challenge API is briefly unpopulated right after
-- PLAYER_ENTERING_WORLD) must NOT end the run.
--
-- Run from the repo root:  lua addon/tests/key_abandoned.lua
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

-- Fake clock + scheduler (After, NewTimer, NewTicker, all cancellable).
local now = 1000
GetTime = function() return now end
time = function() return 1700000000 + math.floor(now) end
local scheduled = {}
local function Schedule(delay, fn, interval)
  local entry = {at = now + delay, fn = fn, interval = interval}
  entry.handle = {Cancel = function() entry.cancelled = true end}
  table.insert(scheduled, entry)
  return entry.handle
end
C_Timer = {
  After = function(d, fn) Schedule(d, fn) end,
  NewTimer = function(d, fn) return Schedule(d, fn) end,
  NewTicker = function(d, fn) return Schedule(d, fn, d) end,
}
local function Advance(seconds)
  local deadline = now + seconds
  while true do
    local bestIdx, best
    for i, e in ipairs(scheduled) do
      if not e.cancelled and e.at <= deadline and (not best or e.at < best.at) then
        bestIdx, best = i, e
      end
    end
    if not best then break end
    now = best.at
    if best.interval then
      best.at = best.at + best.interval
    else
      table.remove(scheduled, bestIdx)
    end
    best.fn(best.handle)
  end
  now = deadline
  for i = #scheduled, 1, -1 do
    if scheduled[i].cancelled then table.remove(scheduled, i) end
  end
end

local activeMapID = 501
C_ChallengeMode = {
  GetActiveChallengeMapID = function() return activeMapID end,
  GetMapUIInfo = function() return "Murder Row", nil, 1800 end,
  GetActiveKeystoneInfo = function() return 10, {} end,
  GetChallengeCompletionInfo = function() return nil end,
  GetDeathCount = function() return 0, 0 end,
}
C_Scenario = {GetStepInfo = function() return "step", 1, 0 end}
C_ScenarioInfo = {GetCriteriaInfo = function() return nil end}
GetWorldElapsedTime = function() return 1, 900 end
hooksecurefunc = function() end
C_AddOns = {IsAddOnLoaded = function() return false, false end}
GetCVar = function() return "1" end
SetCVar = function() end
function CopyTable(t) local c = {} for k, v in pairs(t) do c[k] = v end return c end

local loggingOn = false
function LoggingCombat(state)
  if state == nil then return loggingOn end
  loggingOn = state and true or false
  return loggingOn
end

local MA = {}
MA.db = {debugMode = false, combatLoggingEnabled = true, warnLoggingConflicts = false,
         showChestTimer = true, showBossSplits = true, bestSplits = {}}
MA.state = {}
for _, name in ipairs({"Bootstrap", "CombatLogging", "ChestTimer", "Tracker"}) do
  assert(loadfile("addon/Postmortem/" .. name .. ".lua"))("Postmortem", MA)
end
MA.db = MA.db  -- Bootstrap's GetDB reads self.db; ADDON_LOADED is not needed here

local trackerFrame
for _, f in ipairs(frames) do
  if f.events["PLAYER_ENTERING_WORLD"] and f.events["CHALLENGE_MODE_START"] then trackerFrame = f end
end
assert(trackerFrame, "Tracker.lua registered no PLAYER_ENTERING_WORLD frame")
local function PEW() trackerFrame.handler(trackerFrame, "PLAYER_ENTERING_WORLD") end

-- --- 1. hearth out mid-key: the run ends and logging goes OFF ----------
activeMapID = 501
MA:DispatchKeyEvent("CHALLENGE_MODE_START")
assert(MA.state.active, "the key did not start")
assert(loggingOn, "logging should be ON during the key")
Advance(30)

activeMapID = nil   -- hearthed: loading screen into Dornogal
PEW()
Advance(12)
assert(not MA.state.active, "leaving the key did not end the run (HUD stays frozen)")
Advance(10)          -- past CombatLogging's 5 s stop grace
assert(not loggingOn, "combat logging is still forced ON after leaving the key")
Advance(60)
assert(not loggingOn, "the re-assert ticker turned logging back on after the key was left")
print("ok: leaving a key ends the run and stops logging")

-- --- 2. no loading screen at all: the watchdog still ends the run ----
activeMapID = 501
MA:DispatchKeyEvent("CHALLENGE_MODE_START")
assert(MA.state.active and loggingOn, "second key did not start")
Advance(20)
activeMapID = nil
Advance(30)
assert(not MA.state.active, "the watchdog did not end a key that silently went away")
Advance(10)
assert(not loggingOn, "logging stayed on after the watchdog ended the key")
print("ok: the watchdog ends a key that vanished without a loading screen")

-- --- 3. a loading screen back into the SAME key does not end it ------
activeMapID = 501
MA:DispatchKeyEvent("CHALLENGE_MODE_START")
Advance(20)
activeMapID = nil    -- the API is unpopulated for a moment after the zone-in
PEW()
Advance(3)
activeMapID = 501    -- ...and then answers again
Advance(60)
assert(MA.state.active, "a zone-in to the same key ended the run")
assert(loggingOn, "a zone-in to the same key stopped logging")
print("ok: a zone-in to the same key keeps the run")

-- --- 4. a real completion still ends the run exactly once ------------
local resets = 0
local probe = NewFrame()
probe:RegisterEvent("CHALLENGE_MODE_RESET")
probe:SetScript("OnEvent", function(_, e) if e == "CHALLENGE_MODE_RESET" then resets = resets + 1 end end)
MA:RegisterKeyEventFrame(probe)
MA:DispatchKeyEvent("CHALLENGE_MODE_COMPLETED")
activeMapID = nil
PEW()
Advance(60)
assert(not MA.state.active, "completion did not end the run")
assert(resets == 0, "a completed key was also reset by the abandoned-key check")
assert(not loggingOn, "logging not stopped after completion")
print("ok: a completed key is not reset afterwards")

print("all key-abandoned checks passed")
