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
  if reassertTicker then reassertTicker:Cancel() end
  reassertTicker = C_Timer.NewTicker(REASSERT_INTERVAL_S, function()
    EnsureAdvancedLogging()
    MA:CombatLogging_SetState(true, false)
  end)
end

function MA:CombatLogging_OnChallengeModeEnd()
  -- Stop re-asserting before anything else -- otherwise a ticker tick
  -- landing between here and the LoggingCombat(false) call below could
  -- turn logging straight back on behind our own back.
  if reassertTicker then
    reassertTicker:Cancel()
    reassertTicker = nil
  end

  -- Record the REAL logging state before touching anything -- this is what
  -- Overlay.lua's post-key recap panel reports as "log saved" or not.
  -- Captured unconditionally (even if combatLoggingEnabled is off), since
  -- the user may have started logging manually via /combatlog, or another
  -- addon may control it; either way this is the true answer to "was this
  -- key actually being recorded", not just "did *we* turn it on".
  MA.state.combatLogWasOn = self:CombatLogging_GetCurrentState()

  if not self:GetDB().combatLoggingEnabled then return end
  self:CombatLogging_SetState(false, false)
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
