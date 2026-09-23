-- TankDeath.lua: the live tank death post-mortem.
--
-- What makes this worth a harness is the honesty rule it shares with
-- analysis/tank_death.py: it must never tell a tank they sat on a button
-- they could not press. Three separate ways that could go wrong are
-- covered here -- an untalented spell, a spell still on cooldown, and a
-- resource-gated spell the cooldown API calls "ready" regardless of
-- whether the player had the rage for it.
--
-- Run from the repo root:
--
--     lua addon/tests/tank_death.lua
--
-- See addon/tests/README.md for how these harnesses work.

local frames = {}
local function NewFrame()
  local f = { events = {}, unitEvents = {} }
  function f:RegisterEvent(e) self.events[e] = true end
  function f:UnregisterEvent(e) self.events[e] = nil end
  function f:RegisterUnitEvent(e, unit) self.events[e] = true; self.unitEvents[e] = unit end
  function f:SetScript(_, fn) self.handler = fn end
  table.insert(frames, f)
  return f
end
function CreateFrame() return NewFrame() end

-- Controllable client state, reset per scenario.
--
-- GetTime() is the client's session-relative monotonic clock (what
-- cooldown maths uses); time() is wall clock (what the persisted record
-- carries, since the analyzer has to line it up against combat-log
-- timestamps). Both exist in the WoW client and neither exists in plain
-- Lua, so both are stubbed -- time() the same way chesttimer_reload.lua
-- does it.
local NOW = 1000.0
function GetTime() return NOW end
time = os.time

local PROT_PALADIN = 66
local knownSpells, cooldowns, specID

-- Secret Values, simulated the way interrupts_secret.lua does it:
-- issecretvalue() answers from a set the harness controls, and a secret is
-- a poisoned object that throws on arithmetic, ordering, concatenation or
-- length -- the operations the live client refuses. (The live client also
-- refuses a secret as a table key; plain Lua cannot intercept that, so the
-- cast-handler scenario below checks that the guard was consulted instead.)
--
-- `restricted` models an active keystone: every cooldown the client hands
-- back is secret. The API names and flags these stubs follow were verified
-- 2026-09-23 against Blizzard's generated documentation, build 12.1.0.69933.
local restricted = false
local secretSet, consulted = {}, {}
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
-- A live secret reports its underlying type -- interrupts_secret.lua's
-- incident was a value "accepted by the checks" that then threw. Without
-- this, a plain-table stand-in answers type() with "table", and a
-- `type(x) ~= "number"` guard quietly bails out in the harness while the
-- same code throws in a key. That is exactly how the first version of this
-- module passed here.
local real_type = type
function type(v)
  if secretSet[v] then return secretSet[v] end
  return real_type(v)
end
function issecretvalue(v)
  consulted[#consulted + 1] = v
  return secretSet[v] ~= nil
end

C_SpecializationInfo = {
  GetSpecialization = function() return specID and 1 or nil end,
  GetSpecializationInfo = function() return specID end,
}
-- The live call. (C_SpellBook.IsSpellKnownOrOverridesKnown, which an earlier
-- version of this harness stubbed, does not exist on the live client.)
C_SpellBook = {
  IsSpellKnown = function(id) return knownSpells[id] == true end,
}

local lastIgnoreGCD
-- GetSpellCooldownDuration's LuaDurationObject. Under restriction it
-- reports HasSecretValues() and REFUSES the reads -- so a module that reads
-- before asking fails here, the way it would in a key.
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
  GetSpellCooldownDuration = function(id, ignoreGCD)
    lastIgnoreGCD = ignoreGCD
    return Duration(cooldowns[id] or 0, restricted)
  end,
  -- SecretWhenCooldownsRestricted on the live client. Poisoned when
  -- restricted, so any path that still reads it fails loudly.
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

local ARDENT_DEFENDER = 31850   -- 120s cooldown
local DIVINE_SHIELD = 642       -- 300s cooldown
local SOTR = 53600              -- resource-gated: cooldown 0 in the table
local GOAK = 86659              -- 300s cooldown

local function LoadModule()
  frames = {}
  local MA = {
    Debug = function() end,
    RegisterKeyEventFrame = function() end,
    state = {},
    -- Bootstrap.lua hands this over as PostmortemTankDB.global; the
    -- capture appends to it and the Python side reads it back.
    tankDb = { deaths = {} },
  }
  -- Load in .toc order: the generated data table, the shared client
  -- readers, then the module under test.
  assert(loadfile("addon/Postmortem/TankDefensives.lua"))("Postmortem", MA)
  assert(loadfile("addon/Postmortem/TankUtil.lua"))("Postmortem", MA)
  assert(loadfile("addon/Postmortem/TankDeath.lua"))("Postmortem", MA)

  local eventFrame
  for _, f in ipairs(frames) do
    if f.events["PLAYER_DEAD"] then eventFrame = f end
  end
  assert(eventFrame, "TankDeath.lua registered no PLAYER_DEAD frame")
  return MA, eventFrame
end

local function names(list)
  local out = {}
  for _, e in ipairs(list or {}) do out[e.name] = true end
  return out
end

local failures = 0
local function check(label, ok)
  if ok then
    print("  ok   " .. label)
  else
    failures = failures + 1
    print("  FAIL " .. label)
  end
end

-- Scenario 1: a talented, off-cooldown major that was never pressed is
-- exactly the finding this feature exists to produce.
do
  print("ready and unused")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  local MA, frame = LoadModule()

  frame.handler(frame, "PLAYER_DEAD")
  local result = MA.state.lastTankDeath

  check("a post-mortem was recorded", result ~= nil)
  check("Ardent Defender reported ready", names(result.readyUnused)["Ardent Defender"])
  check("spec name resolved", result.specName == "Protection Paladin")
end

-- Scenario 2: the talent case the combat log physically cannot resolve.
-- Divine Shield is in the table for this spec but not in the spellbook,
-- so it must not appear at all.
do
  print("untalented spells are never reported")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }  -- Divine Shield NOT known
  cooldowns = { [ARDENT_DEFENDER] = 0, [DIVINE_SHIELD] = 0 }
  local MA, frame = LoadModule()

  frame.handler(frame, "PLAYER_DEAD")
  local reported = names(MA.state.lastTankDeath.readyUnused)

  check("Ardent Defender still reported", reported["Ardent Defender"])
  check("Divine Shield not reported", not reported["Divine Shield"])
end

-- Scenario 3: a spell genuinely on cooldown is not available, whatever
-- the table's baseline says.
do
  print("spells on cooldown are not reported")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true, [GOAK] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0, [GOAK] = 45 }
  local MA, frame = LoadModule()

  frame.handler(frame, "PLAYER_DEAD")
  local reported = names(MA.state.lastTankDeath.readyUnused)

  check("Ardent Defender reported", reported["Ardent Defender"])
  check("Guardian of Ancient Kings not reported", not reported["Guardian of Ancient Kings"])
end

-- Scenario 4: Shield of the Righteous carries cooldown 0 because it is
-- Holy Power-gated. The cooldown API happily calls it ready even with no
-- Holy Power banked, so scoring it would be a guess.
do
  print("resource-gated buttons are never scored")
  specID = PROT_PALADIN
  knownSpells = { [SOTR] = true, [ARDENT_DEFENDER] = true }
  cooldowns = { [SOTR] = 0, [ARDENT_DEFENDER] = 0 }
  local MA, frame = LoadModule()

  frame.handler(frame, "PLAYER_DEAD")
  local reported = names(MA.state.lastTankDeath.readyUnused)

  check("Shield of the Righteous not scored", not reported["Shield of the Righteous"])
  check("Ardent Defender still scored", reported["Ardent Defender"])
end

-- Scenario 5: a defensive pressed inside its own duration was being held,
-- not ignored -- the live mirror of the analyzer's active_at_death.
do
  print("a held defensive is reported as held, not unused")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  local MA, frame = LoadModule()

  -- Cast it 2s ago; Ardent Defender's duration is 8s, so it was up.
  frame.handler(frame, "UNIT_SPELLCAST_SUCCEEDED", "player", nil, ARDENT_DEFENDER)
  NOW = NOW + 2
  frame.handler(frame, "PLAYER_DEAD")
  local result = MA.state.lastTankDeath

  check("reported as held", names(result.held)["Ardent Defender"])
  check("not also reported as unused", not names(result.readyUnused)["Ardent Defender"])
  NOW = 1000.0
end

-- Scenario 6: a non-tank (or uncovered) spec produces nothing at all
-- rather than a misleading empty finding.
do
  print("uncovered specs record nothing")
  specID = 63  -- fire mage: not in the tank table
  knownSpells = {}
  cooldowns = {}
  local MA, frame = LoadModule()

  frame.handler(frame, "PLAYER_DEAD")
  check("no post-mortem recorded", MA.state.lastTankDeath == nil)
end

-- Scenario 7: a new key must not inherit the previous key's casts.
do
  print("key start clears carried state")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  local MA, frame = LoadModule()

  frame.handler(frame, "UNIT_SPELLCAST_SUCCEEDED", "player", nil, ARDENT_DEFENDER)
  frame.handler(frame, "PLAYER_DEAD")
  check("held before reset", names(MA.state.lastTankDeath.held)["Ardent Defender"])

  frame.handler(frame, "CHALLENGE_MODE_START")
  check("last result cleared", MA.state.lastTankDeath == nil)

  frame.handler(frame, "PLAYER_DEAD")
  check("cast history cleared, now reads as unused",
    names(MA.state.lastTankDeath.readyUnused)["Ardent Defender"])
end

-- Scenario 8: the spellbook snapshot. This is the whole reason the
-- capture is persisted -- the combat log has no spellbook, so these two
-- lists are the only way the analyzer can tell "never talented" apart
-- from "had it, never pressed it".
do
  print("the spellbook snapshot is recorded and persisted")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true, [SOTR] = true }  -- rest unknown
  cooldowns = { [ARDENT_DEFENDER] = 0, [SOTR] = 0 }
  local MA, frame = LoadModule()

  frame.handler(frame, "PLAYER_DEAD")
  local result = MA.state.lastTankDeath

  local function has(list, id)
    for _, v in ipairs(list) do if v == id then return true end end
    return false
  end

  check("known lists Ardent Defender", has(result.known, ARDENT_DEFENDER))
  check("notKnown lists Divine Shield", has(result.notKnown, DIVINE_SHIELD))
  check("a spell is never in both", not has(result.known, DIVINE_SHIELD))

  local persisted = MA.tankDb.deaths[1]
  check("one record persisted", persisted ~= nil and #MA.tankDb.deaths == 1)
  check("record carries the spec", persisted.specID == PROT_PALADIN)
  check("record carries known", has(persisted.known, ARDENT_DEFENDER))
  check("record carries a wall-clock ts", type(persisted.ts) == "number" and persisted.ts > 1e9)
end

-- Scenario 9: a key with several deaths keeps all of them, not just the
-- last -- the overlay shows the most recent, the results window shows the
-- key.
do
  print("every death in the key is kept")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  local MA, frame = LoadModule()

  frame.handler(frame, "CHALLENGE_MODE_START")
  frame.handler(frame, "PLAYER_DEAD")
  frame.handler(frame, "PLAYER_DEAD")
  frame.handler(frame, "PLAYER_DEAD")

  check("three kept in key history", #MA:TankDeath_All() == 3)
  check("three persisted", #MA.tankDb.deaths == 3)
  check("overlay still sees the latest", MA.state.lastTankDeath ~= nil)

  -- A new key resets the in-key history but must NOT wipe the capture:
  -- that is cross-key data the analyzer reads.
  frame.handler(frame, "CHALLENGE_MODE_START")
  check("key history cleared", #MA:TankDeath_All() == 0)
  check("persisted capture survives a new key", #MA.tankDb.deaths == 3)
end

-- Scenario 10: a record with no findings is still persisted when it
-- learned something about the spellbook. "Knew all of them, pressed none"
-- is precisely what the analyzer wants, and dropping it loses that.
do
  print("a findings-free record is still worth keeping")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 999 }  -- known, but on cooldown: no finding
  local MA, frame = LoadModule()

  frame.handler(frame, "PLAYER_DEAD")
  local result = MA.state.lastTankDeath

  check("still recorded", result ~= nil)
  check("no findings", #result.readyUnused == 0 and #result.held == 0)
  check("but the spellbook went with it", #result.known > 0)
  check("and it was persisted", #MA.tankDb.deaths == 1)
end

-- ---------------------------------------------------------------------
-- Under restriction -- the state an active keystone puts the client in.
-- Everything above ran with readable cooldowns, which is why the first
-- version passed every check here and would still have failed in a key.
-- ---------------------------------------------------------------------

-- Scenario 11: the capture must survive unreadable cooldowns. The spellbook
-- snapshot is what the analyzer needs, and it depends only on IsSpellKnown,
-- which is never secret -- losing it to an unrelated cooldown read is what
-- the first version would have done.
do
  print("restricted: the spellbook snapshot survives unreadable cooldowns")
  restricted = true
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true, [GOAK] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0, [GOAK] = 0 }
  local MA, frame = LoadModule()

  local ok = pcall(frame.handler, frame, "PLAYER_DEAD")
  local result = MA.state.lastTankDeath
  check("no error escapes", ok)
  check("a record was still made", result ~= nil)
  check("with the spellbook in it", result and #result.known == 2)
  check("and it was persisted", #MA.tankDb.deaths == 1)
  -- No cast was seen, so there is no evidence either way; saying "ready"
  -- from an unreadable cooldown would be a guess.
  check("nothing claimed without evidence", result and #result.readyUnused == 0)
  restricted = false
end

-- Scenario 12: with cooldowns unreadable, a cast we DID see is evidence.
-- Ardent Defender (120s) pressed 300s ago is back by any reckoning.
do
  print("restricted: our own cast times stand in for the cooldown API")
  restricted = true
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true, [GOAK] = true }
  cooldowns = {}
  local MA, frame = LoadModule()

  frame.handler(frame, "UNIT_SPELLCAST_SUCCEEDED", "player", "cast-1", ARDENT_DEFENDER)
  frame.handler(frame, "UNIT_SPELLCAST_SUCCEEDED", "player", "cast-2", GOAK)
  NOW = NOW + 300   -- AD (120s) is long back; GoAK (300s) is inside its margin
  frame.handler(frame, "PLAYER_DEAD")
  local result = MA.state.lastTankDeath

  local ad
  for _, e in ipairs(result.readyUnused) do if e.id == ARDENT_DEFENDER then ad = e end end
  check("Ardent Defender estimated ready", ad ~= nil)
  check("and labelled as an estimate", ad and ad.source == "estimate")
  check("GoAK not claimed inside the margin", not names(result.readyUnused)["Guardian of Ancient Kings"])
  NOW = 1000.0
  restricted = false
end

-- Scenario 13: the cast event itself arriving secret. The live client
-- refuses a secret as a table key -- the 2026-09-15 incident in
-- interrupts_secret.lua was 15,976 errors from one key -- and this handler
-- runs on every cast with no pcall in front of it.
do
  print("a secret cast event is skipped, never used as a key")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  local MA, frame = LoadModule()

  local secretID = Secret("spellID")
  consulted = {}
  local ok = pcall(frame.handler, frame, "UNIT_SPELLCAST_SUCCEEDED", "player", "cast-3", secretID)
  local asked = false
  for _, v in ipairs(consulted) do if v == secretID then asked = true end end
  check("no error escapes the handler", ok)
  check("issecretvalue was consulted before the key was used", asked)

  frame.handler(frame, "PLAYER_DEAD")
  check("an unreadable cast is not counted as held",
    not names(MA.state.lastTankDeath.held)["Ardent Defender"])
end

-- Scenario 14: readiness must ignore the global cooldown, or every spell
-- reads "on cooldown" for a second and a half after any button press.
do
  print("cooldown reads ignore the GCD")
  specID = PROT_PALADIN
  knownSpells = { [ARDENT_DEFENDER] = true }
  cooldowns = { [ARDENT_DEFENDER] = 0 }
  local MA, frame = LoadModule()
  lastIgnoreGCD = nil
  frame.handler(frame, "PLAYER_DEAD")
  check("ignoreGCD passed as true", lastIgnoreGCD == true)
end

if failures > 0 then
  print(string.format("\n%d check(s) failed", failures))
  os.exit(1)
end
print("\nall checks passed")
