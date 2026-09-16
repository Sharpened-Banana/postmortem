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
local NOW = 1000.0
function GetTime() return NOW end

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
  }
  -- The generated data table first, exactly as the .toc orders them.
  assert(loadfile("addon/Postmortem/TankDefensives.lua"))("Postmortem", MA)
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

if failures > 0 then
  print(string.format("\n%d check(s) failed", failures))
  os.exit(1)
end
print("\nall checks passed")
