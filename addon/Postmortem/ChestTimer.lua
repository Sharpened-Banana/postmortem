-- ChestTimer.lua
-- Live +2/+3 chest countdowns and per-boss/objective split times with
-- personal-best comparison. Reads MA.state (kept up to date by Tracker.lua)
-- for elapsed time and writes its own MA.state.chestTimer/MA.state.splits
-- for Overlay.lua to render -- same "this file never calls the WoW
-- live-tracking APIs it doesn't own" split Overlay.lua/Tracker.lua already
-- use, except this file *does* own its own two APIs
-- (C_ChallengeMode.GetMapUIInfo/GetActiveKeystoneInfo and
-- C_ScenarioInfo.GetCriteriaInfo) since nothing else in the addon reads
-- them yet.
--
-- Math verified this session against two real, currently-shipping addons
-- that independently arrived at the identical formula:
-- EllesmereUIMythicTimer.lua:18-20,55-77 and
-- EnhanceQoLDungeonRaid/MythicPlusTimer.lua:30,623-635. +2 requires
-- finishing within 80% of the time limit, +3 within 60%. Challenger's
-- Peril (affix id 152) used to add 90s to the base timer; both of those
-- addons still special-case it defensively even though it's currently
-- folded into a different affix rather than live on its own -- same reason
-- kept here: detecting it costs nothing and costs nothing if it's absent.
--
-- Boss/objective splits: C_Scenario.GetStepInfo()'s 3rd return is the
-- criteria count; C_ScenarioInfo.GetCriteriaInfo(i) per index gives
-- .description/.completed/.isWeightedProgress. A split is recorded on the
-- rising edge only (completed flips false->true), exactly the pattern in
-- EllesmereUIMythicTimer.lua:611-663 and AngryKeystones/Splits.lua. The
-- isWeightedProgress criteria is enemy forces (already handled by
-- Tracker.lua's UpdateForces) and is skipped here -- this file only cares
-- about the boss/objective-kill criteria.

local ADDON_NAME, MA = ...

local CHALLENGERS_PERIL_AFFIX_ID = 152
local PLUS_TWO_RATIO = 0.8
local PLUS_THREE_RATIO = 0.6

-- Formats seconds as MM:SS, or -MM:SS once past a threshold (overtime).
-- FormatElapsed (Overlay.lua) floors negatives to 0 -- this file needs the
-- negative shown, so it gets its own formatter rather than reusing that one.
local function FormatSigned(seconds)
  seconds = seconds or 0
  local sign = seconds < 0 and "-" or ""
  local whole = math.floor(math.abs(seconds))
  local m = math.floor(whole / 60)
  local s = whole % 60
  return string.format("%s%02d:%02d", sign, m, s)
end

local function CalculateChestTimers(timeLimit, affixIDs)
  local t2 = (timeLimit or 0) * PLUS_TWO_RATIO
  local t3 = (timeLimit or 0) * PLUS_THREE_RATIO
  if not timeLimit or timeLimit <= 0 then return t2, t3 end

  for _, affixID in ipairs(affixIDs or {}) do
    if affixID == CHALLENGERS_PERIL_AFFIX_ID then
      local adjusted = timeLimit - 90
      if adjusted > 0 then
        t2 = adjusted * PLUS_TWO_RATIO + 90
        t3 = adjusted * PLUS_THREE_RATIO + 90
      end
      break
    end
  end
  return t2, t3
end

-- Fresh/empty chest-timer state. Set at file load and again on every
-- CHALLENGE_MODE_START (MA.state itself is replaced wholesale by
-- Tracker.lua's StartRun()).
local function ResetChestTimer()
  MA.state.chestTimer = { timeLimit = nil, t2 = nil, t3 = nil, mapID = nil, level = nil }
  MA.state.splits = {}
end
ResetChestTimer()

-- criteriaIndex -> true once its rising edge has been recorded this key, so
-- a later tick (or a /pm debug synthetic re-dispatch) never double-records
-- the same boss.
local recordedCriteria = {}

local function GetBestSplitsTable(mapID, level, createIfMissing)
  if not mapID or not level then return nil end
  local db = MA:GetDB()
  db.bestSplits = db.bestSplits or {}
  local byMap = db.bestSplits[mapID]
  if not byMap then
    if not createIfMissing then return nil end
    byMap = {}
    db.bestSplits[mapID] = byMap
  end
  local byLevel = byMap[level]
  if not byLevel then
    if not createIfMissing then return nil end
    byLevel = {}
    byMap[level] = byLevel
  end
  return byLevel
end

local function RecordSplit(criteriaIndex, description, elapsed)
  -- No chest timer means no key as far as this file is concerned, and
  -- there is nothing to file a split against. This is reachable for real:
  -- a /reload during a key sends Tracker.lua down its recovery path, which
  -- rebuilds MA.state from scratch, and this file's own state goes with it
  -- -- CHALLENGE_MODE_START has already fired and will not fire again. The
  -- key then FINISHED with chestTimer nil and this function threw
  -- "attempt to index a nil value (local 'ct')" (2026-09-11).
  local ct = MA.state.chestTimer
  if not ct or not MA.state.splits then return end

  local best = GetBestSplitsTable(ct.mapID, ct.level, false)
  local previousBest = best and best[criteriaIndex]
  local delta = previousBest and (elapsed - previousBest) or nil

  table.insert(MA.state.splits, {
    index = criteriaIndex,
    name = description or ("Objective " .. criteriaIndex),
    elapsed = elapsed,
    best = previousBest,
    delta = delta,
  })

  -- Personal best only ever moves down (faster) or gets set for the first
  -- time -- never overwritten with a slower split.
  if not previousBest or elapsed < previousBest then
    local writable = GetBestSplitsTable(ct.mapID, ct.level, true)
    if writable then writable[criteriaIndex] = elapsed end
  end

  MA:Debug("ChestTimer: split recorded -- %s at %s (best %s)",
    description or tostring(criteriaIndex), FormatSigned(elapsed),
    previousBest and FormatSigned(previousBest) or "none")
end

local function CheckSplits(elapsed)
  -- Same reason as RecordSplit's own guard: EndChestTimer() reaches here
  -- on CHALLENGE_MODE_COMPLETED whether or not this file ever saw the
  -- matching start, and after a mid-key /reload it did not.
  if not MA.state.chestTimer then return end

  local numCriteria = select(3, C_Scenario.GetStepInfo()) or 0
  for i = 1, numCriteria do
    local info = C_ScenarioInfo.GetCriteriaInfo(i)
    if info and not info.isWeightedProgress and info.completed and not recordedCriteria[i] then
      recordedCriteria[i] = true
      RecordSplit(i, info.description, elapsed)
    end
  end
end

-- Called from Tracker.lua's MA:Tracker_OnTick() (once per second while a
-- key is active).
function MA:ChestTimer_OnTick()
  local ct = MA.state.chestTimer
  if not ct or not ct.timeLimit then return end
  CheckSplits(MA.state.elapsed or 0)
end

local function StartChestTimer()
  ResetChestTimer()
  recordedCriteria = {}

  local mapID
  if C_ChallengeMode.GetActiveChallengeMapID then
    mapID = C_ChallengeMode.GetActiveChallengeMapID()
  end
  if not mapID then return end
  local _, _, timeLimit = C_ChallengeMode.GetMapUIInfo(mapID)

  -- Not "X and X()" here -- `and` only ever yields ONE value, so it would
  -- silently truncate this call's second return (affixIDs) to nil even
  -- when the function exists and returns both. Real bug caught by
  -- luacheck's "variable is never set" warning during this WP.
  local level, affixIDs
  if C_ChallengeMode.GetActiveKeystoneInfo then
    level, affixIDs = C_ChallengeMode.GetActiveKeystoneInfo()
  end
  if not timeLimit or timeLimit <= 0 then return end

  local t2, t3 = CalculateChestTimers(timeLimit, affixIDs)
  MA.state.chestTimer = { timeLimit = timeLimit, t2 = t2, t3 = t3, mapID = mapID, level = level }
  MA:Debug("ChestTimer: limit %s, +2 at %s, +3 at %s", FormatSigned(timeLimit), FormatSigned(t2), FormatSigned(t3))
end

local function EndChestTimer(event)
  if event ~= "CHALLENGE_MODE_COMPLETED" then return end
  -- Same lesson Tracker.lua's EndRun() already applies: GetWorldElapsedTime
  -- can return a secret/stale value right at completion, so use the
  -- authoritative completion time instead once it's available. MA.state.elapsed
  -- has usually already been corrected by Tracker.lua's own EndRun handler
  -- by the time our own CHALLENGE_MODE_COMPLETED handler runs (both frames
  -- register the same event; exact ordering isn't guaranteed either way),
  -- so re-derive it here too rather than assuming that ordering.
  local completionInfo = C_ChallengeMode.GetChallengeCompletionInfo and C_ChallengeMode.GetChallengeCompletionInfo()
  local finalElapsed = MA.state.elapsed
  if completionInfo and completionInfo.time and completionInfo.time > 0 then
    finalElapsed = completionInfo.time / 1000
  end
  CheckSplits(finalElapsed or 0)
end

local eventFrame = CreateFrame("Frame")
MA:RegisterKeyEventFrame(eventFrame)
eventFrame:RegisterEvent("CHALLENGE_MODE_START")
eventFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")
eventFrame:RegisterEvent("CHALLENGE_MODE_RESET")

eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    StartChestTimer()
  elseif event == "CHALLENGE_MODE_COMPLETED" or event == "CHALLENGE_MODE_RESET" then
    EndChestTimer(event)
  end
end)

-- Exposed for Overlay.lua/RunHistory.lua so they don't need to know the
-- MM:SS formatting rule (in particular, the negative-overtime case) lives
-- here.
MA.ChestTimer_FormatSigned = FormatSigned
