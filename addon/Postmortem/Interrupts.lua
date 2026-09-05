-- Interrupts.lua
-- Live interrupt tracker: counts SPELL_INTERRUPT events landed by the
-- player's own group during a Mythic+ key, total and per-player.
--
-- This file also owns the addon's single COMBAT_LOG_EVENT_UNFILTERED
-- registration. RouteImport.lua's pull-progress tracking needs to inspect
-- the same event at the same (very high) frequency, and splitting that into
-- two separate COMBAT_LOG_EVENT_UNFILTERED registrations would double the
-- per-event unpack overhead for no benefit, so as a deliberate exception to
-- this addon's usual separate-frame-per-file style, this file's frame
-- forwards every combat log event to MA:RouteImport_OnCombatLogEvent() (if
-- defined) after handling its own SPELL_INTERRUPT concern. RouteImport.lua
-- still owns its own CHALLENGE_MODE_START/COMPLETED/RESET frame for
-- resetting/loading route state -- only the COMBAT_LOG_EVENT_UNFILTERED
-- registration itself is shared, here.

local ADDON_NAME, MA = ...

-- unit flag bits (COMBATLOG_OBJECT_*). Preferring the real Blizzard globals
-- (with a literal fallback in case a global happens to be unset) is the
-- real, currently-shipping idiom for this -- verified against
-- SpecSage/Modules/Combat.lua:83-85 ("local AFFILIATION_MINE =
-- COMBATLOG_OBJECT_AFFILIATION_MINE or 0x00000001", etc.). The bit values
-- themselves are also cross-checked against this project's own Python side,
-- src/postmortem/combatlog/events.py (AFFILIATION_MINE/PARTY/RAID,
-- TYPE_PLAYER/TYPE_PET/TYPE_GUARDIAN), which reads the identical combat log
-- flag values from log files.
local AFFILIATION_MINE = COMBATLOG_OBJECT_AFFILIATION_MINE or 0x00000001
local AFFILIATION_PARTY = COMBATLOG_OBJECT_AFFILIATION_PARTY or 0x00000002
local AFFILIATION_RAID = COMBATLOG_OBJECT_AFFILIATION_RAID or 0x00000004
local AFFILIATION_GROUP = AFFILIATION_MINE + AFFILIATION_PARTY + AFFILIATION_RAID
local TYPE_PLAYER = COMBATLOG_OBJECT_TYPE_PLAYER or 0x00000400
local TYPE_PET = COMBATLOG_OBJECT_TYPE_PET or 0x00001000
local TYPE_GUARDIAN = COMBATLOG_OBJECT_TYPE_GUARDIAN or 0x00002000

-- bit.band() is a real global table in WoW's Lua environment (LuaJIT's bit
-- library) -- confirmed this session by grepping installed, currently-
-- shipping addons for the idiom real code actually uses:
-- Recount/Tracker.lua:12 ("local bit_band = bit.band"), RaiderIO/core.lua:10
-- ("local band = bit.band"), and SpecSage/Modules/Combat.lua:11 (same),
-- all of which then use it directly on COMBATLOG_OBJECT_* flag values
-- exactly as done below. No hand-rolled modulo-based bitwise-AND is needed.
local band = bit.band

-- Group-member source check for SPELL_INTERRUPT. Deliberately broader than
-- this project's Python-side is_group_player() (which checks TYPE_PLAYER
-- only): a pet/guardian interrupt (e.g. a Hunter's Spell Lock, a Warlock's
-- Optical Blast) is still "our" interrupt and should count, so this checks
-- group affiliation plus (player OR pet OR guardian) type.
local function IsGroupSource(flags)
  if not flags then return false end
  if band(flags, AFFILIATION_GROUP) == 0 then return false end
  return band(flags, TYPE_PLAYER + TYPE_PET + TYPE_GUARDIAN) ~= 0
end

-- Fresh/empty interrupts state. Set at file load (so MA.state.interrupts is
-- always a safe table for Overlay.lua to read, even before any key has
-- started) and again on every CHALLENGE_MODE_START.
local function ResetInterrupts()
  MA.state.interrupts = { total = 0, byPlayer = {} }
end
ResetInterrupts()

local function HandleSpellInterrupt(sourceName, sourceFlags)
  if not IsGroupSource(sourceFlags) then return end

  -- Defensive re-init: MA.state itself can be replaced wholesale by
  -- Tracker.lua's StartRun() (MA.state = NewState()), so never trust a
  -- cached reference to the old MA.state.interrupts table -- always read/
  -- lazily recreate it fresh off the live MA.state.
  MA.state.interrupts = MA.state.interrupts or { total = 0, byPlayer = {} }
  local interrupts = MA.state.interrupts
  interrupts.total = (interrupts.total or 0) + 1
  local name = sourceName or "Unknown"
  interrupts.byPlayer[name] = (interrupts.byPlayer[name] or 0) + 1
  MA:Debug("Interrupts: %s kicked (%d this run)", name, interrupts.total)

  if MA.Overlay_Refresh then MA.Overlay_Refresh(MA) end
end

-- Our own event frame, separate from Tracker.lua's/CombatLogging.lua's per
-- this addon's decoupled-via-MA-table style -- confirmed fine for multiple
-- frames to each independently register the same CHALLENGE_MODE_* event
-- names, since that's exactly how CombatLogging.lua and Tracker.lua already
-- coexist.
local eventFrame = CreateFrame("Frame")

-- COMBAT_LOG_EVENT_UNFILTERED fires on every combat log line addon-wide,
-- not just in Mythic+, so (like Tracker.lua's SCENARIO_CRITERIA_UPDATE) it
-- is only registered while a key is actually active.
local function RegisterCombatLogEvent()
  eventFrame:RegisterEvent("COMBAT_LOG_EVENT_UNFILTERED")
end

local function UnregisterCombatLogEvent()
  eventFrame:UnregisterEvent("COMBAT_LOG_EVENT_UNFILTERED")
end

MA:RegisterKeyEventFrame(eventFrame)
eventFrame:RegisterEvent("CHALLENGE_MODE_START")
eventFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")
eventFrame:RegisterEvent("CHALLENGE_MODE_RESET")

eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    ResetInterrupts()
    RegisterCombatLogEvent()
    MA:Debug("Interrupts: listening to the combat log (counts kicks by you/party/raid)")
  elseif event == "CHALLENGE_MODE_COMPLETED" or event == "CHALLENGE_MODE_RESET" then
    UnregisterCombatLogEvent()
    MA:Debug("Interrupts: stopped -- %d kicks this run", (MA.state.interrupts and MA.state.interrupts.total) or 0)
  elseif event == "COMBAT_LOG_EVENT_UNFILTERED" then
    -- CombatLogGetCurrentEventInfo() is the standard, current, real API for
    -- unpacking a COMBAT_LOG_EVENT_UNFILTERED event -- verified as the idiom
    -- real addons use in Details_MythicPlus/parser.lua. Full shape:
    -- timestamp, subevent, hideCaster, sourceGUID, sourceName, sourceFlags,
    -- sourceRaidFlags, destGUID, destName, destFlags, destRaidFlags,
    -- ...(subevent-specific args). Only the fields this WP needs are kept.
    local _, subevent, _, sourceGUID, sourceName, sourceFlags, _, destGUID, _, destFlags =
        CombatLogGetCurrentEventInfo()

    if subevent == "SPELL_INTERRUPT" then
      -- SPELL_INTERRUPT real shape verified against
      -- Details_MythicPlus/parser.lua:58 (real, currently-shipping code):
      -- sourceGUID, sourceName, sourceFlags, sourceRaidFlags, targetGUID,
      -- targetName, targetFlags, targetRaidFlags, spellId, spellName,
      -- spellType, extraSpellID, extraSpellName, extraSchool -- this WP only
      -- needs the source identity, already unpacked above.
      HandleSpellInterrupt(sourceName, sourceFlags)
    end

    -- Shared-frame forward to RouteImport.lua (see file header). Guarded
    -- the same way this addon already loosely couples files through MA
    -- (e.g. Tracker.lua's "if MA.Overlay_Refresh then ... end").
    if MA.RouteImport_OnCombatLogEvent then
      MA:RouteImport_OnCombatLogEvent(subevent, sourceGUID, sourceFlags, destGUID, destFlags)
    end
  end
end)
