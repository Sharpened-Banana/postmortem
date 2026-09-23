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

-- True when the client handed us a secret value (patch 12.0's Secret Values):
-- one that throws the moment it is compared, used in arithmetic, concatenated
-- or used as a table key. Same guard as MeterUtil.lua's; nothing is ever
-- secret on a client that predates the system.
function MA:TankUtil_IsSecret(value)
  return issecretvalue ~= nil and issecretvalue(value) or false
end

-- Is the spell actually in the player's spellbook right now? This is the
-- question the combat log can never answer, and it is what keeps the live
-- report from telling a tank they sat on a button they never talented.
-- A nil/unknown answer is treated as "don't claim anything" by the caller.
--
-- C_SpellBook.IsSpellKnown is the documented call on the live client and
-- carries no secret annotation (verified against Blizzard's generated API
-- documentation, build 12.1.0.69933). It does not follow talent overrides,
-- which can only produce a false "not known" -- the direction that
-- suppresses a claim, never invents one. The globals are kept for clients
-- without the namespaced call.
function MA:TankUtil_IsKnown(spellID)
  local known
  if C_SpellBook and C_SpellBook.IsSpellKnown then
    known = C_SpellBook.IsSpellKnown(spellID)
  elseif IsSpellKnownOrOverridesKnown then
    known = IsSpellKnownOrOverridesKnown(spellID)
  elseif IsSpellKnown then
    known = IsSpellKnown(spellID)
  else
    return nil
  end
  if MA:TankUtil_IsSecret(known) then return nil end
  return known and true or false
end

-- Seconds remaining on spellID's cooldown: 0 when ready, nil when the
-- client will not say.
--
-- Why this asks a duration object instead of GetSpellCooldown
-- ----------------------------------------------------------
-- C_Spell.GetSpellCooldown and C_Spell.GetSpellCharges are both declared
-- SecretWhenCooldownsRestricted, and the restriction types include
-- ChallengeMode -- "an active and incomplete mythic keystone dungeon",
-- i.e. exactly where this addon runs. Their fields come back secret, and
-- comparing or subtracting them throws. The first version of this function
-- did both, so under restriction it could not answer at all.
--
-- C_Spell.GetSpellCooldownDuration carries no such annotation. It returns a
-- LuaDurationObject whose HasSecretValues() and IsZero() are unannotated
-- too -- the intended pattern is to ask "can I read this?" before "is it on
-- cooldown?". When the answer to the first is no, this returns nil and the
-- caller falls back to its own evidence (see TankUtil_Readiness).
--
-- ignoreGCD, because otherwise every spell looks "on cooldown" for a second
-- and a half after any cast.
function MA:TankUtil_CooldownRemaining(spellID)
  if C_Spell and C_Spell.GetSpellCooldownDuration then
    local duration = C_Spell.GetSpellCooldownDuration(spellID, true)
    if duration == nil or duration.HasSecretValues == nil then return nil end
    if duration:HasSecretValues() then return nil end
    if duration:IsZero() then return 0 end
    local remaining = duration:GetRemainingDuration()
    if type(remaining) ~= "number" or MA:TankUtil_IsSecret(remaining) then return nil end
    return remaining > 0 and remaining or 0
  end

  -- Clients without duration objects. Every field is checked for secrecy
  -- before it is touched, rather than trusting that this path only runs on
  -- clients where nothing is secret.
  if C_Spell and C_Spell.GetSpellCooldown then
    local info = C_Spell.GetSpellCooldown(spellID)
    if type(info) ~= "table" then return nil end
    local startTime, dur = info.startTime, info.duration
    if MA:TankUtil_IsSecret(startTime) or MA:TankUtil_IsSecret(dur) then return nil end
    if type(startTime) ~= "number" or type(dur) ~= "number" then return nil end
    if startTime == 0 or dur == 0 then return 0 end
    local remaining = (startTime + dur) - GetTime()
    return remaining > 0 and remaining or 0
  end
  return nil
end

-- Is `entry` (a TankDefensives row) ready right now? Returns two values:
--
--   "ready" | "cooling" | nil   -- nil means nobody can honestly say
--   "client" | "estimate"       -- where the answer came from
--
-- The client's own cooldown state is used whenever it is readable. When it
-- is not (see TankUtil_CooldownRemaining), the fallback is the analyzer's
-- method: the player's own last cast plus the table's baseline cooldown,
-- with a margin. That fallback only ever reasons from POSITIVE evidence --
-- a cast we actually saw. Never having seen a cast is not evidence of
-- anything here, because the cast event itself can be secret under
-- restriction (UNIT_SPELLCAST_SUCCEEDED is SecretWhenUnitSpellCastRestricted),
-- so "no cast recorded" may just mean "no cast readable". The analyzer, which
-- sees the whole log, is the one place that can reason from absence.
function MA:TankUtil_Readiness(entry, castAt, now, margin)
  local remaining = MA:TankUtil_CooldownRemaining(entry.id)
  if remaining ~= nil then
    return remaining <= 0 and "ready" or "cooling", "client"
  end
  if type(castAt) == "number" then
    if now - castAt >= entry.cooldown + (margin or 0) then
      return "ready", "estimate"
    end
    return "cooling", "estimate"
  end
  return nil, nil
end
