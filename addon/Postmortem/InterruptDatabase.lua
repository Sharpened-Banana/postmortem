-- InterruptDatabase.lua
-- Self-growing "is this spell interruptible" database. The combat log the
-- Python side parses has no such flag -- it currently guesses via a
-- heuristic (a spell only counts as "kickable" once someone has actually
-- kicked it at least once), which wrongly flags genuinely-uninterruptible
-- casts as missed kicks. The client itself knows this live, via
-- UnitCastingInfo()/UnitChannelInfo()'s notInterruptible return value, so
-- this file watches every enemy cast during a key and records that flag per
-- spellID into PostmortemSpellDB (via MA:GetSpellDB(), see
-- Bootstrap.lua) -- a real, always-accurate database built purely from play.
--
-- Cast-watching pattern (one shared frame with plain RegisterEvent on
-- UNIT_SPELLCAST_START/UNIT_SPELLCAST_CHANNEL_START, filtered against a
-- hash-table of tracked unit tokens maintained via NAME_PLATE_UNIT_ADDED/
-- REMOVED) verified this session against real, currently-installed addon
-- source: real addons deliberately avoid RegisterUnitEvent per-nameplate
-- (which would mean registering/unregistering per unit token, up to ~40
-- separate registrations) in favor of exactly this single-frame, filtered
-- pattern. UnitCastingInfo/UnitChannelInfo return-value shapes (including
-- that UnitChannelInfo has no castID slot, so notInterruptible/spellID sit
-- one position earlier than in UnitCastingInfo) also confirmed this
-- session -- see the two local functions below.
--
-- Gating structure (register the high-frequency events only while a key is
-- active, via CHALLENGE_MODE_START/COMPLETED/RESET on their own frame)
-- mirrors Interrupts.lua's COMBAT_LOG_EVENT_UNFILTERED gating exactly.

local ADDON_NAME, MA = ...

-- Nameplate/boss unit tokens currently worth watching for casts. Populated
-- by NAME_PLATE_UNIT_ADDED/REMOVED for nameplates; boss1-boss8 are added
-- once below and never removed -- dungeon bosses aren't always on a visible
-- nameplate, but their unit tokens are always valid to query.
local trackedUnits = {}
for i = 1, 8 do
  trackedUnits["boss" .. i] = true
end

-- Records (or updates) one spell's interruptible flag. Shared by all three
-- call sites below (both cast-start event handlers plus the
-- NAME_PLATE_UNIT_ADDED mid-cast probe) so the recording logic -- and its
-- "only write on change" discipline -- exists in exactly one place.
-- notInterruptible is WoW's own true-means-CANNOT-interrupt flag; this
-- stores the more intuitive `interruptible = not notInterruptible` so the
-- SavedVariables data reads naturally and the Python side doesn't have to
-- double-negate.
local function RecordCast(name, notInterruptible, spellID)
  if not spellID or not name then return end

  local db = MA:GetSpellDB()
  if not db then return end

  local interruptible = not notInterruptible
  local existing = db[spellID]

  -- Only write when the entry is new or the stored flag actually changed --
  -- same "only write on change" discipline as CombatLogging.lua's
  -- CombatLogging_SetState, to avoid needless SavedVariables churn on every
  -- repeat cast of an already-known spell.
  if existing and existing.interruptible == interruptible then
    return
  end

  db[spellID] = { name = name, interruptible = interruptible, lastSeenTs = time() }
end

-- name, text, texture, startTime, endTime, isTradeSkill, castID,
-- notInterruptible, spellID -- verified return shape for UnitCastingInfo.
local function RecordUnitCast(unit)
  local name, _, _, _, _, _, _, notInterruptible, spellID = UnitCastingInfo(unit)
  RecordCast(name, notInterruptible, spellID)
end

-- name, text, texture, startTime, endTime, isTradeSkill, notInterruptible,
-- spellID -- no castID slot (unlike UnitCastingInfo), so notInterruptible/
-- spellID each sit one position earlier. Verified return shape for
-- UnitChannelInfo.
local function RecordUnitChannel(unit)
  local name, _, _, _, _, _, notInterruptible, spellID = UnitChannelInfo(unit)
  RecordCast(name, notInterruptible, spellID)
end

-- A mob can already be mid-cast the moment its nameplate first appears, so
-- probe immediately on NAME_PLATE_UNIT_ADDED rather than only reacting to
-- future UNIT_SPELLCAST_START/CHANNEL_START events -- otherwise an
-- already-in-progress cast would never get recorded.
local function ProbeUnitForInProgressCast(unit)
  local name, _, _, _, _, _, _, notInterruptible, spellID = UnitCastingInfo(unit)
  if name then
    RecordCast(name, notInterruptible, spellID)
    return
  end

  local channelName, _, _, _, _, _, channelNotInterruptible, channelSpellID = UnitChannelInfo(unit)
  if channelName then
    RecordCast(channelName, channelNotInterruptible, channelSpellID)
  end
end

-- Our own event frame for the cast-watching events, separate from
-- Interrupts.lua's/Tracker.lua's per this addon's decoupled-via-MA-table
-- style (see Interrupts.lua's own comment on this).
local castEventFrame = CreateFrame("Frame")

local function RegisterCastEvents()
  castEventFrame:RegisterEvent("UNIT_SPELLCAST_START")
  castEventFrame:RegisterEvent("UNIT_SPELLCAST_CHANNEL_START")
  castEventFrame:RegisterEvent("NAME_PLATE_UNIT_ADDED")
  castEventFrame:RegisterEvent("NAME_PLATE_UNIT_REMOVED")
end

local function UnregisterCastEvents()
  castEventFrame:UnregisterEvent("UNIT_SPELLCAST_START")
  castEventFrame:UnregisterEvent("UNIT_SPELLCAST_CHANNEL_START")
  castEventFrame:UnregisterEvent("NAME_PLATE_UNIT_ADDED")
  castEventFrame:UnregisterEvent("NAME_PLATE_UNIT_REMOVED")

  -- Clear tracked nameplate unit tokens back down to just the always-
  -- tracked boss1-boss8 set, so no stale unit tokens from this key leak
  -- into the next one.
  trackedUnits = {}
  for i = 1, 8 do
    trackedUnits["boss" .. i] = true
  end
end

castEventFrame:SetScript("OnEvent", function(self, event, unit, ...)
  if event == "NAME_PLATE_UNIT_ADDED" then
    trackedUnits[unit] = true
    ProbeUnitForInProgressCast(unit)
  elseif event == "NAME_PLATE_UNIT_REMOVED" then
    trackedUnits[unit] = nil
  elseif event == "UNIT_SPELLCAST_START" then
    if not trackedUnits[unit] then return end
    RecordUnitCast(unit)
  elseif event == "UNIT_SPELLCAST_CHANNEL_START" then
    if not trackedUnits[unit] then return end
    RecordUnitChannel(unit)
  end
end)

-- Separate frame for CHALLENGE_MODE_START/COMPLETED/RESET, mirroring
-- Interrupts.lua's exact gating structure: these high-frequency unit-cast
-- events are only useful (and only registered) while a key is active.
local challengeModeFrame = CreateFrame("Frame")
challengeModeFrame:RegisterEvent("CHALLENGE_MODE_START")
challengeModeFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")
challengeModeFrame:RegisterEvent("CHALLENGE_MODE_RESET")

function MA:InterruptDB_OnChallengeModeStart()
  RegisterCastEvents()
end

function MA:InterruptDB_OnChallengeModeEnd()
  UnregisterCastEvents()
end

challengeModeFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    MA:InterruptDB_OnChallengeModeStart()
  elseif event == "CHALLENGE_MODE_COMPLETED" or event == "CHALLENGE_MODE_RESET" then
    MA:InterruptDB_OnChallengeModeEnd()
  end
end)
