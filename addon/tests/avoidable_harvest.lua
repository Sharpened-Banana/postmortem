-- AvoidableDatabase.lua must harvest the avoidable-damage meter ONCE per
-- key, and only count what this key added to it.
--
-- Two bugs this pins down:
--   * COMPLETED and RESET each started a harvest poll, and a pending
--     C_Timer.After could not be cancelled -- so a key harvested twice, and
--     the second poll overwrote the ticker handle, leaving the first ticker
--     re-harvesting every 0.5 s forever.
--   * The Overall session the harvest reads can carry earlier keys, and
--     every amount in it was added to the DB again each key.
--
-- Run from the repo root:  lua addon/tests/avoidable_harvest.lua
-- See addon/tests/README.md.

local frames = {}
local function NewFrame()
  local f = {events = {}}
  function f:RegisterEvent(e) self.events[e] = true end
  function f:UnregisterEvent(e) self.events[e] = nil end
  function f:SetScript(kind, fn) if kind == "OnEvent" then self.handler = fn end end
  table.insert(frames, f)
  return f
end
function CreateFrame() return NewFrame() end

-- Fake clock + scheduler (After is not cancellable, as in WoW).
local now = 1000
GetTime = function() return now end
time = function() return 1700000000 + math.floor(now) end
local scheduled = {}
local function Schedule(delay, fn, interval)
  local entry = {at = now + delay, fn = fn, interval = interval}
  entry.handle = {Cancel = function() entry.cancelled = true end}
  table.insert(scheduled, entry)
  return entry.handle
end
C_Timer = {
  After = function(d, fn) Schedule(d, fn) end,
  NewTimer = function(d, fn) return Schedule(d, fn) end,
  NewTicker = function(d, fn) return Schedule(d, fn, d) end,
}
local function Advance(seconds)
  local deadline = now + seconds
  while true do
    local bestIdx, best
    for i, e in ipairs(scheduled) do
      if not e.cancelled and e.at <= deadline and (not best or e.at < best.at) then
        bestIdx, best = i, e
      end
    end
    if not best then break end
    now = best.at
    if best.interval then best.at = best.at + best.interval else table.remove(scheduled, bestIdx) end
    best.fn(best.handle)
  end
  now = deadline
end
local function LiveTickers()
  local n = 0
  for _, e in ipairs(scheduled) do if e.interval and not e.cancelled then n = n + 1 end end
  return n
end

-- The meter: one player source; spells[spellID] = amount in the Overall
-- session. sessionReads counts how often the harvest looked.
local spells = {}
local sessionReads = 0
Enum = {DamageMeterType = {AvoidableDamageTaken = 9}}
local function Total()
  local t = 0
  for _, a in pairs(spells) do t = t + a end
  return t
end
C_DamageMeter = {
  GetCombatSessionFromType = function()
    sessionReads = sessionReads + 1
    return {totalAmount = Total(), combatSources = {{name = "Me", sourceGUID = "Player-1", totalAmount = Total()}}}
  end,
  GetCombatSessionSourceFromType = function()
    local list = {}
    for id, a in pairs(spells) do list[#list + 1] = {spellID = id, totalAmount = a} end
    return {combatSpells = list}
  end,
}
C_Spell = {GetSpellInfo = function(id) return {name = "Spell " .. id} end}
local mapID = 501
C_ChallengeMode = {GetActiveChallengeMapID = function() return mapID end}

local db = {}
local MA = {
  GetAvoidableDB = function() return db end,
  Debug = function() end,
  RegisterKeyEventFrame = function() end,
}
assert(loadfile("addon/Postmortem/AvoidableDatabase.lua"))("Postmortem", MA)
local frame
for _, f in ipairs(frames) do if f.events["CHALLENGE_MODE_START"] then frame = f end end
assert(frame, "AvoidableDatabase.lua registered no key-event frame")
local function Fire(e) frame.handler(frame, e) end

-- --- 1. a key the meter already carried an earlier key into -----------
-- Before the key: spell 100 at 5000 (earlier dungeon), 300 at 1000.
spells = {[100] = 5000, [300] = 1000}
Fire("CHALLENGE_MODE_START")
-- This key: 100 grows by 2000, 200 is new at 300, 300 is untouched.
spells = {[100] = 7000, [200] = 300, [300] = 1000}
Fire("CHALLENGE_MODE_COMPLETED")
Fire("CHALLENGE_MODE_RESET") -- the client can send both
Advance(10)

assert(db[100] and db[100].keys == 1,
  "spell 100 harvested " .. tostring(db[100] and db[100].keys) .. " times for one key (COMPLETED + RESET double harvest)")
assert(db[100].total == 2000, "spell 100 total should be this key's 2000, got " .. tostring(db[100].total))
assert(db[100].maps[501], "spell 100 was hit this key and should be tagged with its dungeon")
assert(db[200] and db[200].total == 300 and db[200].keys == 1 and db[200].maps[501], "new spell 200 recorded wrong")
assert(db[300] == nil, "spell 300 was only carried over from an earlier key and must not be recorded")
print("ok: one harvest per key, only this key's amounts")

-- --- 2. no runaway ticker --------------------------------------------
local reads = sessionReads
Advance(120)
assert(sessionReads == reads, string.format("the meter was re-read %d more times after the harvest finished (runaway ticker)", sessionReads - reads))
assert(LiveTickers() == 0, "a harvest ticker is still running: " .. LiveTickers())
assert(db[100].keys == 1 and db[100].total == 2000, "totals changed after the harvest finished")
print("ok: no ticker survives the harvest")

-- --- 3. the next key starts clean ------------------------------------
Fire("CHALLENGE_MODE_START")
spells = {[100] = 7500, [200] = 300, [300] = 1000}
Fire("CHALLENGE_MODE_RESET")
Advance(10)
assert(db[100].keys == 2 and db[100].total == 2500, "second key: spell 100 should add 500 once, got keys="
  .. db[100].keys .. " total=" .. db[100].total)
assert(db[200].keys == 1, "second key: untouched spell 200 was counted again")
Advance(120)
assert(LiveTickers() == 0, "a ticker survived the second key")
print("ok: the next key is harvested once, with its own delta")

-- --- 4. a meter reset between start and end: all of it is this key's --
Fire("CHALLENGE_MODE_START")          -- snapshot sees the big session
spells = {[100] = 400}                -- ...then something reset the meter
Fire("CHALLENGE_MODE_COMPLETED")
Advance(10)
assert(db[100].keys == 3 and db[100].total == 2900, "after a meter reset the whole session is this key's: got total "
  .. db[100].total)
print("ok: a meter reset mid-key is detected")

-- --- 5. an end event with no key started does nothing ----------------
local before = db[100].keys
Fire("CHALLENGE_MODE_RESET")
Advance(10)
assert(db[100].keys == before, "an end event with no key pending harvested again")
print("ok: a stray end event does not harvest")

print("all avoidable-harvest checks passed")
