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
-- Returns a plain array of { name, remaining, severity }. Anything the
-- client won't answer for is skipped rather than guessed at: a row we
-- cannot label or time is worse than no row.
local function UpcomingEvents()
  if not TimelineAvailable() then return {} end

  local list = C_EncounterTimeline.GetSortedEventList()
  if type(list) ~= "table" then return {} end

  local out = {}
  for _, handle in ipairs(list) do
    local info = C_EncounterTimeline.GetEventInfo and C_EncounterTimeline.GetEventInfo(handle)
    local remaining = C_EncounterTimeline.GetEventTimeRemaining
        and C_EncounterTimeline.GetEventTimeRemaining(handle)

    -- A secret or missing field means this row is not ours to render.
    if type(info) == "table" and type(remaining) == "number"
        and type(info.spellName) == "string"
        and remaining >= 0 and remaining <= HORIZON_S then
      out[#out + 1] = {
        name = info.spellName,
        remaining = remaining,
        severity = info.severity,
        -- The timeline marks a cast that may not actually happen; saying
        -- "maybe" is more honest than showing it as certain.
        approximate = info.isApproximate and true or false,
      }
      if #out >= MAX_ROWS then break end
    end
  end
  return out
end

-- The player's own major defensives that are off cooldown right now.
--
-- Deliberately majors only. Active mitigation (Shield Block, Ironfur,
-- Shield of the Righteous) is resource-gated, so the cooldown API calls it
-- ready whether or not the player can afford it -- the same reason
-- TankDeath.lua never scores those. Listing them here would be a panel that
-- lies during a rage drought.
local function ReadyDefensives()
  local T = MA.TankDefensives
  if not T or not T.bySpec then return {} end

  local specID = MA:TankUtil_PlayerSpecID()
  if not specID then return {} end
  local entries = T.bySpec[specID]
  if not entries then return {} end

  local ready = {}
  for _, entry in ipairs(entries) do
    if entry.cooldown > 0 and MA:TankUtil_IsKnown(entry.id) then
      local remaining = MA:TankUtil_CooldownRemaining(entry.id)
      if remaining ~= nil and remaining <= 0 then
        ready[#ready + 1] = { id = entry.id, name = entry.name }
      end
    end
  end
  return ready
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

  MA.state.incoming = { events = events, ready = ReadyDefensives() }
end

-- Formats one event row: "Rending Maw  4.2s". Kept here rather than in
-- Overlay.lua so the panel's wording lives with the data that shapes it.
function MA:Incoming_FormatEvent(event)
  return string.format("%s  %.1fs%s",
    event.name, event.remaining, event.approximate and "?" or "")
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
