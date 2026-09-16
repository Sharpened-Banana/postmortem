-- TankDeath.lua
-- Live tank death post-mortem: the moment the local player dies, record
-- which of their spec's defensives were sitting ready and unused, and which
-- they were actually holding.
--
-- This is the in-game half of analysis/tank_death.py. The two share one
-- data table (TankDefensives.lua, generated from the same JSON the analyzer
-- loads) and the same honesty rule -- say nothing rather than say something
-- wrong -- but they are NOT redundant, because each can see something the
-- other cannot:
--
--   * The analyzer sees the whole run and exact damage numbers, but the
--     combat log carries no spellbook, so it cannot tell "never talented"
--     apart from "had it, never pressed it" (see tank_death.py's header).
--   * This file cannot see damage at all -- patch 12.0's Secret Values put
--     health, absorbs and incoming damage permanently out of addon reach --
--     but it CAN ask the client whether a spell is known and whether it is
--     off cooldown. That resolves exactly the ambiguity the analyzer is
--     stuck with.
--
-- So the live capture is the more precise of the two about availability,
-- and the log is the more precise about what killed you. The overlay shows
-- this one immediately; the uploaded report carries the other.
--
-- Nothing here reads a combat value. Cooldown and spellbook state are the
-- player's own, are not secret, and are explicitly sanctioned -- see the
-- Blizzard API notes in InterruptDatabase.lua's header for the same
-- distinction drawn from the other side. Every read is still wrapped and
-- tolerant of a nil/secret return, per this addon's standing rule that a
-- read may simply fail.

local ADDON_NAME, MA = ...

-- Extra seconds a spell must have been off cooldown before it is called
-- "ready and unused". Mirrors READY_MARGIN_S in analysis/tank_death.py so
-- the live overlay and the uploaded report don't disagree on a borderline
-- case; here it also absorbs the gap between the killing blow landing and
-- PLAYER_DEAD firing.
local READY_MARGIN_S = 3.0

-- How far back a "was I holding it" check looks when the client can't tell
-- us directly: a cast inside its own duration counts as held. Same idea as
-- the analyzer's active_at_death.
local HELD_GRACE_S = 0.5

-- spellID -> GetTime() of the last successful cast by the player this run.
local lastCastAt = {}

-- Builds the post-mortem for the death that just happened.
--
-- Returns nil when there is nothing honestly sayable: the player isn't a
-- tank spec we cover, or the table isn't loaded. A partially-readable
-- client degrades field by field rather than losing the whole record --
-- a defensive whose cooldown wouldn't read is simply left out.
--
-- `known` and `notKnown` are recorded even though nothing in-game displays
-- them: they are the spellbook snapshot the analyzer cannot obtain from a
-- combat log, and the whole point of persisting this capture. See
-- TankDeath_Persist below.
local function BuildPostMortem()
  local T = MA.TankDefensives
  if not T or not T.bySpec then return nil end

  local specID = MA:TankUtil_PlayerSpecID()
  if not specID then return nil end
  local entries = T.bySpec[specID]
  if not entries then return nil end

  local now = GetTime()
  local readyUnused, held, known, notKnown = {}, {}, {}, {}

  for _, entry in ipairs(entries) do
    local isKnown = MA:TankUtil_IsKnown(entry.id)
    -- Only reason about spells the client confirms the player has. An
    -- untalented spell is skipped entirely rather than reported as
    -- "unused", and an unreadable answer (nil) is skipped from BOTH lists
    -- rather than guessed into either.
    if isKnown == true then
      known[#known + 1] = entry.id
    elseif isKnown == false then
      notKnown[#notKnown + 1] = entry.id
    end

    if isKnown then
      local castAt = lastCastAt[entry.id]
      local wasHeld = castAt ~= nil
          and entry.duration > 0
          and (castAt + entry.duration + HELD_GRACE_S) >= now

      if wasHeld then
        held[#held + 1] = { id = entry.id, name = entry.name }
      elseif entry.cooldown > 0 then
        -- cooldown == 0 means resource-gated (rage, Holy Power, runes):
        -- the cooldown API says it's "ready" even when the player had no
        -- resource to press it with, so claiming it would be a guess.
        local remaining = MA:TankUtil_CooldownRemaining(entry.id)
        if remaining ~= nil and remaining <= 0 then
          local readyFor
          if castAt then readyFor = now - (castAt + entry.cooldown) end
          if readyFor == nil or readyFor >= READY_MARGIN_S then
            readyUnused[#readyUnused + 1] = {
              id = entry.id, name = entry.name,
              category = entry.category, readyFor = readyFor,
            }
          end
        end
      end
    end
  end

  -- A record with no findings is still worth keeping when we learned
  -- something about the spellbook: "you knew all six and used none" is
  -- exactly what the analyzer wants, and discarding it would throw that
  -- away. Only a record that says nothing at all is dropped.
  if #readyUnused == 0 and #held == 0 and #known == 0 then return nil end
  return {
    specID = specID,
    specName = T.specNames and T.specNames[specID] or nil,
    readyUnused = readyUnused,
    held = held,
    known = known,
    notKnown = notKnown,
  }
end

-- How many death records to keep in the SavedVariable. This grows across
-- every key forever otherwise, and the analyzer only ever needs recent
-- builds -- a spellbook snapshot from six months ago describes a
-- character that no longer exists.
local MAX_PERSISTED_DEATHS = 200

-- Appends one record to PostmortemTankDB for the Python side to read back.
-- Wall-clock (time()) rather than GetTime(), because the analyzer has to
-- line these up against combat-log timestamps, and GetTime() is a
-- session-relative monotonic clock that means nothing outside the client.
local function Persist(result)
  local db = MA.tankDb
  if type(db) ~= "table" then return end
  if type(db.deaths) ~= "table" then db.deaths = {} end

  db.deaths[#db.deaths + 1] = {
    ts = time(),
    specID = result.specID,
    known = result.known,
    notKnown = result.notKnown,
    readyUnused = result.readyUnused,
    held = result.held,
  }

  -- Trim oldest-first, in place.
  local overflow = #db.deaths - MAX_PERSISTED_DEATHS
  if overflow > 0 then
    for i = 1, #db.deaths - overflow do
      db.deaths[i] = db.deaths[i + overflow]
    end
    for i = #db.deaths, #db.deaths - overflow + 1, -1 do
      db.deaths[i] = nil
    end
  end
end

-- MA-facing entry point, so Overlay.lua and the tests can drive this
-- without the event frame.
function MA:TankDeath_Capture()
  local ok, result = pcall(BuildPostMortem)
  if not ok then
    MA:Debug("TankDeath: errored building post-mortem -- %s", tostring(result))
    return nil
  end
  if not result then return nil end

  -- lastTankDeath drives the overlay's recap line; tankDeaths keeps every
  -- death this key, so the results window can show a key with four deaths
  -- as four deaths rather than only the last one.
  MA.state.lastTankDeath = result
  if type(MA.state.tankDeaths) ~= "table" then MA.state.tankDeaths = {} end
  MA.state.tankDeaths[#MA.state.tankDeaths + 1] = result

  -- Persisting is independent of the display toggle on purpose: the
  -- capture is what the analyzer reads back, and a player who turned off
  -- the on-screen line has not asked for a worse report. The toggle is
  -- checked where the line is drawn (Overlay.lua), not here.
  local persisted, err = pcall(Persist, result)
  if not persisted then
    MA:Debug("TankDeath: could not persist -- %s", tostring(err))
  end

  MA:Debug("TankDeath: %d ready and unused, %d held, %d known",
    #result.readyUnused, #result.held, #result.known)
  if MA.Overlay_Refresh then MA.Overlay_Refresh(MA) end
  return result
end

-- Every tank post-mortem recorded so far this key, oldest first.
function MA:TankDeath_All()
  return MA.state.tankDeaths or {}
end

-- Clears per-run state. Called on key start so one key's casts can't leak
-- into the next key's post-mortem. Deliberately does NOT clear
-- PostmortemTankDB -- that is the cross-key capture the analyzer reads.
function MA:TankDeath_Reset()
  lastCastAt = {}
  MA.state.lastTankDeath = nil
  MA.state.tankDeaths = {}
end

local eventFrame = CreateFrame("Frame")
MA:RegisterKeyEventFrame(eventFrame)
-- RegisterUnitEvent rather than a filtered RegisterEvent: this only ever
-- cares about the player's own casts, and the unit-filtered form means the
-- client never wakes us for the other four group members' casts.
eventFrame:RegisterUnitEvent("UNIT_SPELLCAST_SUCCEEDED", "player")
eventFrame:RegisterEvent("PLAYER_DEAD")
eventFrame:RegisterEvent("CHALLENGE_MODE_START")
eventFrame:RegisterEvent("CHALLENGE_MODE_RESET")

eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "UNIT_SPELLCAST_SUCCEEDED" then
    local _, _, spellID = ...
    if type(spellID) == "number" then
      lastCastAt[spellID] = GetTime()
    end
  elseif event == "PLAYER_DEAD" then
    -- PLAYER_DEAD fires for the local player at the moment of death, so
    -- cooldown state read here is the state at death. DeathTagging.lua's
    -- polling delay exists because the *Deaths meter* needs time to
    -- settle; nothing here depends on that meter, so nothing here waits.
    MA:TankDeath_Capture()
  elseif event == "CHALLENGE_MODE_START" or event == "CHALLENGE_MODE_RESET" then
    MA:TankDeath_Reset()
  end
end)
