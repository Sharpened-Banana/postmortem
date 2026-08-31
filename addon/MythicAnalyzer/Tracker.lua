-- Tracker.lua
-- Live run tracker: keeps MA.state up to date with the current Mythic+
-- run's forces progress, elapsed time, and death count/time lost. Driven
-- off Blizzard's own once-per-second ChallengeModeBlock.UpdateTime hook
-- (no OnUpdate polling of our own), plus the scenario/challenge-mode
-- events that fire on real state changes. Overlay.lua reads MA.state and
-- renders it; this file doesn't know or care how it's displayed -- it only
-- calls MA:Overlay_Refresh() if that function exists, matching this
-- addon's loose-coupling-via-shared-MA-table style (see how CombatLogging.lua
-- and Bootstrap.lua only touch each other through MA, never a direct file
-- reference).

local ADDON_NAME, MA = ...

-- True while a Mythic+ key is actively running.
-- verified against EllesmereUIMythicTimer.lua:2760-2769 (real, currently-
-- shipping addon source), cross-referenced against Details_MythicPlus.
local function IsKeyActive()
  if C_ChallengeMode.GetActiveChallengeMapID() then return true end
  -- IsChallengeModeActive() flips false immediately at completion, before
  -- our own CHALLENGE_MODE_COMPLETED handler has necessarily finished, so
  -- fall back to the same instance-type/difficulty check MDT's own
  -- CombatLogging.lua already uses for "am I in a Mythic+ instance".
  -- verified against MythicDungeonTools/Core/CombatLogging.lua:67-69
  -- (real, currently-shipping code): local _, instanceType, difficultyID =
  -- GetInstanceInfo(); partyDifficultyToContent[8] == Mythic+.
  local _, instanceType, difficultyID = GetInstanceInfo()
  return instanceType == "party" and difficultyID == 8
end

-- How long the overlay keeps showing final numbers after a key ends before
-- hiding itself, instead of vanishing the instant IsKeyActive() flips false.
local RECAP_DURATION_S = 15

-- Fresh/empty state shape. Used both on a real CHALLENGE_MODE_START and on
-- the soft-start path from PLAYER_ENTERING_WORLD below, so stale numbers
-- from a previous key never leak into a new one.
local function NewState()
  return {
    active = false,
    forces = { current = 0, total = 0, percent = 0 },
    elapsed = 0,
    deaths = 0,
    deathTimeLost = 0,
    -- Set on CHALLENGE_MODE_COMPLETED/RESET by EndRun() below: a GetTime()
    -- deadline the overlay stays visible until, and whether combat logging
    -- was actually on for this key (set by CombatLogging.lua's own
    -- CHALLENGE_MODE_COMPLETED/RESET handler -- see its comment for why
    -- this is the *real* logging state, not just "did our addon turn it
    -- on"). Both nil outside the post-key recap window.
    recapUntil = nil,
    combatLogWasOn = nil,
  }
end

-- Live run state. Overlay.lua (and anything else) should read this rather
-- than calling the WoW APIs itself.
MA.state = NewState()

-- Refreshes MA.state.forces from C_Scenario/C_ScenarioInfo.
-- info.quantityString is the real, precise, locale-formatted value;
-- info.quantity is a truncated integer, so we parse the string ourselves
-- instead of trusting .quantity.
-- verified against EllesmereUIMythicTimer.lua:604-689 (real, currently-
-- shipping code, esp. the isWeightedProgress/quantityString handling at
-- 658-679), cross-referenced against Details_MythicPlus.
local function UpdateForces()
  local numCriteria = select(3, C_Scenario.GetStepInfo()) or 0
  for i = 1, numCriteria do
    local info = C_ScenarioInfo.GetCriteriaInfo(i)
    if info and info.isWeightedProgress then
      -- Strip a trailing '%'; only convert a ',' to '.' when it's acting as
      -- a decimal separator (present with no '.' already in the string) --
      -- a naive "strip everything but digits/period" would silently mangle
      -- a European-locale value like "45,5" into "455" instead of 45.5.
      -- verified against EllesmereUIMythicTimer.lua:670-676 (real,
      -- currently-shipping code) -- this is that same normalization,
      -- ported exactly rather than the simpler (and locale-incorrect)
      -- strip-non-digits approach.
      local raw = info.quantityString or tostring(info.quantity or 0)
      local normalized = raw:gsub("%%", "")
      if normalized:find(",") and not normalized:find("%.") then
        normalized = normalized:gsub(",", ".")
      end
      MA.state.forces.current = tonumber(normalized) or 0
      MA.state.forces.total = info.totalQuantity or 0
      MA.state.forces.percent = MA.state.forces.total > 0
          and (MA.state.forces.current / MA.state.forces.total * 100)
          or 0
      return
    end
  end
end

-- Called from the Blizzard UpdateTime hook (see InstallTickHook below) once
-- per second while a key is active. Refreshes the full live-state snapshot
-- and asks the overlay to redraw.
function MA:Tracker_OnTick()
  if not MA.state.active then return end

  -- verified against EllesmereUIMythicTimer.lua:733 (real, currently-
  -- shipping code): local elapsed = select(2, GetWorldElapsedTime(1))
  local _, elapsedSeconds = GetWorldElapsedTime(1)
  if elapsedSeconds and elapsedSeconds >= 0 then
    MA.state.elapsed = elapsedSeconds
  end

  -- verified against EllesmereUIMythicTimer.lua:743 (real, currently-
  -- shipping code): local deathCount, timeLost = C_ChallengeMode.GetDeathCount()
  local deathCount, timeLostSeconds = C_ChallengeMode.GetDeathCount()
  MA.state.deaths = deathCount or 0
  MA.state.deathTimeLost = timeLostSeconds or 0

  UpdateForces()

  -- RouteImport.lua's pull-boundary detection is timer-based (a gap since
  -- the last engagement), so it needs to be checked on a regular tick too,
  -- not just when a new combat log event arrives. Reuses this existing
  -- once-per-second tick instead of an OnUpdate of its own; same guarded-
  -- call pattern as Overlay_Refresh below.
  if MA.RouteImport_OnTick then MA.RouteImport_OnTick(MA) end
  if MA.Overlay_Refresh then MA.Overlay_Refresh(MA) end
end

-- Hooks Blizzard's own once-per-second ChallengeModeBlock.UpdateTime so we
-- get a free tick driver instead of running our own OnUpdate loop. Installed
-- once (guarded by tickHookInstalled) since hooksecurefunc would otherwise
-- stack the same hook and call MA:Tracker_OnTick() multiple times per tick.
-- verified against EllesmereUIMythicTimer.lua:755-764 (real, currently-
-- shipping code).
local tickHookInstalled = false
local function InstallTickHook()
  if tickHookInstalled then return end
  local block = (ScenarioObjectiveTracker and ScenarioObjectiveTracker.ChallengeModeBlock)
      or (ScenarioBlocksFrame and ScenarioBlocksFrame.ChallengeModeBlock)
  if block and block.UpdateTime then
    tickHookInstalled = true
    hooksecurefunc(block, "UpdateTime", function() MA:Tracker_OnTick() end)
  end
end

-- Our own event frame -- deliberately separate from CombatLogging.lua's,
-- per this addon's decoupled-via-MA-table style; the two files don't need
-- to know about each other, only about MA.
local trackerFrame = CreateFrame("Frame")

-- SCENARIO_CRITERIA_UPDATE and CHALLENGE_MODE_DEATH_COUNT_UPDATED are only
-- registered while a key is active. SCENARIO_CRITERIA_UPDATE in particular
-- fires constantly during non-M+ scenario content (pet battles, world
-- quests, etc.) if left always-registered.
-- verified against EllesmereUIMythicTimer.lua:2804-2815 (real, currently-
-- shipping code and comment): "SCENARIO_CRITERIA_UPDATE fires constantly in
-- any scenario (pet battles, world quest scenarios, garrisons, etc.)...
-- Registering them only during a key keeps idle CPU at zero."
local function RegisterRunEvents()
  trackerFrame:RegisterEvent("SCENARIO_CRITERIA_UPDATE")
  trackerFrame:RegisterEvent("CHALLENGE_MODE_DEATH_COUNT_UPDATED")
end

local function UnregisterRunEvents()
  trackerFrame:UnregisterEvent("SCENARIO_CRITERIA_UPDATE")
  trackerFrame:UnregisterEvent("CHALLENGE_MODE_DEATH_COUNT_UPDATED")
end

-- Shared start path for both a real CHALLENGE_MODE_START and the soft-start
-- recovery from PLAYER_ENTERING_WORLD (reload mid-key).
local function StartRun()
  InstallTickHook()
  MA.state = NewState()
  MA.state.active = true
  RegisterRunEvents()
  -- Populate immediately instead of waiting for the next 1s Blizzard tick,
  -- so the overlay has real numbers (not just zeros) the moment it shows.
  MA:Tracker_OnTick()
end

-- Shared end path for CHALLENGE_MODE_COMPLETED and CHALLENGE_MODE_RESET.
local function EndRun(event)
  if event == "CHALLENGE_MODE_COMPLETED" then
    -- GetWorldElapsedTime can return an unreliable ("secret") value right
    -- at completion; C_ChallengeMode.GetChallengeCompletionInfo() is the
    -- authoritative final time once available. Kept minimal here (just the
    -- .time field, in milliseconds) since that's the one field this WP
    -- actually verified the shape of.
    -- verified against EllesmereUIMythicTimer.lua:925-935 (real,
    -- currently-shipping code and comment): "Use
    -- C_ChallengeMode.GetChallengeCompletionInfo() as the authoritative
    -- completion time (milliseconds). GetWorldElapsedTime can return
    -- secret or stale values after depletion, producing '99:99' display."
    local completionInfo = C_ChallengeMode.GetChallengeCompletionInfo and C_ChallengeMode.GetChallengeCompletionInfo()
    if completionInfo and completionInfo.time and completionInfo.time > 0 then
      MA.state.elapsed = completionInfo.time / 1000
    end
    local deathCount, timeLostSeconds = C_ChallengeMode.GetDeathCount()
    MA.state.deaths = deathCount or 0
    MA.state.deathTimeLost = timeLostSeconds or 0
    UpdateForces()
  end

  MA.state.active = false
  UnregisterRunEvents()

  -- Keep the overlay up for RECAP_DURATION_S showing final numbers, instead
  -- of it vanishing the instant IsKeyActive() flips false (see
  -- Overlay.lua's show/hide logic, which checks this deadline). Nothing
  -- else drives a tick once the key ends (Tracker_OnTick() early-returns
  -- once state.active is false), so schedule one more refresh for exactly
  -- when the window closes -- otherwise the overlay would stay frozen on
  -- screen forever with nothing left to tell it to hide.
  -- GetTime()/C_Timer.After are standard, long-unchanged Blizzard APIs
  -- (already used elsewhere in this addon -- see RouteImport.lua's own
  -- GetTime() usage and PLAYER_ENTERING_WORLD's C_Timer.After retry above).
  MA.state.recapUntil = GetTime() + RECAP_DURATION_S
  if MA.Overlay_Refresh then MA.Overlay_Refresh(MA) end
  C_Timer.After(RECAP_DURATION_S, function()
    if MA.Overlay_Refresh then MA.Overlay_Refresh(MA) end
  end)
end

-- Soft-start recovery for a /reload mid-key: if we're actually in an active
-- key but MA.state doesn't reflect that yet, treat it like CHALLENGE_MODE_START.
-- Checked once immediately on PLAYER_ENTERING_WORLD and once more after a
-- short delay, since scenario/challenge-mode API data isn't always fully
-- populated at the moment PLAYER_ENTERING_WORLD fires (particularly right
-- after a /reload).
-- verified against EllesmereUIMythicTimer.lua:2819-2823 (real, currently-
-- shipping code and comment): "API data isn't fully populated at PEW;
-- retry once after 10s to catch a /reload mid-key."
local function CheckForActiveKeyAfterReload()
  if IsKeyActive() and not MA.state.active then
    StartRun()
  end
end

trackerFrame:RegisterEvent("PLAYER_ENTERING_WORLD")
trackerFrame:RegisterEvent("CHALLENGE_MODE_START")
trackerFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")
trackerFrame:RegisterEvent("CHALLENGE_MODE_RESET")

trackerFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    StartRun()
  elseif event == "CHALLENGE_MODE_COMPLETED" or event == "CHALLENGE_MODE_RESET" then
    EndRun(event)
  elseif event == "SCENARIO_CRITERIA_UPDATE" or event == "CHALLENGE_MODE_DEATH_COUNT_UPDATED" then
    MA:Tracker_OnTick()
  elseif event == "PLAYER_ENTERING_WORLD" then
    CheckForActiveKeyAfterReload()
    C_Timer.After(10, CheckForActiveKeyAfterReload)
  end
end)
