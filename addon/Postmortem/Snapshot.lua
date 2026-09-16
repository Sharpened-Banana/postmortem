-- Snapshot.lua
-- "What just happened" marker for healers and tanks: a keybind (or
-- /pm snapshot) pressed mid-key plants a marker in the combat log that the
-- analyzer / desktop app later turn into a snapshot report of the window
-- around the press (docs/SNAPSHOT.md, section 1 -- this file is the addon
-- side of that contract; keep the three sides in sync).
--
-- How the marker works: WoW writes a timestamped COMBAT_LOG_VERSION header
-- line every time combat logging is switched ON (verified against real
-- logs, which carry 4-12 of them). Toggling logging off/on N times in
-- quick succession therefore leaves N header lines within ~1.5 s, and N
-- encodes the presser's role (HEALER = 2, TANK = 3, anything else = 4).
-- Roles are unique inside a key, so the role identifies the player without
-- anyone needing to know the local character's name. A single header (a
-- normal toggle by this or another addon, or the 2 s-apart login pair) is
-- never read as a marker.

local ADDON_NAME, MA = ...

-- Keybinding labels. Bindings.xml refers to these by name (header
-- "POSTMORTEM" -> BINDING_HEADER_POSTMORTEM, binding "POSTMORTEM_SNAPSHOT"
-- -> BINDING_NAME_POSTMORTEM_SNAPSHOT); they must be globals set from Lua
-- before the Key Bindings panel is opened, which file-load time guarantees.
BINDING_HEADER_POSTMORTEM = "Postmortem"
BINDING_NAME_POSTMORTEM_SNAPSHOT = "Mark a snapshot"

-- Toggle i (1-based) switches logging OFF at (i-1)*TOGGLE_SPACING_S and
-- back ON TOGGLE_OFF_S later. 0.1 s off loses at most a handful of events
-- (the window's data does not depend on them); 0.25 s spacing keeps all N
-- headers comfortably inside the analyzer's 1.5 s clustering rule even for
-- N = 4 (last header at 0.85 s) while staying far outside the 2 s-apart
-- login pair that must never be mistaken for one.
local TOGGLE_SPACING_S = 0.25
local TOGGLE_OFF_S = 0.1

-- Header count per role. Anything not listed (DAMAGER, NONE, nil) is the
-- "general" marker.
local ROLE_TOGGLES = { HEALER = 2, TANK = 3 }
local ROLE_LABELS = { HEALER = "healer", TANK = "tank" }
local GENERAL_TOGGLES = 4
local GENERAL_LABEL = "general"

-- Presses within this many seconds of the previous one are ignored: a
-- second toggle burst overlapping the first would merge into one bigger
-- cluster and be read as the wrong role.
local COOLDOWN_S = 5

-- Bounded offline record in SavedVariables (db.snapshotMarks). 50 presses
-- is many sessions' worth; the combat log itself is the primary record.
local MARKS_CAP = 50

-- How long the HUD status row shows "Snapshot marked".
local FLASH_S = 4

-- GetTime() of the last accepted press; nil until the first one.
local lastPressAt = nil

-- Generation counter for the scheduled toggles. C_Timer.After has no
-- cancel, so each closure captures the generation it was scheduled under
-- and bails if it changed -- bumped on CHALLENGE_MODE_COMPLETED/RESET so a
-- press in the last second of a key can't leave a stray LoggingCombat(true)
-- landing after CombatLogging.lua's own post-key stop.
local generation = 0

local function FormatMSS(seconds)
  seconds = math.floor(tonumber(seconds) or 0)
  return string.format("%d:%02d", math.floor(seconds / 60), seconds % 60)
end

-- Zone name and key level for the SavedVariables record, from the same
-- source RunHistory.lua uses (ChestTimer.lua's state.chestTimer, filled on
-- CHALLENGE_MODE_START), falling back to the live API in case this fires
-- before ChestTimer populated it (e.g. straight after a /reload).
local function CurrentZoneAndLevel()
  local ct = (MA.state and MA.state.chestTimer) or {}
  local mapID, level = ct.mapID, ct.level
  if not mapID and C_ChallengeMode and C_ChallengeMode.GetActiveChallengeMapID then
    mapID = C_ChallengeMode.GetActiveChallengeMapID()
  end
  if not level and C_ChallengeMode and C_ChallengeMode.GetActiveKeystoneInfo then
    level = C_ChallengeMode.GetActiveKeystoneInfo()
  end
  local zone = nil
  if mapID and C_ChallengeMode and C_ChallengeMode.GetMapUIInfo then
    zone = C_ChallengeMode.GetMapUIInfo(mapID)
  end
  return zone, level
end

local function Say(msg)
  print("|cffd7a94cPostmortem|r: " .. msg)
end

-- Schedules the N off/on toggles. Goes through CombatLogging_SetState
-- rather than LoggingCombat() directly so the module's "only write on
-- change" rule applies: its 2 s re-assert ticker may turn logging back on
-- inside one of our 0.1 s gaps, and a forced LoggingCombat(true) on top of
-- that would write a second header and shift the count to the wrong role.
-- With the on-change rule the ticker's write and ours collapse into one
-- header either way, and the end state is ON -- exactly what the ticker
-- expects, so nothing in CombatLogging.lua needs to know we were here.
local function ScheduleToggles(count)
  generation = generation + 1
  local gen = generation
  for i = 1, count do
    local offAt = (i - 1) * TOGGLE_SPACING_S
    C_Timer.After(offAt, function()
      if gen ~= generation then return end
      MA:CombatLogging_SetState(false, false)
    end)
    C_Timer.After(offAt + TOGGLE_OFF_S, function()
      if gen ~= generation then return end
      MA:CombatLogging_SetState(true, false)
    end)
  end
end

-- The press. Bound to the POSTMORTEM_SNAPSHOT key (via the global below)
-- and to /pm snapshot (InfoWindow.lua).
function MA:Snapshot_Mark()
  local now = GetTime()
  if lastPressAt and (now - lastPressAt) < COOLDOWN_S then
    MA:Debug("Snapshot: press ignored -- %.1fs since the last one (cooldown %ds)", now - lastPressAt, COOLDOWN_S)
    return
  end

  -- MA.state.active is the precise "a key is running" answer (see
  -- Bootstrap.lua's IsKeyActive comment); IsKeyActive() covers debug mode
  -- and the brief post-/reload window before Tracker's recovery runs.
  local keyActive = (MA.state and MA.state.active) or self:IsKeyActive()
  if not keyActive then
    Say("snapshot not marked -- no Mythic+ key is active.")
    return
  end
  if not self:CombatLogging_GetCurrentState() then
    Say("snapshot not marked -- combat logging is off (nothing to mark in). Turn on auto logging in /pm options or type /combatlog.")
    return
  end

  lastPressAt = now

  local roleToken = UnitGroupRolesAssigned("player")
  local count = ROLE_TOGGLES[roleToken] or GENERAL_TOGGLES
  local role = ROLE_LABELS[roleToken] or GENERAL_LABEL
  ScheduleToggles(count)

  local db = self:GetDB()
  local zone, level = CurrentZoneAndLevel()
  local marks = db.snapshotMarks
  if type(marks) ~= "table" then
    marks = {}
    db.snapshotMarks = marks
  end
  marks[#marks + 1] = {
    at = time(),
    role = role,
    zone = zone,
    level = level,
    elapsed = MA.state and MA.state.elapsed or nil,
  }
  while #marks > MARKS_CAP do
    table.remove(marks, 1)
  end

  local beforeS = db.snapshotBeforeS or 120
  local afterS = db.snapshotAfterS or 60
  Say(string.format("snapshot marked (%s) -- %s before / %s after", role, FormatMSS(beforeS), FormatMSS(afterS)))
  MA:Debug("Snapshot: %d header toggles scheduled for role %s (%s)", count, role, tostring(roleToken))

  -- Flash the HUD status row. Overlay.lua renders state.statusFlash while
  -- a key is active and GetTime() < expires; Tracker's 1 s tick refreshes
  -- it away afterwards, the extra timer just makes the hide prompt.
  if MA.state then
    MA.state.statusFlash = {
      text = string.format("Snapshot marked (%s)", role),
      expires = now + FLASH_S,
    }
    if MA.Overlay_Refresh then MA:Overlay_Refresh() end
    C_Timer.After(FLASH_S + 0.1, function()
      if MA.Overlay_Refresh then MA:Overlay_Refresh() end
    end)
  end
end

-- What Bindings.xml actually calls. A plain global, since XML bindings
-- can't reach the addon-private MA table.
function Postmortem_SnapshotMark()
  MA:Snapshot_Mark()
end

-- Key lifecycle: forget the cooldown on a fresh start (a press in the last
-- seconds of one key must not block the first seconds of the next), and
-- invalidate any still-pending toggles when a key ends -- see `generation`.
local eventFrame = CreateFrame("Frame")
MA:RegisterKeyEvents(eventFrame, { "CHALLENGE_MODE_START", "CHALLENGE_MODE_COMPLETED", "CHALLENGE_MODE_RESET" })
eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "CHALLENGE_MODE_START" then
    lastPressAt = nil
  else -- CHALLENGE_MODE_COMPLETED or CHALLENGE_MODE_RESET
    generation = generation + 1
  end
end)
