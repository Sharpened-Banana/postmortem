-- RunHistory.lua
-- Bounded, account-scoped log of past keys in PostmortemDB.global.runHistory
-- -- not a new SavedVariables table, since this is the same storage tier as
-- settings (account-scoped, bounded), not a growing shared database like
-- PostmortemSpellDB/PostmortemAvoidableDB. Every field recorded here already
-- exists on MA.state by the time CHALLENGE_MODE_COMPLETED/RESET fires,
-- thanks to Tracker.lua (forces/deaths), ChestTimer.lua (splits), and
-- Interrupts.lua (kick count) -- this file only reads and archives, it
-- never computes anything itself.
--
-- Deliberately no dedicated browser window: Details_MythicPlus already owns
-- that space richly (run selector, scoreboard, timeline, export). A chat
-- dump via /pm history is proportionate for this addon's scope.

local ADDON_NAME, MA = ...

local function RecordRun(event)
  local db = MA:GetDB()
  if not db.saveRunHistory then return end

  local state = MA.state or {}
  local ct = state.chestTimer or {}

  local zone = nil
  if C_ChallengeMode.GetMapUIInfo and ct.mapID then
    zone = C_ChallengeMode.GetMapUIInfo(ct.mapID)
  end

  -- Not "a and b or nil" here -- if state.elapsed > ct.timeLimit, that
  -- comparison legitimately evaluates to false, and "false or nil" collapses
  -- to nil, silently turning a real "not timed" result into "unknown".
  local timed = nil
  if ct.timeLimit and state.elapsed then
    timed = state.elapsed <= ct.timeLimit
  end

  local entry = {
    timestamp = time(),
    zone = zone,
    level = ct.level,
    mapID = ct.mapID,
    completed = (event == "CHALLENGE_MODE_COMPLETED"),
    timed = timed,
    duration_ms = state.elapsed and math.floor(state.elapsed * 1000) or nil,
    deaths = state.deaths or 0,
    deathTimeLost = state.deathTimeLost or 0,
    forcesPct = state.forces and state.forces.percent or nil,
    kicks = state.interrupts and state.interrupts.total or 0,
    splits = state.splits,
  }

  local history = db.runHistory
  table.insert(history, entry)

  local limit = db.runHistoryLimit or 50
  while #history > limit do
    table.remove(history, 1)
  end

  MA:Debug("RunHistory: recorded run #%d (%s +%s, %s)", #history,
    tostring(entry.zone), tostring(entry.level), entry.completed and "completed" or "abandoned")
end

-- Prints the last `count` runs (most recent last) to chat, /pm history's
-- backing function.
function MA:RunHistory_Print(count)
  local db = MA:GetDB()
  local history = db.runHistory or {}
  if #history == 0 then
    print("|cffd7a94cPostmortem|r: no run history saved yet.")
    return
  end

  count = math.min(count or 5, #history)
  print(string.format("|cffd7a94cPostmortem|r: last %d run(s):", count))
  for i = #history - count + 1, #history do
    local r = history[i]
    local verdict
    if not r.completed then
      verdict = "|cffe0a020abandoned|r"
    elseif r.timed then
      verdict = "|cff58c47ctimed|r"
    else
      verdict = "|cffe06060depleted|r"
    end
    print(string.format(
      "  %s +%s -- %s, %d death(s), %d kick(s)",
      tostring(r.zone or "?"), tostring(r.level or "?"), verdict, r.deaths or 0, r.kicks or 0
    ))
  end
end

local eventFrame = CreateFrame("Frame")
MA:RegisterKeyEventFrame(eventFrame)
eventFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")
eventFrame:RegisterEvent("CHALLENGE_MODE_RESET")

eventFrame:SetScript("OnEvent", function(self, event, ...)
  RecordRun(event)
end)
