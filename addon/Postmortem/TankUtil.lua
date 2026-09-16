-- TankUtil.lua
-- The three client reads the tank modules share: what spec am I, do I know
-- this spell, and is it off cooldown.
--
-- Factored out for the same reason MeterUtil.lua was -- two consumers
-- (TankDeath.lua's post-death capture and Incoming.lua's live panel)
-- needed the identical tolerant wrappers, and a second hand-maintained
-- copy is how the two quietly stop agreeing about what "ready" means.
--
-- None of this reads a combat value. Spec, spellbook and your own cooldowns
-- are explicitly outside patch 12.0's Secret Values -- they are yours, not
-- the encounter's. Every call is still wrapped and tolerant of a nil return,
-- per this addon's standing rule that a read may simply fail.

local ADDON_NAME, MA = ...

-- Resolves the player's current specialization id (65, 250, 581, ...),
-- tolerating both the modern C_SpecializationInfo path and the legacy
-- globals -- same defensive shape as DeathTagging.lua's SpellName().
function MA:TankUtil_PlayerSpecID()
  local index
  if C_SpecializationInfo and C_SpecializationInfo.GetSpecialization then
    index = C_SpecializationInfo.GetSpecialization()
  elseif GetSpecialization then
    index = GetSpecialization()
  end
  if not index then return nil end

  if C_SpecializationInfo and C_SpecializationInfo.GetSpecializationInfo then
    local id = C_SpecializationInfo.GetSpecializationInfo(index)
    if type(id) == "number" then return id end
  elseif GetSpecializationInfo then
    local id = GetSpecializationInfo(index)
    if type(id) == "number" then return id end
  end
  return nil
end

-- Is the spell actually in the player's spellbook right now? This is the
-- question the combat log can never answer, and it is what keeps the live
-- report from telling a tank they sat on a button they never talented.
-- A nil/unknown answer is treated as "don't claim anything" by the caller.
function MA:TankUtil_IsKnown(spellID)
  if C_SpellBook and C_SpellBook.IsSpellKnownOrOverridesKnown then
    return C_SpellBook.IsSpellKnownOrOverridesKnown(spellID) and true or false
  end
  if C_SpellBook and C_SpellBook.IsSpellKnown then
    return C_SpellBook.IsSpellKnown(spellID) and true or false
  end
  if IsSpellKnownOrOverridesKnown then
    return IsSpellKnownOrOverridesKnown(spellID) and true or false
  end
  if IsSpellKnown then
    return IsSpellKnown(spellID) and true or false
  end
  return nil
end

-- Seconds remaining on spellID's cooldown: 0 when ready, nil when the
-- client wouldn't say. Handles the modern table-returning
-- C_Spell.GetSpellCooldown and the legacy multiple-return GetSpellCooldown,
-- and treats a charge-based spell with a charge banked as ready regardless
-- of the recharge timer (that is what having a charge means).
function MA:TankUtil_CooldownRemaining(spellID)
  if C_Spell and C_Spell.GetSpellCharges then
    local charges = C_Spell.GetSpellCharges(spellID)
    if type(charges) == "table" and type(charges.currentCharges) == "number" then
      if charges.currentCharges > 0 then return 0 end
    end
  end

  local startTime, duration
  if C_Spell and C_Spell.GetSpellCooldown then
    local info = C_Spell.GetSpellCooldown(spellID)
    if type(info) == "table" then
      startTime, duration = info.startTime, info.duration
    end
  elseif GetSpellCooldown then
    startTime, duration = GetSpellCooldown(spellID)
  end

  if type(startTime) ~= "number" or type(duration) ~= "number" then
    return nil
  end
  if startTime == 0 or duration == 0 then
    return 0  -- not on cooldown
  end
  local remaining = (startTime + duration) - GetTime()
  return remaining > 0 and remaining or 0
end
