-- DeathTagging.lua
-- Tags the local player's most recent death with the actual killing spell,
-- and whether it's avoidable (cross-referenced against
-- AvoidableDatabase.lua's harvested spell list), for Overlay.lua's post-key
-- recap to show.
--
-- This is real per-death precision, not a heuristic. Deaths meter rows
-- (Enum.DamageMeterType.Deaths=9) carry a `deathRecapID` handle per death
-- (opaque, not a 1-based index -- confirmed this session:
-- Hindsight/MM_Core.lua:455-491, "C_DeathRecap.GetRecapEvents(1) returns
-- nil. Recap ids are not an index starting at 1."). Feeding that handle into
-- C_DeathRecap.HasRecapEvents/GetRecapEvents (real, working code in
-- Details_MythicPlus/segments.lua:70-79) returns the actual kill-event list
-- for that specific death, including each event's spellId/amount.
--
-- deathTimeSeconds is secret while in combat, but deathRecapID itself reads
-- fine immediately after death per Hindsight's own measured comment -- still
-- polled briefly (not the long AvoidableDatabase.lua-style 60s poll; a few
-- seconds is enough) since Secret Values discipline means never assuming a
-- read succeeds on the first try.

local ADDON_NAME, MA = ...

local SESSION_CURRENT = 1
local SESSION_OVERALL = 0

local INITIAL_DELAY_S = 1
local POLL_INTERVAL_S = 1
local POLL_MAX_TRIES = 10

local function DeathsMeterType()
  return Enum and Enum.DamageMeterType and Enum.DamageMeterType.Deaths
end

-- Resolves a display name for a spell id, tolerating both the modern
-- C_Spell.GetSpellInfo (returns a table) and the legacy global GetSpellInfo
-- (returns a name first) -- same tolerant pattern as AvoidableDatabase.lua's
-- own SpellName(), duplicated rather than shared (this addon's existing
-- small-duplication-over-a-shared-utility-file precedent, e.g.
-- RouteImport.lua's flag-bit constants).
local function SpellName(spellID)
  if C_Spell and C_Spell.GetSpellInfo then
    local info = C_Spell.GetSpellInfo(spellID)
    if type(info) == "table" and type(info.name) == "string" then
      return info.name
    end
  elseif GetSpellInfo then
    local name = GetSpellInfo(spellID)
    if type(name) == "string" then return name end
  end
  return nil
end

-- Finds the local player's most recent death row across the Deaths meter
-- (Current session first, falling back to Overall -- the proven order from
-- Hindsight/MM_Core.lua's own LatestRecapID()). Returns deathRecapID or nil.
local function FindLocalPlayerRecapID()
  local meterType = DeathsMeterType()
  if not meterType then return nil end
  for _, sessionType in ipairs({ SESSION_CURRENT, SESSION_OVERALL }) do
    local session = select(1, MA:MeterUtil_GetSession(sessionType, meterType))
    if session then
      local found = nil
      MA:MeterUtil_ForEachSource(session, function(source)
        if found then return end
        local id = source.deathRecapID
        if source.isLocalPlayer and id and id ~= 0 and not MA:MeterUtil_IsSecret(id) then
          found = id
        end
      end)
      if found then return found end
    end
  end
  return nil
end

-- Given a deathRecapID, finds the highest-amount kill event and tags its
-- spell avoidable/not. Returns {spellId, name, avoidable} or nil.
local function TagDeathCause(recapID)
  if not C_DeathRecap or not C_DeathRecap.HasRecapEvents or not C_DeathRecap.GetRecapEvents then
    return nil
  end
  if not C_DeathRecap.HasRecapEvents(recapID) then return nil end
  local events = C_DeathRecap.GetRecapEvents(recapID)
  if type(events) ~= "table" then return nil end

  local bestSpellId, bestAmount = nil, -1
  for i = 1, #events do
    local ev = events[i]
    local spellId = ev.spellId or ev.spellID
    local amount = ev.amount
    if spellId and type(amount) == "number" and amount > bestAmount then
      bestSpellId, bestAmount = spellId, amount
    end
  end
  if not bestSpellId then return nil end

  local avoidableDb = MA:GetAvoidableDB() or {}
  local avoidableEntry = avoidableDb[bestSpellId]
  local name = (avoidableEntry and avoidableEntry.name) or SpellName(bestSpellId) or ("spell:" .. bestSpellId)
  return { spellId = bestSpellId, name = name, avoidable = avoidableEntry ~= nil }
end

-- The ticker currently allowed to write a result, plus the pending
-- C_Timer.After that would create one. Both are needed: the event is
-- group-wide, so two deaths inside INITIAL_DELAY_S used to create two
-- tickers while only the newest was tracked -- the older one then ran
-- forever, and when it hit POLL_MAX_TRIES its CancelPoll() stopped the
-- CURRENT ticker instead of itself. Any wipe triggered it (2026-09-11).
local pollTicker = nil
local pollGeneration = 0

local function CancelPoll()
  if pollTicker then
    pollTicker:Cancel()
    pollTicker = nil
  end
  -- Bumping the generation retires any pending C_Timer.After as well, so
  -- a delay that has already been scheduled cannot still turn into a
  -- ticker after this call.
  pollGeneration = pollGeneration + 1
end

local function PollForCause()
  CancelPoll()
  local tries = 0
  local generation = pollGeneration
  C_Timer.After(INITIAL_DELAY_S, function()
    if generation ~= pollGeneration then return end  -- superseded while waiting
    local ticker
    ticker = C_Timer.NewTicker(POLL_INTERVAL_S, function()
      -- Cancel THIS ticker, never whatever is currently in pollTicker: an
      -- orphan reaching its own limit used to stop the live one instead.
      local function stop()
        ticker:Cancel()
        if pollTicker == ticker then
          pollTicker = nil
        end
      end
      if generation ~= pollGeneration then
        stop()
        return
      end
      tries = tries + 1
      local ok, result = pcall(function()
        local recapID = FindLocalPlayerRecapID()
        if not recapID then return nil end
        return TagDeathCause(recapID)
      end)
      if not ok then
        MA:Debug("DeathTagging: errored (%s) -- giving up on this death", tostring(result))
        stop()
        return
      end
      if result then
        MA.state.lastDeathCause = result
        MA:Debug("DeathTagging: died to %s%s", result.name, result.avoidable and " (avoidable)" or "")
        if MA.Overlay_Refresh then MA.Overlay_Refresh(MA) end
        stop()
      elseif tries >= POLL_MAX_TRIES then
        MA:Debug("DeathTagging: couldn't resolve a death cause after %ds", POLL_MAX_TRIES * POLL_INTERVAL_S)
        stop()
      end
    end)
    pollTicker = ticker
  end)
end

local eventFrame = CreateFrame("Frame")
MA:RegisterKeyEventFrame(eventFrame)
eventFrame:RegisterEvent("CHALLENGE_MODE_START")
eventFrame:RegisterEvent("CHALLENGE_MODE_DEATH_COUNT_UPDATED")
eventFrame:RegisterEvent("CHALLENGE_MODE_RESET")

eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    CancelPoll()
    MA.state.lastDeathCause = nil
  elseif event == "CHALLENGE_MODE_DEATH_COUNT_UPDATED" then
    if MA:GetDB().tagDeaths then PollForCause() end
  elseif event == "CHALLENGE_MODE_RESET" then
    CancelPoll()
  end
end)
