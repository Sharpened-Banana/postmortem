-- Overlay.lua
-- Always-on-top status frame shown only while a Mythic+ key is active: live
-- forces progress, elapsed time, deaths/interrupts, chest timer, boss
-- splits, pull progress, and a post-key recap. Plain Blizzard frame API,
-- not AceGUI -- AceGUI is for config-style dialogs (Options.lua uses the
-- native Settings API instead), and MDT's own always-visible tool window is
-- built the same way (MythicDungeonTools/Modules/MainFrame.lua, a plain
-- CreateFrame + CreateFontString window).
--
-- Reads MA.state, which Tracker.lua/ChestTimer.lua/Interrupts.lua/
-- RouteImport.lua/DeathTagging.lua keep up to date; this file never calls
-- the WoW live-tracking APIs itself.
--
-- Rows below the fixed forces-bar/timer/stats header are individually
-- toggleable (Options.lua) and each optional row is reflowed dynamically:
-- ReflowRows() below walks an ordered list, anchoring each visible row's
-- TOP to the previous VISIBLE row's BOTTOM (skipping hidden ones entirely,
-- not leaving a gap), and returns the total content height so the frame
-- can size itself -- same "measure the real rendered content" idea
-- InfoWindow.lua/Results.lua already use for their own dynamic height, just
-- extended to also handle rows disappearing from the middle of the stack.

local ADDON_NAME, MA = ...

-- Formats seconds as MM:SS.
-- verified against EllesmereUIMythicTimer.lua:401-417 (FormatTime, real,
-- currently-shipping code) -- same floor(seconds/60) / (seconds % 60) /
-- "%02d:%02d" shape, without the optional-milliseconds branch this addon
-- doesn't need.
-- MM:SS, or -MM:SS once past a deadline. ChestTimer.lua has the identical
-- formatter for its own +2/+3 countdowns and publishes it as
-- MA.ChestTimer_FormatSigned; this file prefers that when it's loaded and
-- keeps its own copy so the overall countdown still renders if the chest
-- timer module is ever absent (the two must agree -- they sit on adjacent
-- rows showing the same kind of number).
local function FormatSigned(seconds)
  if MA.ChestTimer_FormatSigned then return MA.ChestTimer_FormatSigned(seconds) end
  seconds = seconds or 0
  local sign = seconds < 0 and "-" or ""
  local whole = math.floor(math.abs(seconds))
  return string.format("%s%02d:%02d", sign, math.floor(whole / 60), whole % 60)
end

local function FormatElapsed(seconds)
  seconds = seconds or 0
  if seconds < 0 then seconds = 0 end
  local whole = math.floor(seconds)
  local m = math.floor(whole / 60)
  local s = whole % 60
  return string.format("%02d:%02d", m, s)
end

-- The 14px inset used for every row's left/right anchor and for the
-- frame's top/bottom padding -- was repeated as a literal at eight call
-- sites before the rows became reflow participants.
local PADDING = 14

local frame

-- Anchors each visible row's TOP to the previous visible row's BOTTOM
-- (skipping hidden rows so no gap is left behind), starting from
-- `topAnchorFrame` for the first visible row -- which therefore also gets
-- its own `gap` applied against topAnchorFrame, exactly like every other
-- row in the chain. `rows` is an ordered array of
-- { widget, gap, visible, fixedHeight?, wide? }; fixedHeight is given for
-- non-FontString rows (e.g. statsRow, a plain Frame) where
-- GetStringHeight() doesn't apply. `wide` re-applies the LEFT/RIGHT
-- anchors a full-width row (the forces bar, the deaths/interrupts row)
-- needs -- ClearAllPoints() below drops every anchor, so a row that spans
-- the frame has to have its sides put back, not just its top. Returns the
-- total content height consumed (sum of every visible row's own gap +
-- height), for the caller to size the frame with.
local function ReflowRows(rows, topAnchorFrame)
  local prevWidget = topAnchorFrame
  local height = 0
  for _, row in ipairs(rows) do
    local w = row.widget
    if row.visible then
      w:ClearAllPoints()
      w:SetPoint("TOP", prevWidget, "BOTTOM", 0, -row.gap)
      if row.wide then
        local parent = w:GetParent()
        w:SetPoint("LEFT", parent, "LEFT", PADDING, 0)
        w:SetPoint("RIGHT", parent, "RIGHT", -PADDING, 0)
      end
      w:Show()
      height = height + row.gap + (row.fixedHeight or w:GetStringHeight())
      prevWidget = w
    else
      w:Hide()
    end
  end
  return height
end

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
  -- Width fixed; height is now computed every refresh from whichever rows
  -- are actually shown (see MA:Overlay_Refresh() below) -- SetSize here is
  -- just a safe starting point before the first refresh runs.
  f:SetSize(240, 100)
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
  -- Always shown -- not one of Options.lua's toggleable rows.
  -- Zero-height anchor pinned to the top inset, so EVERY row below --
  -- including the timer and the forces bar -- can be an ordinary
  -- ReflowRows participant instead of carrying its own fixed anchor. The
  -- first visible row's own `gap` then supplies the top padding, the same
  -- way each later row supplies its own.
  local topAnchor = CreateFrame("Frame", nil, f)
  topAnchor:SetHeight(0)
  topAnchor:SetPoint("TOPLEFT", f, "TOPLEFT", PADDING, 0)
  topAnchor:SetPoint("TOPRIGHT", f, "TOPRIGHT", -PADDING, 0)

  local forcesBar = CreateFrame("StatusBar", nil, f)
  forcesBar:SetHeight(20)
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
  -- the same size as every stat row below it. Always shown.
  -- The key's own clock, and the top row of the overlay: the large,
  -- centered figure is the overall dungeon time -- counting DOWN against
  -- the key's time limit, which is the number that decides whether the run
  -- counts, and the one Blizzard's own UI puts front and centre. Elapsed
  -- (what this row used to show on its own) stays as a small readout on
  -- the left rather than being dropped, since it's the figure the rest of
  -- the report is written in terms of.
  local timerRow = CreateFrame("Frame", nil, f)
  timerRow:SetHeight(18)

  local timerFS = timerRow:CreateFontString(nil, "OVERLAY")
  timerFS:SetFontObject(GameFontNormalLarge)
  timerFS:SetPoint("CENTER", timerRow, "CENTER", 0, 0)

  local elapsedFS = timerRow:CreateFontString(nil, "OVERLAY")
  elapsedFS:SetFontObject(GameFontHighlightSmall)
  elapsedFS:SetPoint("LEFT", timerRow, "LEFT", 0, 0)
  elapsedFS:SetJustifyH("LEFT")
  elapsedFS:SetTextColor(0.7, 0.7, 0.72)

  -- Deaths and interrupts merged into one row (left/right split within
  -- a shared row frame, same LEFT/RIGHT-anchor-split idiom InfoWindow.lua
  -- already uses for its urlFS/copyButton row) instead of two separate
  -- stacked text rows -- both are single short stats, and putting them
  -- side by side reads as a compact stat line instead of padding out
  -- the frame with two mostly-empty rows. The row itself is always shown
  -- (deaths isn't toggleable); interruptsFS within it is individually
  -- hidden by db.showInterrupts (see MA:Overlay_Refresh() below).
  local statsRow = CreateFrame("Frame", nil, f)
  statsRow:SetHeight(14)

  local deathsFS = statsRow:CreateFontString(nil, "OVERLAY")
  deathsFS:SetFontObject(GameFontHighlightSmall)
  deathsFS:SetPoint("LEFT", statsRow, "LEFT", 0, 0)
  deathsFS:SetJustifyH("LEFT")

  local interruptsFS = statsRow:CreateFontString(nil, "OVERLAY")
  interruptsFS:SetFontObject(GameFontHighlightSmall)
  interruptsFS:SetPoint("RIGHT", statsRow, "RIGHT", 0, 0)
  interruptsFS:SetJustifyH("RIGHT")

  -- Below this point, every row is a ReflowRows() participant -- created
  -- with no TOP anchor of its own; MA:Overlay_Refresh() anchors and shows
  -- (or hides) each one every call, since which rows are present changes
  -- with both Options.lua's toggles and live state (a route loaded or not,
  -- a split completed or not, the post-key recap window open or not).

  -- Chest timer row: "+2 MM:SS   +3 MM:SS", ChestTimer.lua's countdowns.
  local chestTimerFS = f:CreateFontString(nil, "OVERLAY")
  chestTimerFS:SetFontObject(GameFontHighlightSmall)

  -- Most recently completed boss/objective split, with its personal-best
  -- delta -- ChestTimer.lua's rising-edge split tracking.
  local splitFS = f:CreateFontString(nil, "OVERLAY")
  splitFS:SetFontObject(GameFontHighlightSmall)

  -- Pull progress -- RouteImport.lua's MDT-route comparison.
  local pullFS = f:CreateFontString(nil, "OVERLAY")
  pullFS:SetFontObject(GameFontHighlightSmall)

  -- Post-key recap status: shown only during the RECAP_DURATION_S window
  -- Tracker.lua opens after CHALLENGE_MODE_COMPLETED/RESET (see
  -- MA:Overlay_Refresh() below). SetTextColor (not just a font object) is
  -- used here specifically so "log saved" vs "not recorded" are visually
  -- distinct at a glance -- standard, long-unchanged FontString API, not
  -- something requiring addon-specific verification.
  local statusFS = f:CreateFontString(nil, "OVERLAY")
  statusFS:SetFontObject(GameFontHighlightSmall)

  -- Most recent death's cause, DeathTagging.lua's precise (not heuristic)
  -- C_DeathRecap-based attribution -- shown only during the same post-key
  -- recap window, only when a cause was actually resolved.
  local deathCauseFS = f:CreateFontString(nil, "OVERLAY")
  deathCauseFS:SetFontObject(GameFontHighlightSmall)

  -- Permanent companion-app reminder row: shown alongside statusFS during
  -- the same post-key recap window, a distinct blue tint so it doesn't
  -- compete with statusFS's green/orange recorded/not-recorded coloring.
  -- Text comes from MA.INFO.recapLine (Info.lua) -- never typed here.
  local companionFS = f:CreateFontString(nil, "OVERLAY")
  companionFS:SetFontObject(GameFontHighlightSmall)
  companionFS:SetJustifyH("CENTER")
  companionFS:SetTextColor(0.55, 0.72, 1.0)

  f.topAnchor = topAnchor
  f.forcesBar = forcesBar
  f.forcesTextFS = forcesTextFS
  f.timerRow = timerRow
  f.timerFS = timerFS
  f.elapsedFS = elapsedFS
  f.statsRow = statsRow
  f.deathsFS = deathsFS
  f.interruptsFS = interruptsFS
  f.chestTimerFS = chestTimerFS
  f.splitFS = splitFS
  f.pullFS = pullFS
  f.statusFS = statusFS
  f.deathCauseFS = deathCauseFS
  f.companionFS = companionFS

  -- Restore the saved position (defaulted in Bootstrap.lua's
  -- defaults.global.overlayPosition) rather than whatever anchor
  -- CreateFrame left it at.
  local pos = MA:GetDB().overlayPosition
  f:SetPoint(pos.point, UIParent, pos.relativePoint, pos.x, pos.y)

  f:Hide()
  return f
end

-- Reads MA.state (kept up to date by Tracker.lua and friends) and refreshes
-- the overlay's text, per-row visibility, and overall size. Shown while a
-- key is active OR during the post-key recap window Tracker.lua opens on
-- CHALLENGE_MODE_COMPLETED/RESET (state.recapUntil, a GetTime() deadline)
-- -- so the overlay keeps showing final numbers for a while after the key
-- ends instead of vanishing the instant IsKeyActive() flips false.
function MA:Overlay_Refresh()
  if not frame then
    frame = CreateOverlayFrame()
  end

  local db = MA:GetDB()
  local state = MA.state or {}
  -- MA.state.active, not MA:IsKeyActive() -- see that function's own
  -- comment (real bug, 2026-09-04): calling the broad Blizzard-API check
  -- here meant the overlay stayed visible with frozen numbers for as long
  -- as the group remained in the dungeon after the key ended, since "in a
  -- Mythic+ instance" and "a key is running" are not the same thing.
  -- state.active is set precisely by CHALLENGE_MODE_START/COMPLETED/RESET
  -- (and by debug mode), so it flips the instant the key actually ends.
  local active = state.active
  local inRecap = not active and state.recapUntil and GetTime() < state.recapUntil

  if not active and not inRecap then
    frame:Hide()
    return
  end
  if not frame:IsShown() then
    MA:Debug("Overlay: showing (%s)", inRecap and "post-key recap" or "key active")
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
  -- Overall dungeon time: counts DOWN against the key's own limit
  -- (ChestTimer.lua resolves it from C_ChallengeMode.GetMapUIInfo and
  -- stores it alongside the +2/+3 thresholds), going negative and red once
  -- the key is blown. With no limit resolved yet -- the first tick of a
  -- key, or debug mode with no real keystone -- there is no countdown to
  -- show, so the row falls back to the elapsed time it used to show, and
  -- the small left-hand readout hides rather than printing it twice.
  local elapsed = state.elapsed or 0
  local chestTimer = state.chestTimer
  local timeLimit = chestTimer and chestTimer.timeLimit
  if timeLimit and timeLimit > 0 then
    local remaining = timeLimit - elapsed
    frame.timerFS:SetText(FormatSigned(remaining))
    if remaining < 0 then
      frame.timerFS:SetTextColor(1.0, 0.3, 0.3)
    elseif remaining < 120 then
      frame.timerFS:SetTextColor(1.0, 0.65, 0.2)
    else
      frame.timerFS:SetTextColor(1.0, 1.0, 1.0)
    end
    frame.elapsedFS:SetText(FormatElapsed(elapsed))
    frame.elapsedFS:Show()
  else
    frame.timerFS:SetText(FormatElapsed(elapsed))
    frame.timerFS:SetTextColor(1.0, 1.0, 1.0)
    frame.elapsedFS:Hide()
  end

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
  if db.showInterrupts then
    frame.interruptsFS:SetText(string.format("Interrupts: %d", interrupts.total or 0))
    frame.interruptsFS:Show()
  else
    frame.interruptsFS:Hide()
  end

  -- Chest timer row content -- visibility itself (row.visible below) also
  -- requires db.showChestTimer.
  local showChestTimer = db.showChestTimer and chestTimer and chestTimer.timeLimit and MA.ChestTimer_FormatSigned
  if showChestTimer then
    local t2Remaining = chestTimer.t2 - elapsed
    local t3Remaining = chestTimer.t3 - elapsed
    frame.chestTimerFS:SetText(string.format(
      "+2 %s   +3 %s",
      MA.ChestTimer_FormatSigned(t2Remaining),
      MA.ChestTimer_FormatSigned(t3Remaining)
    ))
    -- Gold once +3 is still reachable, silver once only +2 is, default
    -- white while comfortably under +2, red once overtime -- the same
    -- verdict tiers AngryKeystones' completion message uses.
    if t3Remaining >= 0 then
      frame.chestTimerFS:SetTextColor(1.0, 0.84, 0.0)
    elseif t2Remaining >= 0 then
      frame.chestTimerFS:SetTextColor(0.78, 0.78, 0.81)
    elseif (timeLimit - elapsed) >= 0 then
      frame.chestTimerFS:SetTextColor(1.0, 1.0, 1.0)
    else
      frame.chestTimerFS:SetTextColor(1.0, 0.3, 0.3)
    end
  end

  -- Most recently completed split, with its personal-best delta if one
  -- exists. Content only needs building when the row will actually show.
  local splits = state.splits
  local showSplit = db.showBossSplits and splits and #splits > 0
  if showSplit then
    local latest = splits[#splits]
    if latest.delta then
      local sign = latest.delta <= 0 and "-" or "+"
      frame.splitFS:SetText(string.format(
        "%s   %s%s", latest.name, sign, MA.ChestTimer_FormatSigned and MA.ChestTimer_FormatSigned(math.abs(latest.delta)) or ""
      ))
      frame.splitFS:SetTextColor(latest.delta <= 0 and 0.4 or 1.0, latest.delta <= 0 and 0.9 or 0.65, latest.delta <= 0 and 0.5 or 0.2)
    else
      frame.splitFS:SetText(string.format("%s   (new best)", latest.name))
      frame.splitFS:SetTextColor(1.0, 1.0, 1.0)
    end
  end

  -- Only shown when RouteImport.lua actually found an MDT route to compare
  -- against -- no route means nothing to show, not "Pull ? / ?".
  local route = state.route
  local showPull = db.showPullProgress and route and route.plannedPulls
  if showPull then
    frame.pullFS:SetText(string.format(
      "Pull %d / %d", route.currentPullIndex or 1, #route.plannedPulls
    ))
  end

  -- Post-key recap status: only meaningful once the key has actually
  -- ended. combatLogWasOn is the REAL LoggingCombat() state captured by
  -- CombatLogging.lua right before it (maybe) turns logging off -- see its
  -- comment for why this reports the true recording outcome rather than
  -- just "did our addon try to enable it". Deliberately says "ready to
  -- analyze", not "analyzed": this addon has no way to know whether
  -- postmortem's own record/analyze step actually ran on this log --
  -- that happens in a separate process this addon can't observe.
  local showStatus, showDeathCause, showCompanion = false, false, false
  if inRecap then
    showStatus = true
    if state.combatLogWasOn then
      frame.statusFS:SetTextColor(0.4, 0.9, 0.5)
      frame.statusFS:SetText("Log saved -- ready to analyze")
    else
      frame.statusFS:SetTextColor(1.0, 0.65, 0.2)
      frame.statusFS:SetText("Not recorded -- combat log was off")
    end

    local cause = db.tagDeaths and state.lastDeathCause
    if cause then
      showDeathCause = true
      frame.deathCauseFS:SetText(string.format(
        "Died to %s%s", cause.name, cause.avoidable and " (avoidable)" or ""
      ))
      if cause.avoidable then
        frame.deathCauseFS:SetTextColor(1.0, 0.65, 0.2)
      else
        frame.deathCauseFS:SetTextColor(0.85, 0.85, 0.85)
      end
    end

    if MA.INFO and MA.INFO.recapLine then
      showCompanion = true
      frame.companionFS:SetText(MA.INFO.recapLine)
    end
  end

  -- Reflow every optional row in display order, skipping hidden ones
  -- entirely (no gap left behind), and size the frame to exactly what's
  -- shown -- extending InfoWindow.lua/Results.lua's existing "measure the
  -- real rendered content" dynamic-height idiom to also handle rows
  -- disappearing from the middle of the stack, not just the bottom.
  -- Row order is the reading order the numbers are actually used in:
  -- how long is left overall, then which chest that still buys, then how
  -- much of the dungeon is done, then the run's cost so far.
  local timerHeight = frame.timerFS:GetStringHeight()
  frame.timerRow:SetHeight(timerHeight)

  local contentHeight = ReflowRows({
    { widget = frame.timerRow, gap = PADDING, visible = true, fixedHeight = timerHeight, wide = true },
    { widget = frame.chestTimerFS, gap = 8, visible = showChestTimer },
    { widget = frame.forcesBar, gap = 10, visible = true, fixedHeight = 20, wide = true },
    { widget = frame.statsRow, gap = 10, visible = true, fixedHeight = 14, wide = true },
    { widget = frame.splitFS, gap = 6, visible = showSplit },
    { widget = frame.pullFS, gap = 10, visible = showPull },
    { widget = frame.statusFS, gap = 10, visible = showStatus },
    { widget = frame.deathCauseFS, gap = 6, visible = showDeathCause },
    { widget = frame.companionFS, gap = 10, visible = showCompanion },
  }, frame.topAnchor)

  frame:SetHeight(contentHeight + PADDING) -- + bottom padding, matching the side inset used throughout
  frame:Show()
end
