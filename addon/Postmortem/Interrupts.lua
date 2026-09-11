-- Interrupts.lua
-- Live interrupt tracker: counts interrupts landed by the player's own
-- group during a Mythic+ key, total and per-player.
--
-- Patch 12.0 removed COMBAT_LOG_EVENT_UNFILTERED from addons entirely --
-- the API wiki is explicit: "No longer accessible to AddOns... Fires
-- ADDON_ACTION_FORBIDDEN when attempting to register this event." This file
-- used to count SPELL_INTERRUPT combat log events directly; that threw a
-- forbidden-action error on every real key start. It now polls Blizzard's
-- own built-in damage meter instead, via MeterUtil.lua, the same
-- C_DamageMeter API AvoidableDatabase.lua already uses for avoidable-damage
-- capture. Enum.DamageMeterType.Interrupts (=5): each combatSource is one
-- unit that landed interrupts, and .totalAmount is an interrupt COUNT (not
-- an amount) -- confirmed against Hindsight/Rotation.lua and
-- EllesmereUIDamageMeters.lua's own live-verified comments. Real
-- limitation, unavoidable with this API: no target or interrupted-spell
-- field exists on this meter, so only total-and-per-player counts are
-- possible -- the same scope this file already had.
--
-- The Overall session persists across keys (nothing here ever resets it),
-- so a per-source baseline count is snapshotted at CHALLENGE_MODE_START and
-- subtracted every tick -- the same "seenBeforeKey" idea
-- AvoidableDatabase.lua already uses for its own Overall-session carryover
-- problem. If the baseline snapshot fails (still secret right at key
-- start), this falls back to counting from zero this key, same
-- carried-over-count risk AvoidableDatabase.lua accepts for the same
-- reason.
--
-- RouteImport.lua used to also ride this file's shared
-- COMBAT_LOG_EVENT_UNFILTERED registration for its own pull-engagement
-- tracking -- that forwarding is gone too. See RouteImport.lua's own header
-- for its meter-based replacement.

local ADDON_NAME, MA = ...

local SESSION_OVERALL = 0

local function InterruptsMeterType()
  return Enum and Enum.DamageMeterType and Enum.DamageMeterType.Interrupts
end

-- Per-sourceGUID interrupt count already on the Overall session when this
-- key started, or nil if it couldn't be read (see the header note above).
local baseline = nil

-- Fresh/empty interrupts state. Set at file load (so MA.state.interrupts is
-- always a safe table for Overlay.lua to read, even before any key has
-- started) and again on every CHALLENGE_MODE_START -- MA.state itself is
-- replaced wholesale by Tracker.lua's StartRun(), so a cached reference to
-- the old table would go stale.
local function ResetInterrupts()
  MA.state.interrupts = { total = 0, byPlayer = {} }
end
ResetInterrupts()

local function SnapshotBaseline()
  baseline = nil
  local meterType = InterruptsMeterType()
  if not meterType then return end
  local session = select(1, MA:MeterUtil_GetSession(SESSION_OVERALL, meterType))
  if not session then return end
  local snapshot = {}
  local complete = MA:MeterUtil_ForEachSource(session, function(source)
    if source.sourceGUID then
      snapshot[source.sourceGUID] = source.totalAmount or 0
    end
  end)
  if complete then baseline = snapshot end
end

-- Called from Tracker.lua's MA:Tracker_OnTick() (once per second while a
-- key is active) -- same guarded-call idiom as MA.RouteImport_OnTick.
function MA:Interrupts_OnTick()
  local meterType = InterruptsMeterType()
  if not meterType then return end
  local session = select(1, MA:MeterUtil_GetSession(SESSION_OVERALL, meterType))
  if not session then return end

  local total, byPlayer = 0, {}
  local complete = MA:MeterUtil_ForEachSource(session, function(source)
    local guid = source.sourceGUID
    local name = source.name
    if not guid or type(name) ~= "string" then return end
    local live = source.totalAmount or 0
    local base = (baseline and baseline[guid]) or 0
    local delta = live - base
    if delta > 0 then
      total = total + delta
      byPlayer[name] = (byPlayer[name] or 0) + delta
    end
  end)
  -- A secret mid-walk means this pass's numbers are unreliable -- keep the
  -- previous tick's values on screen and just retry next tick, rather than
  -- flashing a wrong (likely lower) count.
  if not complete then return end

  MA.state.interrupts = MA.state.interrupts or { total = 0, byPlayer = {} }
  MA.state.interrupts.total = total
  MA.state.interrupts.byPlayer = byPlayer
end

local eventFrame = CreateFrame("Frame")
MA:RegisterKeyEventFrame(eventFrame)
eventFrame:RegisterEvent("CHALLENGE_MODE_START")
eventFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")
eventFrame:RegisterEvent("CHALLENGE_MODE_RESET")

eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    ResetInterrupts()
    SnapshotBaseline()
    MA:Debug("Interrupts: tracking via C_DamageMeter (baseline %s)",
      baseline and "captured" or "unavailable -- counting from zero this key")
  elseif event == "CHALLENGE_MODE_COMPLETED" or event == "CHALLENGE_MODE_RESET" then
    MA:Debug("Interrupts: stopped -- %d kicks this run", (MA.state.interrupts and MA.state.interrupts.total) or 0)
  end
end)
