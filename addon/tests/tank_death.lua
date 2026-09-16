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

C_SpecializationInfo = {
  GetSpecialization = function() return specID and 1 or nil end,
  GetSpecializationInfo = function() return specID end,
}
C_SpellBook = {
  IsSpellKnownOrOverridesKnown = function(id) return knownSpells[id] == true end,
}
C_Spell = {
  -- No charges in these scenarios; the module falls through to cooldowns.
  GetSpellCharges = function() return nil end,
  GetSpellCooldown = function(id)
    local remaining = cooldowns[id]
    if remaining == nil or remaining <= 0 then
      return { startTime = 0, duration = 0 }
    end
    return { startTime = NOW - 1, duration = remaining + 1 }
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

if failures > 0 then
  print(string.format("\n%d check(s) failed", failures))
  os.exit(1)
end
print("\nall checks passed")
