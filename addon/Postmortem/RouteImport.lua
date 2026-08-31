-- RouteImport.lua
-- Reads Mythic Dungeon Tools' currently-selected route (pull list only, not
-- NPC identities -- see the scope note below) and tracks coarse live
-- pull-count progress against it during a Mythic+ key.
--
-- ## Scope note
-- The original idea for this WP was full NPC-identity-level "is this pack
-- off-route" deviation detection, like the Python tool's analysis/compare.py.
-- That is not achievable from inside the addon: MDT's per-dungeon NPC data
-- (MDT.dungeonEnemies[dungeonIdx], which maps a route pull's enemy_idx
-- values to actual NPC ids) lives on MDT's own private addon table (local
-- addonName, MDT = ... in MythicDungeonTools/Core/Bootstrap.lua) and is
-- never exposed as a global -- confirmed this session by grepping the whole
-- installed MythicDungeonTools addon for any _G.MDT / global-export pattern
-- and finding none. WoW addons also have no filesystem API, so reading
-- MDT's raw dungeon-data files directly isn't possible either.
--
-- What IS readable is the route itself: MDT's currently-selected preset's
-- pull list (enemy_idx -> clone indices per pull, and thus each pull's total
-- clone count). So this file tracks pull-count/clone-count progress only
-- ("pull 4 of 12 planned", plus a coarse size-mismatch signal comparing live
-- engaged-enemy counts to the planned pull's clone count) -- not identity-
-- level deviation flagging (early/off-route/missed), which would need NPC
-- ids this addon cannot obtain.
--
-- This file's COMBAT_LOG_EVENT_UNFILTERED data arrives via
-- Interrupts.lua's shared frame (MA:RouteImport_OnCombatLogEvent), not a
-- registration of its own -- see Interrupts.lua's file header for why.

local ADDON_NAME, MA = ...

-- Returns MDT's currently-selected route preset table, or nil if MDT isn't
-- present or its data isn't in the expected shape.
--
-- This reimplements MDT's own real MDT:GetCurrentPreset() logic
-- (MythicDungeonTools/Modules/Presets.lua:97, real, currently-shipping
-- code: "return db.presets[db.currentDungeonIdx][db.currentPreset[db.currentDungeonIdx]]")
-- defensively, since this reads another addon's SavedVariables (external,
-- untrusted data), not our own -- tolerating any level of it being
-- missing/malformed, the same way this project's Python side treats
-- untrusted external data.
--
-- IMPORTANT: this must only be called lazily, on demand (from our own
-- CHALLENGE_MODE_START handler below), never at ADDON_LOADED/OnInitialize
-- time. Addon load order between separate addons isn't guaranteed --
-- alphabetically "Postmortem" sorts before "MythicDungeonTools", so at
-- the moment our own ADDON_LOADED fires, MDT may not have loaded yet and
-- MythicDungeonToolsDB may not exist as a global. CHALLENGE_MODE_START fires
-- long after login/PLAYER_ENTERING_WORLD, by which point every addon that
-- is going to load has definitely finished loading.
local function GetMDTCurrentPreset()
  if not MA:IsMDTPresent() then return nil end
  if type(MythicDungeonToolsDB) ~= "table" then return nil end
  local db = MythicDungeonToolsDB.global
  if type(db) ~= "table" then return nil end
  local dungeonIdx = db.currentDungeonIdx
  if type(dungeonIdx) ~= "number" then return nil end
  local presetIdx = db.currentPreset and db.currentPreset[dungeonIdx]
  if type(presetIdx) ~= "number" then return nil end
  local presetsForDungeon = db.presets and db.presets[dungeonIdx]
  if type(presetsForDungeon) ~= "table" then return nil end
  local preset = presetsForDungeon[presetIdx]
  if type(preset) ~= "table" then return nil end
  return preset
end

-- Returns an ordered array of clone counts, one per planned pull (e.g.
-- {5, 3, 8, ...}), or nil if no usable route is found. MDT absent, no
-- current preset, and malformed preset data are all treated as the same
-- "nothing to show" case -- not an error.
--
-- Preset shape verified against src/postmortem/mdt/route.py's
-- Route.from_preset()/Pull class, which documents precisely how this
-- project's Python side already round-trips this exact real MDT format:
-- preset.value.pulls is a table keyed by pull index (1, 2, 3, ...), each
-- entry a table keyed by enemy_idx -> {clone_idx, clone_idx, ...} plus a
-- "color" string key mixed in. A pull's clone count is the sum of #clones
-- across all its enemy_idx entries (skipping the "color" key).
function MA:RouteImport_GetPlannedPulls()
  local preset = GetMDTCurrentPreset()
  if type(preset) ~= "table" then return nil end
  local value = preset.value
  if type(value) ~= "table" then return nil end
  local pullsRaw = value.pulls
  if type(pullsRaw) ~= "table" then return nil end

  -- pairs() over pullsRaw can hand back both the numeric pull-index keys we
  -- want and (in principle, for this untrusted table) stray non-numeric
  -- keys -- filter with type(k) == "number" and sort, since Lua's pairs()
  -- makes no ordering guarantee.
  local pullIndices = {}
  for k in pairs(pullsRaw) do
    if type(k) == "number" then
      pullIndices[#pullIndices + 1] = k
    end
  end
  if #pullIndices == 0 then return nil end
  table.sort(pullIndices)

  local plannedPulls = {}
  for _, pullIdx in ipairs(pullIndices) do
    local pullEntry = pullsRaw[pullIdx]
    local cloneCount = 0
    if type(pullEntry) == "table" then
      for enemyKey, clones in pairs(pullEntry) do
        -- Skip the "color" string key (and any other non-numeric metadata
        -- key) mixed into the same table -- only numeric keys are
        -- enemy_idx entries.
        if type(enemyKey) == "number" and type(clones) == "table" then
          -- Count via pairs() rather than the # operator: more tolerant of
          -- a sparse/non-sequential clones table than trusting # would be,
          -- while giving the identical result for a well-formed MDT export.
          for _ in pairs(clones) do
            cloneCount = cloneCount + 1
          end
        end
      end
    end
    plannedPulls[#plannedPulls + 1] = cloneCount
  end

  return plannedPulls
end

-- unit flag bits (COMBATLOG_OBJECT_*) -- see Interrupts.lua's header
-- comment for how the bit.band idiom and these Blizzard-global-with-literal-
-- fallback values were verified against real installed addon source this
-- session. Duplicated here rather than factored out, matching this
-- codebase's existing small-duplication-over-a-shared-utility-file
-- precedent (see Overlay.lua's IsKeyActive() comment).
local band = bit.band
local AFFILIATION_MINE = COMBATLOG_OBJECT_AFFILIATION_MINE or 0x00000001
local AFFILIATION_PARTY = COMBATLOG_OBJECT_AFFILIATION_PARTY or 0x00000002
local AFFILIATION_RAID = COMBATLOG_OBJECT_AFFILIATION_RAID or 0x00000004
local AFFILIATION_GROUP = AFFILIATION_MINE + AFFILIATION_PARTY + AFFILIATION_RAID
local REACTION_HOSTILE = COMBATLOG_OBJECT_REACTION_HOSTILE or 0x00000040
local CONTROL_PLAYER = COMBATLOG_OBJECT_CONTROL_PLAYER or 0x00000100
local TYPE_NPC = COMBATLOG_OBJECT_TYPE_NPC or 0x00000800
local TYPE_GUARDIAN = COMBATLOG_OBJECT_TYPE_GUARDIAN or 0x00002000

-- Ported from src/postmortem/combatlog/events.py's is_hostile_npc():
-- NPC-or-guardian type, hostile reaction, and not currently player-
-- controlled (e.g. via Mind Control).
local function IsHostileNPC(flags)
  if not flags then return false end
  return band(flags, TYPE_NPC + TYPE_GUARDIAN) ~= 0
      and band(flags, REACTION_HOSTILE) ~= 0
      and band(flags, CONTROL_PLAYER) == 0
end

-- Ported from src/postmortem/combatlog/events.py's is_group_owned():
-- "Player, pet or guardian belonging to the group." This is the check the
-- module docstring/comments below already assumed was here ("a hostile GUID
-- first seen taking group damage") but the source's own affiliation was
-- never actually verified -- without it, hostile-on-hostile damage (which
-- does happen from some mechanics: adds that cleave each other, hostile AoE
-- landing on a second enemy, etc.) would miscount an NPC the group never
-- even engaged as a new pull member, inflating currentPullCloneCount.
local function IsGroupOwned(flags)
  if not flags then return false end
  return band(flags, AFFILIATION_GROUP) ~= 0 and band(flags, CONTROL_PLAYER) ~= 0
end

local DAMAGE_SUBEVENTS = {
  SPELL_DAMAGE = true,
  SWING_DAMAGE = true,
  RANGE_DAMAGE = true,
}

-- gap_seconds: matches this project's Python-side analysis/pulls.py default
-- (gap_seconds=5.0) for the same "how long a lull means a pull has ended"
-- concept -- a simple timer-based version of it is enough here; this file
-- doesn't need pulls.py's exact algorithm.
local PULL_GAP_SECONDS = 5

-- Currently-engaged hostile NPC GUIDs for the pull in progress, and the
-- GetTime() of the most recently observed group-vs-hostile damage event.
-- Module-local (not MA.state) since this is transient bookkeeping the
-- overlay never reads directly -- only the derived counts in
-- MA.state.route matter to anything outside this file.
local engagedGUIDs = {}
local engagedCount = 0
local lastEngagementTime = nil

local function ResetEngagement()
  engagedGUIDs = {}
  engagedCount = 0
  lastEngagementTime = nil
end

-- Fresh/empty route state. Set at file load (so MA.state.route is always a
-- safe table for Overlay.lua to read, even before any key has started, and
-- even when no MDT route ever gets loaded) and again on every
-- CHALLENGE_MODE_START.
local function ResetRouteState()
  ResetEngagement()
  MA.state.route = {
    plannedPulls = nil,
    currentPullIndex = 1,
    currentPullCloneCount = 0,
    -- Coarse size-mismatch signal for the most recently closed pull: live
    -- engaged-clone-count minus that pull's planned clone count (positive =
    -- bigger than planned, negative = smaller). nil until a pull has closed
    -- against a known planned pull. Deliberately just a delta number, not an
    -- attempt to explain *why* -- see the scope note at the top of this file.
    lastPullSizeDelta = nil,
  }
end
ResetRouteState()

-- Called by Interrupts.lua's shared COMBAT_LOG_EVENT_UNFILTERED handler for
-- every combat log event during a key. Tracks "currently engaged hostile
-- NPC GUIDs" for the pull in progress: a hostile GUID first seen taking
-- group damage joins the current pull's engaged set once; the pull is
-- considered done once PULL_GAP_SECONDS pass with no further engagement
-- (checked from MA:RouteImport_OnTick(), driven off Tracker.lua's existing
-- once-per-second tick rather than an OnUpdate of our own).
function MA:RouteImport_OnCombatLogEvent(subevent, sourceGUID, sourceFlags, destGUID, destFlags)
  local route = MA.state.route
  if not route or not route.plannedPulls then
    -- No usable route to compare against -- skip the bookkeeping entirely
    -- rather than doing free work on every combat log event (this fires at
    -- very high frequency during any real combat), matching this addon's
    -- existing run-scoped-registration philosophy.
    return
  end
  if not DAMAGE_SUBEVENTS[subevent] then return end
  if not IsGroupOwned(sourceFlags) then return end
  if not destGUID or destGUID == "" then return end
  if not IsHostileNPC(destFlags) then return end

  if not engagedGUIDs[destGUID] then
    engagedGUIDs[destGUID] = true
    engagedCount = engagedCount + 1
    route.currentPullCloneCount = engagedCount
  end
  lastEngagementTime = GetTime()
end

-- Called from Tracker.lua's MA:Tracker_OnTick() (once per second while a
-- key is active, plus on-demand on SCENARIO_CRITERIA_UPDATE/
-- CHALLENGE_MODE_DEATH_COUNT_UPDATED) -- GetTime() is the standard,
-- long-unchanged Blizzard API for seconds-since-login timing, used the same
-- way for time-delta bookkeeping throughout installed addon source (e.g.
-- Details_MythicPlus/inspect.lua), so no OnUpdate polling of our own is
-- needed here either.
function MA:RouteImport_OnTick()
  local route = MA.state.route
  if not route or not route.plannedPulls then return end
  if not lastEngagementTime or engagedCount == 0 then return end

  if GetTime() - lastEngagementTime < PULL_GAP_SECONDS then return end

  -- Pull looks done: compare the live engaged-clone count against this
  -- pull's planned clone count. Coarse signal only -- see the scope note at
  -- the top of this file for why this can't be identity-level deviation
  -- detection.
  local planned = route.plannedPulls[route.currentPullIndex]
  route.lastPullSizeDelta = planned and (engagedCount - planned) or nil

  route.currentPullIndex = route.currentPullIndex + 1
  route.currentPullCloneCount = 0
  ResetEngagement()
end

-- Our own event frame for the route-progress lifecycle (reset + lazily load
-- planned pulls on CHALLENGE_MODE_START, stop engagement bookkeeping on
-- CHALLENGE_MODE_COMPLETED/RESET). Separate from Interrupts.lua's frame --
-- only the COMBAT_LOG_EVENT_UNFILTERED registration is shared (see
-- Interrupts.lua's file header); every file registering its own
-- CHALLENGE_MODE_START/COMPLETED/RESET is this addon's existing pattern
-- (CombatLogging.lua, Tracker.lua, Interrupts.lua all already do it).
local eventFrame = CreateFrame("Frame")
eventFrame:RegisterEvent("CHALLENGE_MODE_START")
eventFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")
eventFrame:RegisterEvent("CHALLENGE_MODE_RESET")

eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    ResetRouteState()
    -- Lazy, on-demand call -- see GetMDTCurrentPreset()'s comment above for
    -- why this must not happen at file-load/ADDON_LOADED time.
    MA.state.route.plannedPulls = MA:RouteImport_GetPlannedPulls()
    -- This frame is created after Tracker.lua's, so Tracker's own
    -- CHALLENGE_MODE_START handler (which calls MA:Tracker_OnTick() ->
    -- MA.Overlay_Refresh() immediately) may already have fired and rendered
    -- once before plannedPulls was set above. Refresh again here rather
    -- than relying on cross-file frame-dispatch ordering, so the "Pull N/M"
    -- row appears immediately instead of waiting for the next tick.
    if MA.Overlay_Refresh then MA.Overlay_Refresh(MA) end
  elseif event == "CHALLENGE_MODE_COMPLETED" or event == "CHALLENGE_MODE_RESET" then
    ResetEngagement()
  end
end)
