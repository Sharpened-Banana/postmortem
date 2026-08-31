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

-- LoggingCombat() with no args returns the current combat-logging state.
-- verified against MythicDungeonTools/Core/CombatLogging.lua
function MA:CombatLogging_GetCurrentState()
  return LoggingCombat() and true or false
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
  SetCVar("advancedCombatLogging", "1")
  self:CombatLogging_SetState(true, not hasSyncedState)
  hasSyncedState = true
end

function MA:CombatLogging_OnChallengeModeEnd()
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
