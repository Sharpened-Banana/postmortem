-- Overlay.lua
-- Always-on-top status frame shown only while a Mythic+ key is active: live
-- forces progress, elapsed time, and death count/time lost. Plain Blizzard
-- frame API, not AceGUI -- AceGUI is for config-style dialogs (this addon
-- has none yet), and MDT's own always-visible tool window is built the same
-- way (see MythicDungeonTools/Modules/MainFrame.lua, which is a plain
-- CreateFrame + CreateFontString window, not an AceGUI widget).
--
-- Reads MA.state, which Tracker.lua keeps up to date; this file never calls
-- the WoW live-tracking APIs itself.

local ADDON_NAME, MA = ...

-- Duplicated from Tracker.lua rather than factored into a shared-utility
-- file -- small enough (two lines) that this WP's scope doesn't need a
-- third file for it.
-- verified against EllesmereUIMythicTimer.lua:2760-2769 (real, currently-
-- shipping addon source), cross-referenced against Details_MythicPlus, and
-- against MythicDungeonTools/Core/CombatLogging.lua:67-69 for the
-- instanceType/difficultyID fallback.
local function IsKeyActive()
  if C_ChallengeMode.GetActiveChallengeMapID() then return true end
  local _, instanceType, difficultyID = GetInstanceInfo()
  return instanceType == "party" and difficultyID == 8
end

-- Formats seconds as MM:SS.
-- verified against EllesmereUIMythicTimer.lua:401-417 (FormatTime, real,
-- currently-shipping code) -- same floor(seconds/60) / (seconds % 60) /
-- "%02d:%02d" shape, without the optional-milliseconds branch this addon
-- doesn't need.
local function FormatElapsed(seconds)
  seconds = seconds or 0
  if seconds < 0 then seconds = 0 end
  local whole = math.floor(seconds)
  local m = math.floor(whole / 60)
  local s = whole % 60
  return string.format("%02d:%02d", m, s)
end

local frame

-- Lazily built on first MA:Overlay_Refresh() call (i.e. the first tick
-- after a key becomes active) rather than during ADDON_LOADED/OnInitialize.
-- This keeps Overlay.lua decoupled from Bootstrap.lua's OnInitialize hook
-- (still an empty stub other files may want to extend differently later)
-- and guarantees MA:GetDB() -- needed for the saved position -- is already
-- populated by the time this runs.
local function CreateOverlayFrame()
  -- BackdropTemplate is the current, correct way to get backdrop support on
  -- a plain (non-AceGUI) frame.
  -- verified: real, currently-shipping code in
  -- EllesmereUIMythicTimer/EllesmereUIMythicTimer.lua:1314 (its own
  -- always-on standalone Mythic+ tracker frame -- same shape as this one)
  -- and MythicDungeonTools/Modules/ExternalLinks.lua:155 (a plain,
  -- non-AceGUI frame elsewhere in MDT), confirming BackdropTemplate is used
  -- outside AceGUI's own internals too.
  local f = CreateFrame("Frame", "MythicAnalyzerOverlay", UIParent, "BackdropTemplate")
  -- Grown from the original 92 to fit the two new rows added below
  -- (interrupts, always shown; pull progress, shown only when a route is
  -- loaded -- see MA:Overlay_Refresh()).
  f:SetSize(220, 136)
  f:SetFrameStrata("MEDIUM")
  f:SetClampedToScreen(true)

  -- Backdrop shape/color lifted directly from EllesmereUIMythicTimer's own
  -- standalone frame.
  -- verified: EllesmereUIMythicTimer.lua:1327-1333 (real, currently-
  -- shipping code).
  f:SetBackdrop({
    bgFile = "Interface\\Buttons\\WHITE8x8",
    edgeFile = "Interface\\Buttons\\WHITE8x8",
    edgeSize = 1,
  })
  f:SetBackdropColor(0.05, 0.04, 0.08, 0.85)
  f:SetBackdropBorderColor(0.15, 0.15, 0.15, 0.6)

  -- Draggable, position-persisting overlay. SetMovable/EnableMouse/
  -- RegisterForDrag/StartMoving/StopMovingOrSizing are standard, long-
  -- unchanged Blizzard frame API and don't need addon-specific
  -- verification. The GetPoint()-on-drag-stop persistence pattern below is
  -- verified against MythicDungeonTools/Modules/MainFrame.lua:24-46
  -- (RegisterMainFrameDragHandle, real, currently-shipping code): "local
  -- from, _, to, x, y = frame:GetPoint(); db.anchorFrom = from; ...".
  f:SetMovable(true)
  f:EnableMouse(true)
  f:RegisterForDrag("LeftButton")
  f:SetScript("OnDragStart", f.StartMoving)
  f:SetScript("OnDragStop", function(self)
    self:StopMovingOrSizing()
    local point, _, relativePoint, x, y = self:GetPoint()
    local db = MA:GetDB()
    db.overlayPosition.point = point
    db.overlayPosition.relativePoint = relativePoint
    db.overlayPosition.x = x
    db.overlayPosition.y = y
  end)

  -- GameFontNormal / GameFontHighlight(Small) are standard FrameXML font
  -- templates, used the same way on plain (non-AceGUI) FontStrings in
  -- MythicDungeonTools/Modules/MainFrame.lua:366-452 (e.g.
  -- frame.topPanelString:SetFontObject(GameFontNormalMed3)).
  local forcesFS = f:CreateFontString(nil, "OVERLAY")
  forcesFS:SetFontObject(GameFontNormal)
  forcesFS:SetPoint("TOP", f, "TOP", 0, -14)

  local timerFS = f:CreateFontString(nil, "OVERLAY")
  timerFS:SetFontObject(GameFontHighlight)
  timerFS:SetPoint("TOP", forcesFS, "BOTTOM", 0, -10)

  local deathsFS = f:CreateFontString(nil, "OVERLAY")
  deathsFS:SetFontObject(GameFontHighlightSmall)
  deathsFS:SetPoint("TOP", timerFS, "BOTTOM", 0, -10)

  -- WP-3 additions: interrupt count (always shown once a key is active)
  -- and pull progress (shown only when Interrupts.lua/RouteImport.lua found
  -- an MDT route to compare against -- see MA:Overlay_Refresh() below).
  -- Same FontString-row layout pattern as the three rows above.
  local interruptsFS = f:CreateFontString(nil, "OVERLAY")
  interruptsFS:SetFontObject(GameFontHighlightSmall)
  interruptsFS:SetPoint("TOP", deathsFS, "BOTTOM", 0, -10)

  local pullFS = f:CreateFontString(nil, "OVERLAY")
  pullFS:SetFontObject(GameFontHighlightSmall)
  pullFS:SetPoint("TOP", interruptsFS, "BOTTOM", 0, -10)

  f.forcesFS = forcesFS
  f.timerFS = timerFS
  f.deathsFS = deathsFS
  f.interruptsFS = interruptsFS
  f.pullFS = pullFS

  -- Restore the saved position (defaulted in Bootstrap.lua's
  -- defaults.global.overlayPosition) rather than whatever anchor
  -- CreateFrame left it at.
  local pos = MA:GetDB().overlayPosition
  f:SetPoint(pos.point, UIParent, pos.relativePoint, pos.x, pos.y)

  f:Hide()
  return f
end

-- Reads MA.state (kept up to date by Tracker.lua) and refreshes the
-- overlay's text and visibility. Show/hide is driven by IsKeyActive()
-- directly (not MA.state.active) so the overlay still displays final
-- numbers for the few seconds between CHALLENGE_MODE_COMPLETED and the
-- player actually leaving the dungeon.
function MA:Overlay_Refresh()
  if not frame then
    frame = CreateOverlayFrame()
  end

  if not IsKeyActive() then
    frame:Hide()
    return
  end

  local state = MA.state or {}
  local forces = state.forces or {}
  frame.forcesFS:SetText(string.format(
    "%d / %d (%.1f%%)",
    forces.current or 0,
    forces.total or 0,
    forces.percent or 0
  ))
  frame.timerFS:SetText(FormatElapsed(state.elapsed))

  local deaths = state.deaths or 0
  local timeLost = state.deathTimeLost or 0
  if deaths > 0 and timeLost > 0 then
    frame.deathsFS:SetText(string.format("Deaths: %d  (-%s)", deaths, FormatElapsed(timeLost)))
  else
    frame.deathsFS:SetText(string.format("Deaths: %d", deaths))
  end

  local interrupts = state.interrupts or {}
  frame.interruptsFS:SetText(string.format("Interrupts: %d", interrupts.total or 0))

  -- Only shown when RouteImport.lua actually found an MDT route to compare
  -- against -- no route means nothing to show, not "Pull ? / ?".
  local route = state.route
  if route and route.plannedPulls then
    frame.pullFS:SetText(string.format(
      "Pull %d / %d", route.currentPullIndex or 1, #route.plannedPulls
    ))
    frame.pullFS:Show()
  else
    frame.pullFS:Hide()
  end

  frame:Show()
end
