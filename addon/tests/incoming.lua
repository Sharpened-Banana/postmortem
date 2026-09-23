-- Incoming.lua: the live "what's coming / what's up" panel.
--
-- Two things are worth pinning here. The first is degradation: the
-- encounter timeline does not exist on every client, does not exist
-- outside an encounter, and (per its own API declarations) can hand back
-- secret or missing fields at any moment -- and every one of those has to
-- produce no panel rather than a broken one or an error.
--
-- The second is the line the module refuses to cross. It shows majors
-- only, never resource-gated buttons, because the cooldown API reports
-- those "ready" whether or not the player can afford to press them -- a
-- panel that lists Shield Block during a rage drought is lying at exactly
-- the moment it is being trusted.
--
-- Run from the repo root:
--
--     lua addon/tests/incoming.lua
--
-- See addon/tests/README.md for how these harnesses work.

local frames = {}
local function NewFrame()
  local f = { events = {} }
  function f:RegisterEvent(e) self.events[e] = true end
  function f:UnregisterEvent(e) self.events[e] = nil end
  function f:RegisterUnitEvent(e) self.events[e] = true end
  function f:SetScript(_, fn) self.handler = fn end
  table.insert(frames, f)
  return f
end
function CreateFrame() return NewFrame() end

local NOW = 1000.0
function GetTime() return NOW end

local PROT_PALADIN = 66
local ARDENT_DEFENDER = 31850  -- major, 120s
local GOAK = 86659             -- major, 300s
local SOTR = 53600             -- resource-gated: cooldown 0 in the table

local knownSpells, cooldowns, specID, timeline

-- Secret Values, simulated as in tank_death.lua: poisoned values that throw
-- on the operations the live client refuses, and that report their
-- underlying type to type() the way live secrets do. `restricted` models an
-- active keystone, where every cooldown read comes back secret.
local restricted = false
local secretSet = {}
local function Secret(label, underlying)
  local poison = function() error("attempt to use a secret value (" .. label .. ")", 2) end
  local v = setmetatable({}, {
    __add = poison, __sub = poison, __mul = poison, __div = poison,
    __lt = poison, __le = poison, __concat = poison, __len = poison,
    __tostring = function() return "<secret " .. label .. ">" end,
  })
  secretSet[v] = underlying or "number"
  return v
end
local real_type = type
function type(v) if secretSet[v] then return secretSet[v] end return real_type(v) end
function issecretvalue(v) return secretSet[v] ~= nil end

-- string.format must also refuse a secret, as the live one does: the first
-- version of Incoming.lua formatted the spell name into a row string.
local real_format = string.format
string.format = function(fmt, ...)
  for i = 1, select("#", ...) do
    if secretSet[(select(i, ...))] then error("string.format on a secret value", 2) end
  end
  return real_format(fmt, ...)
end

C_SpecializationInfo = {
  GetSpecialization = function() return specID and 1 or nil end,
  GetSpecializationInfo = function() return specID end,
}
C_SpellBook = {
  IsSpellKnown = function(id) return knownSpells[id] == true end,
}
local function Duration(remaining, secret)
  local d = {}
  function d:HasSecretValues() return secret end
  function d:IsZero()
    if secret then error("read a secret duration without asking HasSecretValues first", 2) end
    return remaining <= 0
  end
  function d:GetRemainingDuration()
    if secret then error("read a secret duration without asking HasSecretValues first", 2) end
    return remaining
  end
  return d
end
C_Spell = {
  GetSpellCooldownDuration = function(id) return Duration(cooldowns[id] or 0, restricted) end,
  GetSpellCooldown = function(id)
    if restricted then return { startTime = Secret("startTime"), duration = Secret("duration") } end
    local remaining = cooldowns[id]
    if remaining == nil or remaining <= 0 then return { startTime = 0, duration = 0 } end
    return { startTime = NOW - 1, duration = remaining + 1 }
  end,
  GetSpellCharges = function()
    if restricted then return { currentCharges = Secret("currentCharges") } end
    return nil
  end,
}

-- `timeline` is an array of { info, remaining }; a nil info or a
-- non-number remaining stands in for a field the client would not answer
-- for, which is the case the module has to skip rather than render.
local function InstallTimeline()
  if timeline == nil then
    C_EncounterTimeline = nil
    return
  end
  C_EncounterTimeline = {
    GetSortedEventList = function()
      local handles = {}
      for i = 1, #timeline do handles[i] = i end
      return handles
    end,
    GetEventInfo = function(handle) return timeline[handle].info end,
    GetEventTimeRemaining = function(handle) return timeline[handle].remaining end,
  }
end

local function LoadModule()
  frames = {}
  InstallTimeline()
  local db = { showIncoming = true }
  local MA = {
    Debug = function() end,
    RegisterKeyEventFrame = function() end,
    GetDB = function() return db end,
    state = {},
  }
  assert(loadfile("addon/Postmortem/TankDefensives.lua"))("Postmortem", MA)
  assert(loadfile("addon/Postmortem/TankUtil.lua"))("Postmortem", MA)
  assert(loadfile("addon/Postmortem/Incoming.lua"))("Postmortem", MA)

  local eventFrame
  for _, f in ipairs(frames) do
    if f.events["CHALLENGE_MODE_RESET"] then eventFrame = f end
  end
  assert(eventFrame, "Incoming.lua registered no key-event frame")
  return MA, eventFrame, db
end

local function names(list)
  local out = {}
  for _, e in ipairs(list or {}) do out[e.name] = true end
  return out
end

local failures = 0
local function check(label, ok)
  if ok then print("  ok   " .. label)
  else failures = failures + 1; print("  FAIL " .. label) end
end

-- Scenario 1: the ordinary case -- something coming, something up.
do
  print("an upcoming cast with a defensive available")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  timeline = { { info = { spellName = "Rending Maw" }, remaining = 4.2 } }
  local MA = LoadModule()

  MA:Incoming_OnTick()
  local panel = MA.state.incoming

  check("panel built", panel ~= nil)
  check("the cast is listed", panel.events[1].name == "Rending Maw")
  check("Ardent Defender listed as up", names(panel.ready)["Ardent Defender"])
  check("the countdown is formatted on its own",
    MA:Incoming_FormatTime(panel.events[1].remaining) == "4.2s")
end

-- Scenario 2: the panel is about the encounter, so no encounter means no
-- panel -- not an empty one, which would read as "nothing is coming"
-- during trash where nothing was ever being watched.
do
  print("no timeline, no panel")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  timeline = nil  -- client has no C_EncounterTimeline at all
  local MA = LoadModule()

  MA:Incoming_OnTick()
  check("nothing recorded", MA.state.incoming == nil)

  timeline = {}  -- API present, encounter quiet
  local MA2 = LoadModule()
  MA2:Incoming_OnTick()
  check("empty timeline records nothing either", MA2.state.incoming == nil)
end

-- Scenario 3: a distant cast is noise, not a decision.
do
  print("events beyond the horizon are dropped")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  timeline = {
    { info = { spellName = "Far Off" }, remaining = 40.0 },
    { info = { spellName = "Soon" }, remaining = 3.0 },
  }
  local MA = LoadModule()

  MA:Incoming_OnTick()
  local listed = {}
  for _, e in ipairs(MA.state.incoming.events) do listed[e.name] = true end
  check("near cast kept", listed["Soon"])
  check("distant cast dropped", not listed["Far Off"])
end

-- Scenario 4: a field the client would not answer for. Secret Values mean
-- any of these can come back unusable; a row we cannot label or time must
-- be skipped, never guessed at.
do
  print("unreadable events are skipped, not guessed")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  timeline = {
    { info = nil, remaining = 3.0 },                        -- no info at all
    { info = { spellName = "Nameless" }, remaining = nil }, -- secret timing
    { info = { spellName = "Readable" }, remaining = 2.0 },
  }
  local MA = LoadModule()

  MA:Incoming_OnTick()
  local panel = MA.state.incoming
  check("only the readable row survives", #panel.events == 1)
  check("and it is the right one", panel.events[1].name == "Readable")
end

-- Scenario 5: the line the module will not cross. Shield of the Righteous
-- is Holy Power-gated and carries cooldown 0, so the cooldown API calls it
-- ready regardless of resource.
do
  print("resource-gated buttons are never listed as up")
  specID = PROT_PALADIN
  knownSpells = { [SOTR] = true, [ARDENT_DEFENDER] = true }
  cooldowns = { [SOTR] = 0, [ARDENT_DEFENDER] = 0 }
  timeline = { { info = { spellName = "Rending Maw" }, remaining = 4.0 } }
  local MA = LoadModule()

  MA:Incoming_OnTick()
  local up = names(MA.state.incoming.ready)
  check("Shield of the Righteous not listed", not up["Shield of the Righteous"])
  check("Ardent Defender listed", up["Ardent Defender"])
end

-- Scenario 6: "nothing is up" is the most important thing the panel can
-- say, so it must still build when every major is down.
do
  print("an empty ready list still produces a panel")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true, [GOAK] = true }
  cooldowns = { [ARDENT_DEFENDER] = 60, [GOAK] = 120 }
  timeline = { { info = { spellName = "Rending Maw" }, remaining = 4.0 } }
  local MA = LoadModule()

  MA:Incoming_OnTick()
  check("panel still built", MA.state.incoming ~= nil)
  check("with nothing up", #MA.state.incoming.ready == 0)
end

-- Scenario 7: switched off means off.
do
  print("the setting gates the panel")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  timeline = { { info = { spellName = "Rending Maw" }, remaining = 4.0 } }
  local MA, _, db = LoadModule()

  db.showIncoming = false
  MA:Incoming_OnTick()
  check("nothing recorded when disabled", MA.state.incoming == nil)
end

-- Scenario 8: a finished key has nothing incoming, and a leftover row in
-- the recap would be read as a prediction.
do
  print("the panel clears when the key ends")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  timeline = { { info = { spellName = "Rending Maw" }, remaining = 4.0 } }
  local MA, frame = LoadModule()

  MA:Incoming_OnTick()
  check("panel present mid-key", MA.state.incoming ~= nil)
  frame.handler(frame, "CHALLENGE_MODE_COMPLETED")
  check("cleared on completion", MA.state.incoming == nil)
end

-- Scenario 9: an uncovered spec gets the timeline but no defensive list,
-- rather than an error.
do
  print("a non-tank spec still sees what is coming")
  specID = 63  -- fire mage
  knownSpells = {}
  cooldowns = {}
  timeline = { { info = { spellName = "Rending Maw" }, remaining = 4.0 } }
  local MA = LoadModule()

  MA:Incoming_OnTick()
  check("events still listed", #MA.state.incoming.events == 1)
  check("no defensives claimed", #MA.state.incoming.ready == 0)
end

-- ---------------------------------------------------------------------
-- During an encounter, which is the only time there is anything to show.
-- ---------------------------------------------------------------------

-- Scenario 10: GetEventInfo is SecretWhenEncounterEvent -- spellName comes
-- back secret during a boss. It must reach the overlay as the same opaque
-- value, never formatted, compared or concatenated on the way. The first
-- version string.format'ed it into a row and would have thrown here.
do
  print("encounter: a secret spell name is carried, never touched")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  local secretName = Secret("spellName", "string")
  timeline = { { info = { spellName = secretName }, remaining = 4.0 } }
  local MA = LoadModule()

  local ok, err = pcall(MA.Incoming_OnTick, MA)
  check("no error", ok or print("     " .. tostring(err)))
  check("the event is still listed", ok and #MA.state.incoming.events == 1)
  check("carrying the identical secret, for SetText to take as-is",
    ok and MA.state.incoming.events[1].name == secretName)
end

-- Scenario 11: sorted on time, not trusted from the list. The docs do not
-- say what GetSortedEventList sorts by; truncating an unsorted list to three
-- rows would drop whichever cast actually lands first.
do
  print("events are sorted soonest-first before truncation")
  specID = PROT_PALADIN
  knownSpells = {}
  cooldowns = {}
  timeline = {
    { info = { spellName = "Fourth" }, remaining = 9.0 },
    { info = { spellName = "Third" },  remaining = 6.0 },
    { info = { spellName = "Second" }, remaining = 4.0 },
    { info = { spellName = "First" },  remaining = 1.0 },
  }
  local MA = LoadModule()

  MA:Incoming_OnTick()
  local ev = MA.state.incoming.events
  check("three rows kept", #ev == 3)
  check("soonest first", ev[1].name == "First" and ev[2].name == "Second")
  check("the latest one is the one dropped", ev[3].name == "Third")
end

-- ---------------------------------------------------------------------
-- Under restriction -- an active keystone.
-- ---------------------------------------------------------------------

-- Scenario 12: "No major defensive up" is the most important thing the
-- panel says, so it must only be said when true. With cooldowns
-- unreadable, an empty ready list means "can't tell", not "nothing up".
do
  print("restricted: an empty list is not claimed as 'nothing up'")
  restricted = true
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true, [GOAK] = true }
  cooldowns = {}
  timeline = { { info = { spellName = "Rending Maw" }, remaining = 4.0 } }
  local MA = LoadModule()

  local ok = pcall(MA.Incoming_OnTick, MA)
  check("no error", ok)
  check("the events still show", ok and #MA.state.incoming.events == 1)
  check("nothing claimed up", ok and #MA.state.incoming.ready == 0)
  check("and NOT marked complete, so 'none up' is not shown",
    ok and MA.state.incoming.readyComplete == false)
  restricted = false
end

-- Scenario 13: the same guarantee from the other side -- when everything
-- IS readable and everything is down, "none up" is the true statement.
do
  print("readable and all down: 'nothing up' is a real claim")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true, [GOAK] = true }
  cooldowns = { [ARDENT_DEFENDER] = 60, [GOAK] = 120 }
  timeline = { { info = { spellName = "Rending Maw" }, remaining = 4.0 } }
  local MA = LoadModule()

  MA:Incoming_OnTick()
  check("marked complete", MA.state.incoming.readyComplete == true)
  check("with nothing up", #MA.state.incoming.ready == 0)
end

-- Scenario 14: under restriction, a cast we actually saw is evidence. The
-- panel reads it through TankDeath.lua's accessor so both modules fall back
-- to the same record.
do
  print("restricted: a seen cast stands in for the cooldown API")
  restricted = true
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = {}
  timeline = { { info = { spellName = "Rending Maw" }, remaining = 4.0 } }
  local MA = LoadModule()
  function MA:TankDeath_LastCast(id)
    if id == ARDENT_DEFENDER then return NOW - 300 end  -- 120s cooldown, long back
  end

  MA:Incoming_OnTick()
  check("Ardent Defender estimated up", names(MA.state.incoming.ready)["Ardent Defender"])
  check("and complete, since the only known major was assessed",
    MA.state.incoming.readyComplete == true)
  restricted = false
end

if failures > 0 then
  print(string.format("\n%d check(s) failed", failures))
  os.exit(1)
end
print("\nall checks passed")
