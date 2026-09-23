-- A /reload inside CombatLogging.lua's 5 s post-key grace window must not
-- leave combat logging on for the rest of the session. The stop used to
-- live only in a file-local timer, which the reload threw away; its due
-- time is now persisted in PostmortemDB.global.stopLoggingAt and finished
-- on load.
--
-- Each "session" below reloads Bootstrap.lua and CombatLogging.lua into a
-- fresh addon table, the way a real /reload restarts every file, while the
-- PostmortemDB global and the client's logging state survive.
--
-- Run from the repo root:  lua addon/tests/logging_stop_reload.lua
-- See addon/tests/README.md.

local frames
local function NewFrame()
  local f = {events = {}}
  function f:RegisterEvent(e) self.events[e] = true end
  function f:UnregisterEvent(e) self.events[e] = nil end
  function f:IsEventRegistered(e) return self.events[e] and true or false end
  function f:SetScript(kind, fn) if kind == "OnEvent" then self.handler = fn end end
  function f:GetScript(kind) return kind == "OnEvent" and self.handler or nil end
  table.insert(frames, f)
  return f
end
function CreateFrame() return NewFrame() end

-- Wall clock and GetTime advance together; the scheduler is per session
-- (a reload drops every pending timer).
local now = 1000
GetTime = function() return now end
time = function() return 1700000000 + math.floor(now) end
local scheduled
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
    if best.interval then best.at = best.at + best.interval else table.remove(scheduled, bestIdx) end
    best.fn(best.handle)
  end
  now = deadline
end

C_ChallengeMode = {GetActiveChallengeMapID = function() return nil end}
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

PostmortemDB = nil

-- One UI load: fresh files, fresh timers, ADDON_LOADED fired.
local function NewSession()
  frames, scheduled = {}, {}
  local MA = {state = {}}
  assert(loadfile("addon/Postmortem/Bootstrap.lua"))("Postmortem", MA)
  assert(loadfile("addon/Postmortem/CombatLogging.lua"))("Postmortem", MA)
  for _, f in ipairs(frames) do
    if f.events["ADDON_LOADED"] then f.handler(f, "ADDON_LOADED", "Postmortem") end
  end
  assert(MA.db and MA.db.combatLoggingEnabled, "SavedVariables did not initialize")
  MA.db.warnLoggingConflicts = false
  return MA
end

-- --- 1. /reload 2 s after finishing a key ----------------------------
local MA = NewSession()
MA:DispatchKeyEvent("CHALLENGE_MODE_START")
assert(loggingOn, "logging should be ON during the key")
Advance(600)
MA:DispatchKeyEvent("CHALLENGE_MODE_COMPLETED")
Advance(2)
assert(loggingOn, "logging must stay ON through the grace window")

MA = NewSession() -- the /reload
Advance(10)
assert(not loggingOn, "a /reload inside the grace window left combat logging ON")
assert(PostmortemDB.global.stopLoggingAt == nil, "stopLoggingAt was not cleared after the stop")
print("ok: a /reload during the grace window still stops logging")

-- --- 2. /reload long after it was due: carried out immediately --------
MA:DispatchKeyEvent("CHALLENGE_MODE_START")
Advance(600)
MA:DispatchKeyEvent("CHALLENGE_MODE_COMPLETED")
Advance(1)
scheduled = {}   -- the client froze: nothing fired, then a reload 60 s on
now = now + 60
MA = NewSession()
Advance(0.1)
assert(not loggingOn, "an overdue stop was not carried out on load")
print("ok: an overdue stop is carried out on load")

-- --- 3. a stale stamp from a long-gone session is ignored -------------
PostmortemDB.global.stopLoggingAt = time() - 3600
loggingOn = true -- the user's own /combatlog in this session
MA = NewSession()
Advance(10)
assert(loggingOn, "a stale stopLoggingAt turned the user's own logging off")
assert(PostmortemDB.global.stopLoggingAt == nil, "a stale stopLoggingAt was not cleared")
print("ok: a stale stamp is cleared, not acted on")

-- --- 4. the normal path clears the stamp; a new key cancels a pending one
loggingOn = false
MA:DispatchKeyEvent("CHALLENGE_MODE_START")
MA:DispatchKeyEvent("CHALLENGE_MODE_COMPLETED")
assert(type(PostmortemDB.global.stopLoggingAt) == "number", "stopLoggingAt not persisted at key end")
Advance(10)
assert(not loggingOn and PostmortemDB.global.stopLoggingAt == nil, "normal stop did not clear the stamp")
MA:DispatchKeyEvent("CHALLENGE_MODE_START")
MA:DispatchKeyEvent("CHALLENGE_MODE_COMPLETED")
Advance(1)
MA = NewSession()
MA:DispatchKeyEvent("CHALLENGE_MODE_START") -- next key inside the window
Advance(10)
assert(loggingOn, "a stop from the previous key landed on the next one")
assert(PostmortemDB.global.stopLoggingAt == nil, "a new key left the old stop stamp behind")
print("ok: a new key cancels the persisted stop")

print("all logging-stop-reload checks passed")
