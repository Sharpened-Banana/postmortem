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
  local f = CreateFrame("Frame", "PostmortemOverlay", UIParent, "BackdropTemplate")
  -- Widened slightly (220 -> 240) for the deaths/interrupts row below,
  -- now side-by-side instead of stacked, and to give the forces bar's
  -- overlaid text comfortable room. Height (190 -> 182) trimmed only
  -- slightly despite that same row-merge removing a whole text row --
  -- kept generous rather than tightly computed, since font metrics
  -- can't be visually confirmed from here; a little extra bottom
  -- padding is a far smaller risk than clipped text.
  f:SetSize(240, 182)
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

  -- Forces progress bar: a real StatusBar instead of plain "X / Y (Z%)"
  -- text -- the single highest-value addition here, since a bar reads
  -- at a glance where a percentage number needs actually reading.
  -- CreateFrame("StatusBar", ...) + SetStatusBarTexture("Interface\\
  -- Buttons\\WHITE8x8") + SetMinMaxValues(0,1)/SetValue(...) is a real,
  -- currently-shipping pattern -- verified this session against
  -- EllesmereUIMythicTimer/EUI_MythicTimer_TargetFocusBars.lua:101-108
  -- (its own per-cast bars use the exact same plain-white fill texture
  -- this addon's own backdrops already use, tinted via
  -- SetStatusBarColor -- EUI_MythicTimer_TargetedSpellBars.lua:376).
  -- Colored with this project's own brand gold (#d7a94c -- the same
  -- accent used throughout the desktop app and public tracker site's
  -- own dark theme, report/html.py's --accent) rather than a fresh
  -- color choice, so the whole product reads as one visual identity.
  local forcesBar = CreateFrame("StatusBar", nil, f)
  forcesBar:SetHeight(20)
  forcesBar:SetPoint("TOPLEFT", f, "TOPLEFT", 14, -14)
  forcesBar:SetPoint("TOPRIGHT", f, "TOPRIGHT", -14, -14)
  forcesBar:SetStatusBarTexture("Interface\\Buttons\\WHITE8x8")
  forcesBar:SetStatusBarColor(0.843, 0.663, 0.298)
  forcesBar:SetMinMaxValues(0, 1)
  forcesBar:SetValue(0)

  local forcesBarBG = forcesBar:CreateTexture(nil, "BACKGROUND")
  forcesBarBG:SetAllPoints(forcesBar)
  forcesBarBG:SetColorTexture(1, 1, 1, 0.12)

  local forcesTextFS = forcesBar:CreateFontString(nil, "OVERLAY")
  forcesTextFS:SetFontObject(GameFontHighlightSmall)
  forcesTextFS:SetPoint("CENTER", forcesBar, "CENTER", 0, 0)

  -- GameFontNormalLarge -- a standard FrameXML font template, real
  -- currently-shipping usage verified this session against
  -- BossHelper/UI/StartPage.lua:34 and ConfirmDialog.lua:69 among
  -- others -- gives the timer real visual weight instead of matching
  -- the same size as every stat row below it.
  local timerFS = f:CreateFontString(nil, "OVERLAY")
  timerFS:SetFontObject(GameFontNormalLarge)
  timerFS:SetPoint("TOP", forcesBar, "BOTTOM", 0, -12)

  -- Deaths and interrupts merged into one row (left/right split within
  -- a shared row frame, same LEFT/RIGHT-anchor-split idiom InfoWindow.lua
  -- already uses for its urlFS/copyButton row) instead of two separate
  -- stacked text rows -- both are single short stats, and putting them
  -- side by side reads as a compact stat line instead of padding out
  -- the frame with two mostly-empty rows.
  local statsRow = CreateFrame("Frame", nil, f)
  statsRow:SetHeight(14)
  statsRow:SetPoint("TOP", timerFS, "BOTTOM", 0, -10)
  statsRow:SetPoint("LEFT", f, "LEFT", 14, 0)
  statsRow:SetPoint("RIGHT", f, "RIGHT", -14, 0)

  local deathsFS = statsRow:CreateFontString(nil, "OVERLAY")
  deathsFS:SetFontObject(GameFontHighlightSmall)
  deathsFS:SetPoint("LEFT", statsRow, "LEFT", 0, 0)
  deathsFS:SetJustifyH("LEFT")

  local interruptsFS = statsRow:CreateFontString(nil, "OVERLAY")
  interruptsFS:SetFontObject(GameFontHighlightSmall)
  interruptsFS:SetPoint("RIGHT", statsRow, "RIGHT", 0, 0)
  interruptsFS:SetJustifyH("RIGHT")

  -- Pull progress (shown only when Interrupts.lua/RouteImport.lua found an
  -- MDT route to compare against -- see MA:Overlay_Refresh() below).
  local pullFS = f:CreateFontString(nil, "OVERLAY")
  pullFS:SetFontObject(GameFontHighlightSmall)
  pullFS:SetPoint("TOP", statsRow, "BOTTOM", 0, -10)

  -- Post-key recap status: shown only during the RECAP_DURATION_S window
  -- Tracker.lua opens after CHALLENGE_MODE_COMPLETED/RESET (see
  -- MA:Overlay_Refresh() below). SetTextColor (not just a font object) is
  -- used here specifically so "log saved" vs "not recorded" are visually
  -- distinct at a glance -- standard, long-unchanged FontString API, not
  -- something requiring addon-specific verification.
  local statusFS = f:CreateFontString(nil, "OVERLAY")
  statusFS:SetFontObject(GameFontHighlightSmall)
  statusFS:SetPoint("TOP", pullFS, "BOTTOM", 0, -10)

  -- Permanent companion-app reminder row: shown alongside statusFS during
  -- the same post-key recap window (see MA:Overlay_Refresh() below), a
  -- distinct blue tint so it doesn't compete with statusFS's green/orange
  -- recorded/not-recorded coloring. Text comes from MA.INFO.recapLine
  -- (Info.lua, WP-1) -- never typed here.
  local companionFS = f:CreateFontString(nil, "OVERLAY")
  companionFS:SetFontObject(GameFontHighlightSmall)
  companionFS:SetPoint("TOP", statusFS, "BOTTOM", 0, -10)
  companionFS:SetJustifyH("CENTER")
  companionFS:SetTextColor(0.55, 0.72, 1.0)

  f.forcesBar = forcesBar
  f.forcesTextFS = forcesTextFS
  f.timerFS = timerFS
  f.deathsFS = deathsFS
  f.interruptsFS = interruptsFS
  f.pullFS = pullFS
  f.statusFS = statusFS
  f.companionFS = companionFS

  -- Restore the saved position (defaulted in Bootstrap.lua's
  -- defaults.global.overlayPosition) rather than whatever anchor
  -- CreateFrame left it at.
  local pos = MA:GetDB().overlayPosition
  f:SetPoint(pos.point, UIParent, pos.relativePoint, pos.x, pos.y)

  f:Hide()
  return f
end

-- Reads MA.state (kept up to date by Tracker.lua) and refreshes the
-- overlay's text and visibility. Shown while a key is active OR during the
-- post-key recap window Tracker.lua opens on CHALLENGE_MODE_COMPLETED/
-- RESET (state.recapUntil, a GetTime() deadline) -- so the overlay keeps
-- showing final numbers for a while after the key ends instead of
-- vanishing the instant IsKeyActive() flips false.
function MA:Overlay_Refresh()
  if not frame then
    frame = CreateOverlayFrame()
  end

  local state = MA.state or {}
  local active = IsKeyActive()
  local inRecap = not active and state.recapUntil and GetTime() < state.recapUntil

  if not active and not inRecap then
    frame:Hide()
    return
  end
  local forces = state.forces or {}
  local pct = forces.percent or 0
  -- Forces can read slightly over 100% (an overpull past the exact
  -- requirement) -- clamped here since StatusBar values outside
  -- SetMinMaxValues' range aren't guaranteed to render sensibly, even
  -- though the *text* below still shows the real, unclamped percent.
  frame.forcesBar:SetValue(math.min(1, math.max(0, pct / 100)))
  frame.forcesTextFS:SetText(string.format(
    "%d / %d (%.1f%%)",
    forces.current or 0,
    forces.total or 0,
    pct
  ))
  frame.timerFS:SetText(FormatElapsed(state.elapsed))

  local deaths = state.deaths or 0
  local timeLost = state.deathTimeLost or 0
  if deaths > 0 and timeLost > 0 then
    frame.deathsFS:SetText(string.format("Deaths: %d  (-%s)", deaths, FormatElapsed(timeLost)))
  else
    frame.deathsFS:SetText(string.format("Deaths: %d", deaths))
  end
  -- A death is worth drawing the eye to; no deaths stays the same
  -- neutral highlight color the rest of the stat rows use.
  if deaths > 0 then
    frame.deathsFS:SetTextColor(1.0, 0.5, 0.5)
  else
    frame.deathsFS:SetTextColor(1.0, 1.0, 1.0)
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

  -- Post-key recap status: only meaningful once the key has actually
  -- ended. combatLogWasOn is the REAL LoggingCombat() state captured by
  -- CombatLogging.lua right before it (maybe) turns logging off -- see its
  -- comment for why this reports the true recording outcome rather than
  -- just "did our addon try to enable it". Deliberately says "ready to
  -- analyze", not "analyzed": this addon has no way to know whether
  -- postmortem's own record/analyze step actually ran on this log --
  -- that happens in a separate process this addon can't observe.
  if inRecap then
    if state.combatLogWasOn then
      frame.statusFS:SetTextColor(0.4, 0.9, 0.5)
      frame.statusFS:SetText("Log saved -- ready to analyze")
    else
      frame.statusFS:SetTextColor(1.0, 0.65, 0.2)
      frame.statusFS:SetText("Not recorded -- combat log was off")
    end
    frame.statusFS:Show()

    -- Permanent companion-app reminder, shown alongside statusFS. Guarded
    -- on MA.INFO/recapLine so a missing/failed Info.lua load just hides
    -- this row instead of erroring.
    if MA.INFO and MA.INFO.recapLine then
      frame.companionFS:SetText(MA.INFO.recapLine)
      frame.companionFS:Show()
    else
      frame.companionFS:Hide()
    end
  else
    frame.statusFS:Hide()
    frame.companionFS:Hide()
  end

  frame:Show()
end
