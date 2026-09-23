-- Incoming.lua
-- The pre-pull half of the tanking toolkit: what the encounter is about to
-- do, and which of your own defensives are up to meet it.
--
-- Why this shape, and not the obvious one
-- ---------------------------------------
-- The obvious tank addon reads incoming damage and tells you what to press.
-- That addon is not buildable on retail any more and will not become
-- buildable: patch 12.0's Secret Values put enemy casts, health, absorbs and
-- damage taken out of addon reach specifically so that addons cannot make
-- combat decisions for the player. Blizzard's own framing is that combat
-- state is a black box addons may resize and repaint but never look inside.
--
-- What they did ship is C_EncounterTimeline (12.0.1): the sanctioned feed of
-- what an encounter is about to do, the same one the boss mods moved onto.
-- It is display-and-schedule data -- several of its functions are declared
-- SecretArguments = "NotAllowed" and its events carry
-- SecretWhenEncounterEvent -- so this file treats it as something to lay out,
-- never something to compute on.
--
-- The line this file deliberately does not cross
-- ----------------------------------------------
-- It shows what is coming and what you have. It does NOT rank them, score
-- them, or say which button to press. That distinction is not cosmetic
-- caution: "suggest the next action from combat state" is the exact pattern
-- the 12.0 restrictions exist to prevent, and an addon that edges into it is
-- building on ground Blizzard has said it intends to keep taking back. A
-- tank reading "Rending Maw in 4s" next to "Shield Wall ready" has what they
-- need; the judgement stays theirs.
--
-- Everything read here is either the sanctioned timeline or the player's own
-- spec, spellbook and cooldowns (see TankUtil.lua). No combat value is read.

local ADDON_NAME, MA = ...

-- Only events landing inside this window are worth showing: a cast 40s out
-- is not a decision, it is noise, and the panel has to stay glanceable
-- during a pull.
local HORIZON_S = 12.0

-- Cap on rows, so a busy timeline cannot grow the overlay off-screen.
local MAX_ROWS = 3

-- True when the client exposes the encounter timeline at all. Pre-12.0
-- clients (and the Classic flavors) have none, and that is a "show nothing"
-- case, never an error.
local function TimelineAvailable()
  return C_EncounterTimeline ~= nil
      and C_EncounterTimeline.GetSortedEventList ~= nil
end

-- The upcoming timeline events inside HORIZON_S, soonest first.
--
-- Returns a plain array of { name, remaining }. What is readable here was
-- verified 2026-09-23 against Blizzard's generated API documentation (build
-- 12.1.0.69933), after the first version got it wrong:
--
--   * GetEventTimeRemaining carries no secret annotation -> a real number,
--     safe to filter and sort on.
--   * GetEventInfo is SecretWhenEncounterEvent, and inside its struct only
--     id/source/duration/maxQueueDuration are NeverSecret. spellName,
--     spellID, severity and isApproximate are all secret during a boss --
--     which is the only time there are events to show.
--
-- So the name is carried as an opaque value and handed straight to a
-- FontString, whose SetText accepts secrets from addon code
-- (SecretArguments = "AllowedWhenTainted"). It is never formatted, compared
-- or concatenated here: any of those throws. The first version did all
-- three, and also tested isApproximate for a "?" marker; that marker is gone
-- because the flag cannot be read.
local function UpcomingEvents()
  if not TimelineAvailable() then return {} end

  local list = C_EncounterTimeline.GetSortedEventList()
  if type(list) ~= "table" then return {} end

  local out = {}
  for _, eventID in ipairs(list) do
    local remaining = C_EncounterTimeline.GetEventTimeRemaining
        and C_EncounterTimeline.GetEventTimeRemaining(eventID)
    -- Unannotated today; checked anyway, because it is the one value this
    -- file does arithmetic on and an annotation can be added in any patch.
    if type(remaining) == "number" and not MA:TankUtil_IsSecret(remaining)
        and remaining >= 0 and remaining <= HORIZON_S then
      local info = C_EncounterTimeline.GetEventInfo and C_EncounterTimeline.GetEventInfo(eventID)
      -- Not even a nil check on spellName: it is documented Nilable = false,
      -- and "~= nil" is still a comparison on a value that may be secret.
      if type(info) == "table" then
        out[#out + 1] = { name = info.spellName, remaining = remaining }
      end
    end
  end
  -- Sorted here rather than trusted from the list: the documentation does
  -- not say what GetSortedEventList sorts by, and truncating to MAX_ROWS in
  -- the wrong order would drop the cast that lands first.
  table.sort(out, function(a, b) return a.remaining < b.remaining end)
  for i = #out, MAX_ROWS + 1, -1 do out[i] = nil end
  return out
end

-- The player's own major defensives that are ready right now.
--
-- Deliberately majors only. Active mitigation (Shield Block, Ironfur,
-- Shield of the Righteous) is resource-gated, so the cooldown API calls it
-- ready whether or not the player can afford it -- the same reason
-- TankDeath.lua never scores those. Listing them here would be a panel that
-- lies during a rage drought.
--
-- Returns the ready list AND whether every known major could be assessed.
-- The distinction matters for the most important thing the panel says:
-- "No major defensive up" is only true when we know the state of all of
-- them. Under cooldown restriction some may be unreadable, and an empty
-- list then means "can't tell", not "nothing up".
local function ReadyDefensives()
  local T = MA.TankDefensives
  if not T or not T.bySpec then return {}, false end

  local specID = MA:TankUtil_PlayerSpecID()
  if not specID then return {}, false end
  local entries = T.bySpec[specID]
  if not entries then return {}, false end

  local now = GetTime()
  local ready, complete, assessed = {}, true, 0
  for _, entry in ipairs(entries) do
    if entry.cooldown > 0 and MA:TankUtil_IsKnown(entry.id) then
      local castAt = MA.TankDeath_LastCast and MA:TankDeath_LastCast(entry.id)
      local ok, state = pcall(MA.TankUtil_Readiness, MA, entry, castAt, now)
      if not ok or state == nil then
        complete = false
      else
        assessed = assessed + 1
        if state == "ready" then
          ready[#ready + 1] = { id = entry.id, name = entry.name }
        end
      end
    end
  end
  -- Knowing nothing is not knowing everything.
  if assessed == 0 then complete = false end
  return ready, complete
end

-- Recomputes the panel's state into MA.state.incoming, or nil when there is
-- nothing to show. Driven from Tracker.lua's once-per-second tick, which is
-- fast enough for a 12-second horizon and costs nothing when the timeline is
-- empty (the common case: trash).
function MA:Incoming_OnTick()
  if not MA:GetDB().showIncoming then
    MA.state.incoming = nil
    return
  end

  local events = UpcomingEvents()
  if #events == 0 then
    -- Nothing incoming: drop the panel entirely rather than leaving a stale
    -- "ready" list on screen implying something is coming.
    MA.state.incoming = nil
    return
  end

  local ready, complete = ReadyDefensives()
  MA.state.incoming = { events = events, ready = ready, readyComplete = complete }
end

-- The countdown half of an event row. Only the number is formatted here;
-- the spell name is a secret during encounters and goes to its own
-- FontString untouched (see UpcomingEvents).
function MA:Incoming_FormatTime(remaining)
  return string.format("%.1fs", remaining)
end

local eventFrame = CreateFrame("Frame")
MA:RegisterKeyEventFrame(eventFrame)
eventFrame:RegisterEvent("CHALLENGE_MODE_START")
eventFrame:RegisterEvent("CHALLENGE_MODE_RESET")
eventFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")

eventFrame:SetScript("OnEvent", function(self, event, ...)
  -- The panel is live-only: a finished key has nothing incoming, and a
  -- leftover row in the recap would be read as a prediction.
  MA.state.incoming = nil
end)
