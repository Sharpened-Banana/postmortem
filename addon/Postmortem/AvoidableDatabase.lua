-- AvoidableDatabase.lua
--
-- Self-builds an "avoidable damage" spell database from Blizzard's own
-- classification, one key at a time, into the PostmortemAvoidableDB
-- SavedVariables table. The Python side (`postmortem extract-avoidable`)
-- turns that into the avoidable_spells.json the analyzer tags damage
-- taken with -- the same addon-captures / Python-extracts split as the
-- interrupt database used to be (InterruptDatabase.lua) before Secret
-- Values killed that one.
--
-- Where the classification comes from: Patch 12.0.0's built-in damage
-- meter exposes an "Avoidable Damage Taken" meter type
-- (Enum.DamageMeterType.AvoidableDamageTaken) through the C_DamageMeter
-- API, and the spells listed under it are exactly the ones Blizzard
-- flags as avoidable. That flag is NOT in the combat log file, which is
-- why the analyzer can't derive it from WoWCombatLog.txt and needs this.
-- Every call below is grounded in real, currently-installed addon source:
--   * C_DamageMeter.GetCombatSessionFromType(sessionType, meterType)
--     -> { combatSources = { {name, sourceGUID, totalAmount, ...}, ... },
--          totalAmount, durationSeconds, maxAmount }
--     (Details/boot.lua GetSegment/GetSegmentInfo, Details_MythicPlus/
--     segments.lua GetAllCombatTypes; sessionType 0 = Overall, 1 =
--     Current per Details' DamageMeterSessionType table).
--   * C_DamageMeter.GetCombatSessionSourceFromType(sessionType, meterType,
--     sourceGUID) -> { combatSpells = { {spellID, totalAmount, ...}, ... } }
--     (Details_MythicPlus/segments.lua GetActorSpells; Details/core/
--     parser_nocleu1.lua's "9 avoidable damage taken" block reads
--     spellID/totalAmount off each entry exactly like this).
--
-- Secret Values: while the server considers you in combat, the meter's
-- numbers/names come back as "secret" values that error the moment
-- ordinary Lua touches them. Details_MythicPlus handles this at key end
-- by waiting 2s and then polling `issecretvalue()` on the session until
-- it clears (events.lua OnMythicDungeonEnd -> segments.lua
-- WaitServerDropCombat); this file does the same. The whole harvest also
-- runs inside pcall so a secret that slips through can never taint
-- anything outside this file -- the InterruptDatabase lesson (a secret
-- error propagating into UnregisterEvent and sticking events registered).
-- Nothing here ever unregisters an event, by design.
--
-- Scope: the Overall session is "since the meter was last reset", which
-- can include earlier keys (Details owns the reset; we never reset it).
-- For the spell list itself that doesn't matter -- an avoidable spell is
-- avoidable wherever it was seen -- but the per-spell dungeon tag would
-- be wrong for a spell carried over from a previous dungeon, so spells
-- already present when the key STARTS are remembered and only tagged
-- with this key's dungeon if they weren't.

local ADDON_NAME, MA = ...

-- Session type 0 = Overall (Details' DamageMeterSessionType.Overall).
local SESSION_OVERALL = 0

-- How long to keep polling for the meter to drop out of secret lockdown
-- after a key ends before giving up on this key (the next key will pick
-- the same spells up again -- the Overall session persists).
local INITIAL_DELAY_S = 2
local POLL_INTERVAL_S = 0.5
local POLL_MAX_TRIES = 120 -- 60s

local function AvoidableMeterType()
  return Enum and Enum.DamageMeterType and Enum.DamageMeterType.AvoidableDamageTaken
end

local function ApiAvailable()
  return C_DamageMeter ~= nil
    and type(C_DamageMeter.GetCombatSessionFromType) == "function"
    and type(C_DamageMeter.GetCombatSessionSourceFromType) == "function"
    and AvoidableMeterType() ~= nil
end

-- issecretvalue only exists on clients with the Secret Values system; on
-- anything older nothing is ever secret.
local function IsSecret(value)
  return issecretvalue ~= nil and issecretvalue(value) or false
end

-- Resolve a display name for a spell id, tolerating both the modern
-- C_Spell.GetSpellInfo (returns a table; what Details uses) and the
-- legacy global GetSpellInfo (returns a name first).
local function SpellName(spellID)
  if C_Spell and C_Spell.GetSpellInfo then
    local info = C_Spell.GetSpellInfo(spellID)
    if type(info) == "table" and type(info.name) == "string" then
      return info.name
    end
  elseif GetSpellInfo then
    local name = GetSpellInfo(spellID)
    if type(name) == "string" then return name end
  end
  return nil
end

-- The avoidable-damage Overall session, or nil if the API isn't there /
-- the session is still secret. Returns a second value, true when the
-- reason was "still secret" (so callers know to keep polling).
local function GetAvoidableSession()
  if not ApiAvailable() then return nil, false end
  local session = C_DamageMeter.GetCombatSessionFromType(SESSION_OVERALL, AvoidableMeterType())
  if not session then return nil, false end
  if IsSecret(session.totalAmount) then return nil, true end
  local sources = session.combatSources
  if sources and sources[1] and IsSecret(sources[1].totalAmount) then
    return nil, true
  end
  return session, false
end

-- Walks every player source in the session and calls fn(spellID, amount)
-- for each avoidable spell that hit them. Returns false if a secret value
-- was hit part-way (caller retries).
local function ForEachAvoidableSpell(session, fn)
  local meterType = AvoidableMeterType()
  local sources = session.combatSources or {}
  for i = 1, #sources do
    local source = sources[i]
    local guid = source.sourceGUID
    if IsSecret(guid) then return false end
    if guid then
      local container = C_DamageMeter.GetCombatSessionSourceFromType(SESSION_OVERALL, meterType, guid)
      local spells = container and container.combatSpells or {}
      for j = 1, #spells do
        local spell = spells[j]
        local spellID = spell.spellID
        if IsSecret(spellID) then return false end
        if type(spellID) == "number" and spellID > 0 then
          local amount = spell.totalAmount
          if IsSecret(amount) or type(amount) ~= "number" then amount = 0 end
          fn(spellID, amount)
        end
      end
    end
  end
  return true
end

-- Spell ids already in the Overall session when this key started (see the
-- header's note on scope). nil = couldn't read at start; tag nothing.
local seenBeforeKey = nil
local keyMapID = nil
local pollTicker = nil

local function CancelPoll()
  if pollTicker then
    pollTicker:Cancel()
    pollTicker = nil
  end
end

-- Records one spell into PostmortemAvoidableDB. Only the first sighting
-- writes the name; the dungeon set and last-seen stamp update every key
-- ("only write on change" isn't worth it here: this runs once per key,
-- not per cast).
local function RecordSpell(db, spellID, amount, mapID)
  local entry = db[spellID]
  if not entry then
    entry = { name = SpellName(spellID) or ("spell:" .. spellID), maps = {}, keys = 0, total = 0 }
    db[spellID] = entry
  elseif entry.name == nil or entry.name == ("spell:" .. spellID) then
    entry.name = SpellName(spellID) or entry.name
  end
  entry.maps = entry.maps or {}
  if mapID then entry.maps[mapID] = true end
  entry.keys = (entry.keys or 0) + 1
  entry.total = (entry.total or 0) + (amount or 0)
  entry.lastSeenTs = time()
end

-- The actual harvest. Returns true when done, false to keep polling.
local function Harvest()
  local db = MA:GetAvoidableDB()
  if not db then return true end

  local session, stillSecret = GetAvoidableSession()
  if not session then
    return not stillSecret -- no API / no session = nothing to do; secret = retry
  end

  local found = {}
  local complete = ForEachAvoidableSpell(session, function(spellID, amount)
    found[spellID] = (found[spellID] or 0) + amount
  end)
  if not complete then return false end

  local newSpells, knownSpells = 0, 0
  for spellID, amount in pairs(found) do
    local carriedOver = seenBeforeKey ~= nil and seenBeforeKey[spellID]
    -- Tag with this key's dungeon only when the spell can be attributed
    -- to it: either it wasn't in the meter when the key started, or we
    -- couldn't read the meter at start (seenBeforeKey == nil) and the
    -- session is assumed to be this key's.
    local mapID = (not carriedOver) and keyMapID or nil
    if db[spellID] then knownSpells = knownSpells + 1 else newSpells = newSpells + 1 end
    RecordSpell(db, spellID, amount, mapID)
  end
  MA:Debug("Avoidable DB: harvested %d spells from the meter (%d new, %d already known)",
    newSpells + knownSpells, newSpells, knownSpells)
  return true
end

local function StartHarvestPolling()
  CancelPoll()
  local tries = 0
  C_Timer.After(INITIAL_DELAY_S, function()
    pollTicker = C_Timer.NewTicker(POLL_INTERVAL_S, function()
      tries = tries + 1
      local ok, done = pcall(Harvest)
      if not ok then
        MA:Debug("Avoidable DB: harvest errored (%s) -- giving up on this key", tostring(done))
        CancelPoll()
        return
      end
      if done then
        CancelPoll()
      elseif tries >= POLL_MAX_TRIES then
        MA:Debug("Avoidable DB: meter still secret after %ds -- giving up on this key", POLL_MAX_TRIES * POLL_INTERVAL_S)
        CancelPoll()
      end
    end)
  end)
end

-- Snapshot what the Overall session already holds when the key starts,
-- so Harvest() can tell this key's spells from carried-over ones. Best
-- effort: if the meter is secret right now, seenBeforeKey stays nil.
local function SnapshotAtKeyStart()
  seenBeforeKey = nil
  keyMapID = C_ChallengeMode and C_ChallengeMode.GetActiveChallengeMapID and C_ChallengeMode.GetActiveChallengeMapID() or nil
  local ok, session = pcall(GetAvoidableSession)
  if not ok or not session then return end
  local snapshot = {}
  local walked, complete = pcall(ForEachAvoidableSpell, session, function(spellID)
    snapshot[spellID] = true
  end)
  if walked and complete then seenBeforeKey = snapshot end
end

function MA:AvoidableDB_OnChallengeModeStart()
  CancelPoll()
  if not ApiAvailable() then
    MA:Debug("Avoidable DB: C_DamageMeter avoidable-damage meter not available on this client")
    return
  end
  SnapshotAtKeyStart()
  local n = 0
  for _ in pairs(MA:GetAvoidableDB() or {}) do n = n + 1 end
  MA:Debug("Avoidable DB: key started (map %s), %d spells known so far", tostring(keyMapID), n)
end

function MA:AvoidableDB_OnChallengeModeEnd()
  if not ApiAvailable() then return end
  StartHarvestPolling()
end

-- Separate frame for CHALLENGE_MODE_START/COMPLETED/RESET, mirroring the
-- other key-scoped modules' gating structure exactly. RESET (a depleted
-- or abandoned key) harvests too: the damage that was taken is still
-- real data about which spells are avoidable.
local challengeModeFrame = CreateFrame("Frame")
MA:RegisterKeyEventFrame(challengeModeFrame)
challengeModeFrame:RegisterEvent("CHALLENGE_MODE_START")
challengeModeFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")
challengeModeFrame:RegisterEvent("CHALLENGE_MODE_RESET")

challengeModeFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    MA:AvoidableDB_OnChallengeModeStart()
  elseif event == "CHALLENGE_MODE_COMPLETED" or event == "CHALLENGE_MODE_RESET" then
    MA:AvoidableDB_OnChallengeModeEnd()
  end
end)
