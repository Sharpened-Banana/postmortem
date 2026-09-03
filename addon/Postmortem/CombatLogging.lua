-- CombatLogging.lua
-- Recording helper: turns combat logging on for the duration of a Mythic+
-- key and off again once it ends, so the user never has to remember to run
-- /combatlog manually before a push.
--
-- Modeled directly on MythicDungeonTools/Core/CombatLogging.lua (real,
-- currently-shipping addon source), which does the same thing on
-- zone/difficulty change; here it's retriggered on key start/end instead.

local ADDON_NAME, MA = ...

-- On the very first evaluation after load we can't trust our own guess
-- about whether logging is currently on (another addon, or the user's own
-- /combatlog, may have already set it), so the first call forces an
-- unconditional write; every call after that only writes on a real change.
local hasSyncedState = false

-- How often, in seconds, we re-assert our desired logging state for the
-- whole duration of an active key (see reassertTicker below) -- not just
-- at CHALLENGE_MODE_START. Real, confirmed cause: BigWigs_Plugins/Pull.lua
-- (installed on this machine) also toggles LoggingCombat independently,
-- and its own BigWigs_OnBossWin handler is careful to skip stopping during
-- an active key (checks C_ChallengeMode.IsChallengeModeActive()) -- but
-- that API can flip false in the brief window between the final boss's HP
-- hitting zero and CHALLENGE_MODE_COMPLETED actually firing a few seconds
-- later, so BigWigs schedules LoggingCombat(false) 2s after that boss
-- win, cutting logging off before the completion event (or anything
-- after it, including CHALLENGE_MODE_COMPLETED itself) ever reaches the
-- log file. 2s keeps the worst-case gap this could reintroduce short
-- rather than losing the rest of the run, at negligible cost: the actual
-- LoggingCombat()/SetCVar() write only fires when state has genuinely
-- drifted (see the "only write on change" pattern below and in
-- CombatLogging_SetState).
local REASSERT_INTERVAL_S = 2

-- Handle for the mid-key re-assert ticker below; nil whenever no key is
-- active (started in CombatLogging_OnChallengeModeStart, cancelled in
-- CombatLogging_OnChallengeModeEnd -- its lifecycle IS "while a key is
-- active", so nothing here needs its own separate is-the-key-active
-- check).
local reassertTicker = nil

-- How long, in seconds, logging is kept ON after CHALLENGE_MODE_COMPLETED
-- / _RESET before we finally switch it off.
--
-- Confirmed real (2026-09-03, two keys in one session, both timed): the
-- Lua CHALLENGE_MODE_COMPLETED event fires BEFORE the client has written
-- the CHALLENGE_MODE_END line to the combat log file. In a log where the
-- end was captured, CHALLENGE_MODE_END landed 66ms after the final boss's
-- ENCOUNTER_END; in the two broken keys the log simply stopped ~110ms
-- after ENCOUNTER_END with no CHALLENGE_MODE_END at all, and nothing else
-- was written until the next dungeon's zone-in turned logging back on.
-- Calling LoggingCombat(false) synchronously inside the COMPLETED handler
-- (what this file used to do) is exactly what cut it off: whether the END
-- line survives is a frame-timing race we lost twice in a row. Without
-- that line the Python side can never tell the key finished -- the run
-- shows up as abandoned/incomplete in both the analyzer and Watch Live.
--
-- BigWigs_Plugins/Pull.lua (same machine) does precisely this too --
-- scheduleLogStop(5) on CHALLENGE_MODE_COMPLETED, "Delay to prevent any
-- events after the final blow being cut out of the log" -- so 5s is a
-- known-good figure. The re-assert ticker keeps running through the grace
-- period so a third addon stopping logging early doesn't win either.
local STOP_GRACE_S = 5

-- Pending "actually stop logging now" timer scheduled by
-- CombatLogging_OnChallengeModeEnd; nil when nothing is pending. A new
-- CHALLENGE_MODE_START inside the grace window cancels it -- otherwise a
-- stop scheduled for the previous key would fire a few seconds into the
-- next one and silently kill that key's log.
local pendingStop = nil

local function cancelPendingStop()
  if pendingStop then
    pendingStop:Cancel()
    pendingStop = nil
  end
end

local function cancelReassertTicker()
  if reassertTicker then
    reassertTicker:Cancel()
    reassertTicker = nil
  end
end

-- LoggingCombat() with no args returns the current combat-logging state.
-- verified against MythicDungeonTools/Core/CombatLogging.lua
function MA:CombatLogging_GetCurrentState()
  return LoggingCombat() and true or false
end

-- Sets advancedCombatLogging only when it isn't already on -- avoids an
-- unconditional SetCVar write every time this gets called (including on
-- every reassertTicker tick during a key).
-- verified against EllesmereUIQoL_AutoLogging.lua's own EnsureAdvancedLogging()
-- (real, currently-shipping addon source, same idiom).
local function EnsureAdvancedLogging()
  if GetCVar and GetCVar("advancedCombatLogging") ~= "1" then
    SetCVar("advancedCombatLogging", "1")
  end
end

-- Sets combat logging to `shouldLog`, but only issues the LoggingCombat()
-- call when the state actually needs to change (or `forceSync` is set).
-- This "only write on change" behavior matters because the user may be
-- running other addons that also toggle combat logging -- writing
-- unconditionally on every event would fight them.
-- verified against MythicDungeonTools/Core/CombatLogging.lua
function MA:CombatLogging_SetState(shouldLog, forceSync)
  local wasLogging = self:CombatLogging_GetCurrentState()
  if forceSync or wasLogging ~= shouldLog then
    LoggingCombat(shouldLog)
  end
end

function MA:CombatLogging_OnChallengeModeStart()
  -- A stop still pending from the previous key must never land on this
  -- one. Done before the enabled check on purpose: a pending stop can only
  -- exist if we scheduled it, and it must die regardless.
  cancelPendingStop()
  if not self:GetDB().combatLoggingEnabled then return end
  -- Advanced combat logging is required for the parses this project's
  -- Python side analyzes (spell IDs, absorbs, etc.), so force it on
  -- alongside plain combat logging.
  EnsureAdvancedLogging()
  self:CombatLogging_SetState(true, not hasSyncedState)
  hasSyncedState = true

  -- Re-assert our desired state every REASSERT_INTERVAL_S for the rest of
  -- this key -- see that constant's own comment for why (another addon,
  -- confirmed BigWigs on this machine, can race the M+ completion event
  -- and silently turn logging back off mid-key). Cancel any existing
  -- ticker first: defensive, in case this somehow fires twice without an
  -- intervening end event.
  cancelReassertTicker()
  reassertTicker = C_Timer.NewTicker(REASSERT_INTERVAL_S, function()
    EnsureAdvancedLogging()
    MA:CombatLogging_SetState(true, false)
  end)
end

function MA:CombatLogging_OnChallengeModeEnd()
  -- Record the REAL logging state before touching anything -- this is what
  -- Overlay.lua's post-key recap panel reports as "log saved" or not.
  -- Captured unconditionally (even if combatLoggingEnabled is off), since
  -- the user may have started logging manually via /combatlog, or another
  -- addon may control it; either way this is the true answer to "was this
  -- key actually being recorded", not just "did *we* turn it on".
  MA.state.combatLogWasOn = self:CombatLogging_GetCurrentState()

  if not self:GetDB().combatLoggingEnabled then
    cancelReassertTicker()
    return
  end

  -- Do NOT stop logging here. The CHALLENGE_MODE_END combat-log line is
  -- written *after* this Lua event fires (see STOP_GRACE_S), so stopping
  -- synchronously drops it. Keep the re-assert ticker alive through the
  -- grace window -- it's the guard against another addon cutting logging
  -- in that same window -- and stop both together once it's over.
  cancelPendingStop()
  pendingStop = C_Timer.NewTimer(STOP_GRACE_S, function()
    pendingStop = nil
    -- Ticker first: a tick landing between here and LoggingCombat(false)
    -- would turn logging straight back on behind our own back.
    cancelReassertTicker()
    MA:CombatLogging_SetState(false, false)
  end)
end

-- CHALLENGE_MODE_START fires when a Mythic+ key is started (the door
-- closes). CHALLENGE_MODE_COMPLETED fires on a timed/untimed finish, and
-- CHALLENGE_MODE_RESET fires when the key is abandoned/reset -- both should
-- stop logging, so they share the same branch below.
-- verified against MythicDungeonTools/Core/CombatLogging.lua
local eventFrame = CreateFrame("Frame")
eventFrame:RegisterEvent("CHALLENGE_MODE_START")
eventFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")
eventFrame:RegisterEvent("CHALLENGE_MODE_RESET")
eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    MA:CombatLogging_OnChallengeModeStart()
  else -- CHALLENGE_MODE_COMPLETED or CHALLENGE_MODE_RESET
    MA:CombatLogging_OnChallengeModeEnd()
  end
end)
