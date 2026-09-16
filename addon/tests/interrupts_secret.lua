-- Interrupts.lua against Secret Values: a meter source name or GUID that
-- the client refuses as a table key must never throw out of the tick, and
-- the previous good values must survive a bad pass. Run from the repo
-- root: lua addon/tests/interrupts_secret.lua
--
-- Real incident (2026-09-15): "Interrupts.lua:89: attempted to index a
-- table that cannot be indexed with secret keys (x15976)" from one key.

local MA = { state = {} }
function MA:Debug() end
function MA:RegisterKeyEventFrame() end
Enum = { DamageMeterType = { Interrupts = 5 } }
function CreateFrame()
  return { RegisterEvent = function() end, SetScript = function() end }
end

-- Fake meter: one Overall session with sources we control per test.
local sources = {}
C_DamageMeter = {
  GetCombatSessionFromType = function() return { totalAmount = 0, combatSources = sources } end,
  GetCombatSessionSourceFromType = function() return nil end,  -- unused here; must exist
}
-- The secrecy primitives: a name/guid listed in `secret` reads as
-- inaccessible; a key listed in `refused` is accepted by the checks but
-- throws when used as a table key (the live 12.1 shape).
local secret, refused = {}, {}
function issecretvalue(v) return secret[v] == true end
local real_rawset = rawset
function rawset(t, k, v)
  if refused[k] then error("attempted to index a table that cannot be indexed with secret keys") end
  return real_rawset(t, k, v)
end
local real_rawget = rawget
function rawget(t, k)
  if refused[k] then error("attempted to index a table that cannot be indexed with secret keys") end
  return real_rawget(t, k)
end

assert(loadfile("addon/Postmortem/MeterUtil.lua"))("Postmortem", MA)
assert(loadfile("addon/Postmortem/Interrupts.lua"))("Postmortem", MA)

local function src(guid, name, total) return { sourceGUID = guid, name = name, totalAmount = total } end

local function check(label, cond) assert(cond, label); print("ok: " .. label) end

-- 1. plain data: list output, baseline subtracted
sources = { src("Player-A", "Alice", 4), src("Player-B", "Bob", 1) }
MA.state.interrupts = { total = 0, byPlayer = {} }
-- snapshot a baseline the way CHALLENGE_MODE_START does
local baselineFn = nil
-- Interrupts keeps SnapshotBaseline local; drive it through the event
-- frame instead: re-create with a capturing CreateFrame.
local handler
CreateFrame = function()
  return { RegisterEvent = function() end, SetScript = function(_, _, fn) handler = fn end }
end
assert(loadfile("addon/Postmortem/Interrupts.lua"))("Postmortem", MA)
handler(nil, "CHALLENGE_MODE_START")
sources[1].totalAmount = 7   -- Alice: 3 new kicks; Bob: none
MA:Interrupts_OnTick()
check("total counts kicks since the key started", MA.state.interrupts.total == 3)
check("byPlayer is a list, not a name-keyed table",
  #MA.state.interrupts.byPlayer == 1 and MA.state.interrupts.byPlayer[1].name == "Alice"
  and MA.state.interrupts.byPlayer[1].kicks == 3 and MA.state.interrupts.byPlayer.Alice == nil)

-- 2. a name the checks pass but the client refuses as a key: no throw,
--    last good values kept (the list form never keys by it anyway)
refused["Carol"] = true
sources[#sources + 1] = src("Player-C", "Carol", 2)
local ok = pcall(MA.Interrupts_OnTick, MA)
check("a refused NAME never throws out of the tick", ok)
check("Carol is counted (names are values, not keys)",
  MA.state.interrupts.total == 5 and MA.state.interrupts.byPlayer[2].name == "Carol")

-- 3. a GUID refused as a key: the tick keeps the previous values
refused["Player-D"] = true
sources[#sources + 1] = src("Player-D", "Dan", 9)
local before = MA.state.interrupts.total
ok = pcall(MA.Interrupts_OnTick, MA)
check("a refused GUID never throws out of the tick", ok)
check("...and the previous tick's values survive", MA.state.interrupts.total == before)

-- 4. a secret value flagged by canaccessvalue (12.1) is treated as secret
refused = {}
canaccessvalue = function(v) return v ~= "Erin" end
sources = { src("Player-E", "Erin", 3) }
handler(nil, "CHALLENGE_MODE_START")
sources[1].totalAmount = 5
local prev = MA.state.interrupts.total
ok = pcall(MA.Interrupts_OnTick, MA)
check("canaccessvalue()=false counts as secret: no throw, values kept", ok and MA.state.interrupts.total == prev)
check("MeterUtil_IsSecret honours canaccessvalue", MA:MeterUtil_IsSecret("Erin") and not MA:MeterUtil_IsSecret("Alice"))

-- 5. a baseline whose GUID is refused at key start is dropped, not half-kept
canaccessvalue = nil
refused = { ["Player-F"] = true }
sources = { src("Player-F", "Fay", 10), src("Player-G", "Gus", 2) }
handler(nil, "CHALLENGE_MODE_START")
refused = {}
sources[2].totalAmount = 3
MA:Interrupts_OnTick()
check("a baseline with a refused GUID is dropped (counts from zero this key)",
  MA.state.interrupts.total == 13)

print("all interrupts-secret checks passed")
