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
-- ## Engagement detection (rewritten for patch 12.0)
-- This used to detect "a new pull has started" by watching
-- COMBAT_LOG_EVENT_UNFILTERED for group-vs-hostile damage events, forwarded
-- from Interrupts.lua's shared registration. Patch 12.0 removed that event
-- from addons entirely (registering it now fires ADDON_ACTION_FORBIDDEN),
-- so engagement is now read from Blizzard's own built-in damage meter via
-- MeterUtil.lua instead: Enum.DamageMeterType.EnemyDamageTaken (=10, added
-- 12.0.1) lists every enemy the group has damaged, and a distinct-GUID walk
-- of it stands in for "which enemies are engaged" with no combat log access
-- needed. This is polled every tick (once per second, driven from
-- Tracker.lua) rather than event-driven, so pull-boundary detection has up
-- to ~1s of latency it didn't have before -- acceptable for a "which pull
-- am I on" display, not a precision timing signal.

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

local SESSION_OVERALL = 0

local function EnemyDamageMeterType()
  return Enum and Enum.DamageMeterType and Enum.DamageMeterType.EnemyDamageTaken
end

-- gap_seconds: matches this project's Python-side analysis/pulls.py default
-- (gap_seconds=5.0) for the same "how long a lull means a pull has ended"
-- concept -- a simple timer-based version of it is enough here; this file
-- doesn't need pulls.py's exact algorithm.
local PULL_GAP_SECONDS = 5

-- Every enemy GUID already attributed to some pull (this one or an earlier
-- one) this key -- never cleared mid-key, so a corpse lingering in the
-- persistent Overall EnemyDamageTaken session is never re-counted into a
-- later pull. currentPullGUIDs is just this pull's subset of it.
local seenGUIDs = {}
local currentPullGUIDs = {}
local lastActivityTime = nil

local function ResetEngagement()
  currentPullGUIDs = {}
  lastActivityTime = nil
end

-- Snapshot whatever's already in the Overall EnemyDamageTaken session when
-- the key starts (e.g. a hallway pack pulled half a second before
-- CHALLENGE_MODE_START actually fires, or corpses left over from a previous
-- key that never reset the meter) so it isn't miscounted as pull 1.
local function SnapshotBaselineGUIDs()
  seenGUIDs = {}
  local meterType = EnemyDamageMeterType()
  if not meterType then return end
  local session = select(1, MA:MeterUtil_GetSession(SESSION_OVERALL, meterType))
  if not session then return end
  MA:MeterUtil_ForEachSource(session, function(source)
    if source.sourceGUID then seenGUIDs[source.sourceGUID] = true end
  end)
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

-- Called from Tracker.lua's MA:Tracker_OnTick() (once per second while a
-- key is active) -- GetTime() is the standard, long-unchanged Blizzard API
-- for seconds-since-login timing, used the same way for time-delta
-- bookkeeping throughout installed addon source, so no OnUpdate polling of
-- our own is needed here.
function MA:RouteImport_OnTick()
  local route = MA.state.route
  if not route or not route.plannedPulls then return end

  local meterType = EnemyDamageMeterType()
  if meterType then
    local session = select(1, MA:MeterUtil_GetSession(SESSION_OVERALL, meterType))
    if session then
      local sawNewActivity = false
      local complete = MA:MeterUtil_ForEachSource(session, function(source)
        local guid = source.sourceGUID
        if not guid or seenGUIDs[guid] then return end
        seenGUIDs[guid] = true
        currentPullGUIDs[guid] = true
        sawNewActivity = true
      end)
      if complete then
        local count = 0
        for _ in pairs(currentPullGUIDs) do count = count + 1 end
        route.currentPullCloneCount = count
        if sawNewActivity then lastActivityTime = GetTime() end
      end
      -- An incomplete (secret mid-walk) pass just skips this tick's update;
      -- the existing counts and lastActivityTime are left alone and picked
      -- back up next tick.
    end
  end

  if not lastActivityTime or route.currentPullCloneCount == 0 then return end
  if GetTime() - lastActivityTime < PULL_GAP_SECONDS then return end

  -- Pull looks done: compare the live engaged-clone count against this
  -- pull's planned clone count. Coarse signal only -- see the scope note at
  -- the top of this file for why this can't be identity-level deviation
  -- detection.
  local planned = route.plannedPulls[route.currentPullIndex]
  local engagedCount = route.currentPullCloneCount
  route.lastPullSizeDelta = planned and (engagedCount - planned) or nil

  MA:Debug("Route: pull %d done -- %d engaged vs %s planned; now on pull %d",
    route.currentPullIndex, engagedCount, tostring(planned), route.currentPullIndex + 1)
  route.currentPullIndex = route.currentPullIndex + 1
  route.currentPullCloneCount = 0
  ResetEngagement()
end

-- Our own event frame for the route-progress lifecycle (reset + lazily load
-- planned pulls on CHALLENGE_MODE_START, stop engagement bookkeeping on
-- CHALLENGE_MODE_COMPLETED/RESET). Every file registering its own
-- CHALLENGE_MODE_START/COMPLETED/RESET is this addon's existing pattern
-- (CombatLogging.lua, Tracker.lua, Interrupts.lua all already do it).
local eventFrame = CreateFrame("Frame")
MA:RegisterKeyEventFrame(eventFrame)
eventFrame:RegisterEvent("CHALLENGE_MODE_START")
eventFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")
eventFrame:RegisterEvent("CHALLENGE_MODE_RESET")

eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    ResetRouteState()
    SnapshotBaselineGUIDs()
    -- Lazy, on-demand call -- see GetMDTCurrentPreset()'s comment above for
    -- why this must not happen at file-load/ADDON_LOADED time.
    MA.state.route.plannedPulls = MA:RouteImport_GetPlannedPulls()
    if MA.state.route.plannedPulls then
      MA:Debug("Route: MDT's current route has %d pulls -- tracking pull progress", #MA.state.route.plannedPulls)
    else
      MA:Debug("Route: no MDT route found (MDT loaded: %s) -- pull row hidden",
        tostring(MA:IsMDTPresent()))
    end
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
