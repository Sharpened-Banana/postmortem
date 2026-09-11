-- MeterUtil.lua
-- Shared, secret-value-safe helpers for reading Blizzard's built-in
-- C_DamageMeter API, factored out of AvoidableDatabase.lua's own (still
-- separately maintained, untouched) copy of this same pattern so the newer
-- meter-based consumers added in patch 12.0's wake (Interrupts.lua,
-- RouteImport.lua, DeathTagging.lua -- none of which can use
-- COMBAT_LOG_EVENT_UNFILTERED any more, since addons can no longer register
-- it at all: "Fires ADDON_ACTION_FORBIDDEN when attempting to register this
-- event", confirmed on the API wiki for Patch 12.0.0) don't each reimplement
-- the same secret-polling walk.
--
-- AvoidableDatabase.lua is deliberately left with its own copy rather than
-- refactored onto this file -- it already works, and this build touches
-- enough of the addon without also risking regressing verified-working code.
--
-- Every function here tolerates: the API not existing on this client
-- (pre-12.0), a session read returning nil, and any field coming back as a
-- Secret Value while the server still considers the client "in combat" (see
-- issecretvalue()). Callers are expected to treat a `false`/`nil` return as
-- "try again next tick", never as an error.

local ADDON_NAME, MA = ...

-- issecretvalue only exists on clients with the Secret Values system; on
-- anything older nothing is ever secret.
function MA:MeterUtil_IsSecret(value)
  return issecretvalue ~= nil and issecretvalue(value) or false
end

function MA:MeterUtil_ApiAvailable()
  return C_DamageMeter ~= nil
    and type(C_DamageMeter.GetCombatSessionFromType) == "function"
    and type(C_DamageMeter.GetCombatSessionSourceFromType) == "function"
end

-- Returns (session, stillSecret). A nil session with stillSecret == true
-- means "keep polling"; nil with false means "nothing to read" (no API, no
-- session, or meterType not supported on this client).
-- verified against AvoidableDatabase.lua's own GetAvoidableSession(), which
-- this generalizes.
function MA:MeterUtil_GetSession(sessionType, meterType)
  if not meterType then return nil, false end
  if not self:MeterUtil_ApiAvailable() then return nil, false end
  local session = C_DamageMeter.GetCombatSessionFromType(sessionType, meterType)
  if not session then return nil, false end
  if self:MeterUtil_IsSecret(session.totalAmount) then return nil, true end
  local sources = session.combatSources
  if sources and sources[1] and self:MeterUtil_IsSecret(sources[1].totalAmount) then
    return nil, true
  end
  return session, false
end

-- Walks session.combatSources, calling fn(source) per entry. Returns
-- completed:boolean -- false the moment a secret value is hit (callers
-- should discard this pass's partial results and retry on the next tick,
-- exactly as AvoidableDatabase.lua's ForEachAvoidableSpell already does).
function MA:MeterUtil_ForEachSource(session, fn)
  local sources = (session and session.combatSources) or {}
  for i = 1, #sources do
    local source = sources[i]
    if self:MeterUtil_IsSecret(source.sourceGUID) or self:MeterUtil_IsSecret(source.totalAmount) then
      return false
    end
    fn(source)
  end
  return true
end

-- Wraps GetCombatSessionSourceFromType for one source GUID, calling
-- fn(spellEntry) per combatSpells entry. Returns completed:boolean.
function MA:MeterUtil_ForEachSourceSpell(sessionType, meterType, sourceGUID, fn)
  if not meterType then return true end
  if not self:MeterUtil_ApiAvailable() then return true end
  if self:MeterUtil_IsSecret(sourceGUID) then return false end
  if not sourceGUID then return true end
  local container = C_DamageMeter.GetCombatSessionSourceFromType(sessionType, meterType, sourceGUID)
  local spells = (container and container.combatSpells) or {}
  for i = 1, #spells do
    local spell = spells[i]
    if self:MeterUtil_IsSecret(spell.spellID) then return false end
    fn(spell)
  end
  return true
end
