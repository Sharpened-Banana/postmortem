-- InfoWindow.lua
-- The full "what's live vs. what needs the companion app" window: renders
-- MA.INFO (Info.lua, WP-1) as a draggable, ESC-closable frame, plus the
-- /pm slash command that opens it. MinimapButton.lua's left-click already
-- calls MA.Info_Toggle(MA) through a guarded fallback -- this file is what
-- makes that call actually do something instead of falling back to the
-- copy-link popup.
--
-- Visual style (backdrop shape/color, drag/position-persistence idiom,
-- lazy-build-on-first-use timing) is copied verbatim from Overlay.lua
-- rather than reinvented -- this is a companion window to that overlay, not
-- a separate product, and should look like the same addon built it.
--
-- UIPanelCloseButtonNoScripts + a manually-assigned OnClick (rather than
-- plain UIPanelCloseButton, which wires its own OnClick) is a real,
-- currently-shipping pattern -- verified this session against
-- RaiderIO/core.lua:12818-12820 ("Frame.close = CreateFrame("Button", nil,
-- Frame, "UIPanelCloseButtonNoScripts"); ... Frame.close:SetScript("OnClick",
-- function() search:Hide() end)").
--
-- tinsert(UISpecialFrames, <global frame name>) is the standard, real
-- mechanism for ESC-to-close on a plain (non-StaticPopup) frame -- verified
-- against MythicDungeonTools/Modules/MainFrame.lua:1005
-- ("tinsert(UISpecialFrames, "MDTFrame")") and
-- MythicDungeonTools/Modules/ErrorHandling.lua:77. It only works because the
-- frame is given an explicit global name in CreateFrame's second argument
-- ("PostmortemInfoFrame" below) -- UISpecialFrames stores frame names,
-- not frame references.
--
-- Computing the frame's height by summing each FontString's GetStringHeight()
-- (called after that FontString's final SetText + width-defining anchors)
-- plus fixed gaps, rather than a guessed constant, is the same idiom used
-- by BossHelper/UI/Info.lua's own info-card layout (its `add()` helper does
-- `cy = cy - (fs:GetStringHeight() or 16) - <gap>` after every SetText) --
-- verified this session, real currently-shipping code.

local ADDON_NAME, MA = ...

-- Layout constants shared between the FontString/button anchors below and
-- the height-computation block at the end, so the two can never drift apart.
local TITLE_BAR_HEIGHT = 40
local BUTTON_HEIGHT = 22

-- Lazily built on first MA:Info_Toggle() call, not during ADDON_LOADED --
-- same rationale as Overlay.lua's own CreateOverlayFrame(): guarantees
-- MA:GetDB() is already populated by the time this frame needs to read/
-- write db.infoWindowPosition.
local function CreateInfoFrame()
  -- BackdropTemplate, backdrop shape, and backdrop colors are copied
  -- verbatim from Overlay.lua's CreateOverlayFrame() -- same established
  -- visual identity, see that file's own citations for their origin.
  local f = CreateFrame("Frame", "PostmortemInfoFrame", UIParent, "BackdropTemplate")
  f:SetSize(460, 1) -- height set below, computed from actual text content
  f:SetFrameStrata("DIALOG") -- above the overlay's MEDIUM strata
  f:SetClampedToScreen(true)
  f:SetBackdrop({
    bgFile = "Interface\\Buttons\\WHITE8x8",
    edgeFile = "Interface\\Buttons\\WHITE8x8",
    edgeSize = 1,
  })
  f:SetBackdropColor(0.05, 0.04, 0.08, 0.85)
  f:SetBackdropBorderColor(0.15, 0.15, 0.15, 0.6)

  -- Draggable, position-persisting window -- identical pattern to
  -- Overlay.lua's own SetMovable/EnableMouse/RegisterForDrag/OnDragStop,
  -- writing into db.infoWindowPosition instead of db.overlayPosition.
  f:SetMovable(true)
  f:EnableMouse(true)
  f:RegisterForDrag("LeftButton")
  f:SetScript("OnDragStart", f.StartMoving)
  f:SetScript("OnDragStop", function(self)
    self:StopMovingOrSizing()
    local point, _, relativePoint, x, y = self:GetPoint()
    local db = MA:GetDB()
    db.infoWindowPosition.point = point
    db.infoWindowPosition.relativePoint = relativePoint
    db.infoWindowPosition.x = x
    db.infoWindowPosition.y = y
  end)

  -- Title bar: headline text + close button.
  local titleBar = CreateFrame("Frame", nil, f)
  titleBar:SetHeight(TITLE_BAR_HEIGHT)
  titleBar:SetPoint("TOPLEFT", f, "TOPLEFT", 0, 0)
  titleBar:SetPoint("TOPRIGHT", f, "TOPRIGHT", 0, 0)

  local titleFS = titleBar:CreateFontString(nil, "OVERLAY")
  titleFS:SetFontObject(GameFontNormal)
  titleFS:SetPoint("LEFT", titleBar, "LEFT", 16, 0)
  titleFS:SetPoint("RIGHT", titleBar, "RIGHT", -32, 0) -- leave room for the close button
  titleFS:SetJustifyH("LEFT")
  titleFS:SetText(MA.INFO.headline)

  local closeBtn = CreateFrame("Button", nil, titleBar, "UIPanelCloseButtonNoScripts")
  closeBtn:SetPoint("TOPRIGHT", titleBar, "TOPRIGHT", -2, -2)
  closeBtn:RegisterForClicks("LeftButtonUp")
  closeBtn:SetScript("OnClick", function() f:Hide() end)

  -- Body: MA.INFO rendered as two title+body sections (live / companion
  -- app) plus a footer -- each body is ONE multi-line FontString built via
  -- table.concat(..., "\n") on MA.INFO's line arrays, not one FontString
  -- per bullet, so MA.INFO stays the only place this text is ever written.
  local subheadFS = f:CreateFontString(nil, "OVERLAY")
  subheadFS:SetFontObject(GameFontHighlightSmall)
  subheadFS:SetPoint("TOPLEFT", titleBar, "BOTTOMLEFT", 16, -12)
  subheadFS:SetPoint("RIGHT", f, "RIGHT", -16, 0)
  subheadFS:SetJustifyH("LEFT")
  subheadFS:SetText(MA.INFO.subhead)

  local liveTitleFS = f:CreateFontString(nil, "OVERLAY")
  liveTitleFS:SetFontObject(GameFontNormal)
  liveTitleFS:SetPoint("TOPLEFT", subheadFS, "BOTTOMLEFT", 0, -14)
  liveTitleFS:SetPoint("RIGHT", f, "RIGHT", -16, 0)
  liveTitleFS:SetJustifyH("LEFT")
  liveTitleFS:SetText(MA.INFO.liveTitle)

  local liveBodyFS = f:CreateFontString(nil, "OVERLAY")
  liveBodyFS:SetFontObject(GameFontHighlightSmall)
  liveBodyFS:SetPoint("TOPLEFT", liveTitleFS, "BOTTOMLEFT", 0, -6)
  liveBodyFS:SetPoint("RIGHT", f, "RIGHT", -16, 0)
  liveBodyFS:SetJustifyH("LEFT")
  liveBodyFS:SetJustifyV("TOP")
  liveBodyFS:SetSpacing(3)
  liveBodyFS:SetText(table.concat(MA.INFO.liveLines, "\n"))

  local appTitleFS = f:CreateFontString(nil, "OVERLAY")
  appTitleFS:SetFontObject(GameFontNormal)
  appTitleFS:SetPoint("TOPLEFT", liveBodyFS, "BOTTOMLEFT", 0, -14)
  appTitleFS:SetPoint("RIGHT", f, "RIGHT", -16, 0)
  appTitleFS:SetJustifyH("LEFT")
  appTitleFS:SetText(MA.INFO.appTitle)

  local appBodyFS = f:CreateFontString(nil, "OVERLAY")
  appBodyFS:SetFontObject(GameFontHighlightSmall)
  appBodyFS:SetPoint("TOPLEFT", appTitleFS, "BOTTOMLEFT", 0, -6)
  appBodyFS:SetPoint("RIGHT", f, "RIGHT", -16, 0)
  appBodyFS:SetJustifyH("LEFT")
  appBodyFS:SetJustifyV("TOP")
  appBodyFS:SetSpacing(3)
  appBodyFS:SetText(table.concat(MA.INFO.appLines, "\n"))

  local footerFS = f:CreateFontString(nil, "OVERLAY")
  footerFS:SetFontObject(GameFontDisableSmall)
  footerFS:SetPoint("TOPLEFT", appBodyFS, "BOTTOMLEFT", 0, -14)
  footerFS:SetPoint("RIGHT", f, "RIGHT", -16, 0)
  footerFS:SetJustifyH("LEFT")
  footerFS:SetJustifyV("TOP")
  footerFS:SetText(MA.INFO.footer)

  -- Footer row: "Copy download link" / "Close" buttons, plus the raw URL
  -- as plain (non-clickable) text -- the copy-link button, same as
  -- Info.lua's StaticPopup and the minimap button's right-click, is how
  -- the URL actually gets copied; addons can't open a browser.
  local buttonRow = CreateFrame("Frame", nil, f)
  buttonRow:SetHeight(BUTTON_HEIGHT)
  buttonRow:SetPoint("TOPLEFT", footerFS, "BOTTOMLEFT", 0, -20)
  buttonRow:SetPoint("RIGHT", f, "RIGHT", -16, 0)

  local closeButton = CreateFrame("Button", nil, buttonRow, "UIPanelButtonTemplate")
  closeButton:SetSize(80, BUTTON_HEIGHT)
  closeButton:SetText(CLOSE)
  closeButton:SetPoint("RIGHT", buttonRow, "RIGHT", 0, 0)
  closeButton:SetScript("OnClick", function() f:Hide() end)

  local copyButton = CreateFrame("Button", nil, buttonRow, "UIPanelButtonTemplate")
  copyButton:SetSize(150, BUTTON_HEIGHT)
  copyButton:SetText("Copy download link")
  copyButton:SetPoint("RIGHT", closeButton, "LEFT", -8, 0)
  copyButton:SetScript("OnClick", function() MA:Info_ShowLinkPopup() end)

  local urlFS = buttonRow:CreateFontString(nil, "OVERLAY")
  urlFS:SetFontObject(GameFontDisableSmall)
  urlFS:SetPoint("LEFT", buttonRow, "LEFT", 0, 0)
  urlFS:SetPoint("RIGHT", copyButton, "LEFT", -12, 0)
  urlFS:SetJustifyH("LEFT")
  urlFS:SetText(MA.INFO.url)

  -- Height computed from the actual rendered content, not a guessed
  -- constant: every FontString above already has its final SetText AND its
  -- width-defining anchors (TOPLEFT+RIGHT) at this point, so
  -- GetStringHeight() reflects real wrapped height rather than a stale/zero
  -- value. The gap constants below (12/14/6/14/6/14/20) are the exact same
  -- numbers used in the anchor offsets above, so the frame's bottom edge
  -- lands precisely at the button row's bottom -- no leftover gap, nothing
  -- clipped.
  local totalHeight = TITLE_BAR_HEIGHT -- title bar
    + 12 + subheadFS:GetStringHeight()
    + 14 + liveTitleFS:GetStringHeight()
    + 6 + liveBodyFS:GetStringHeight()
    + 14 + appTitleFS:GetStringHeight()
    + 6 + appBodyFS:GetStringHeight()
    + 14 + footerFS:GetStringHeight()
    + 20 + BUTTON_HEIGHT + 10 -- button row + bottom padding
  f:SetHeight(totalHeight)

  -- Restore the saved position (defaulted in Bootstrap.lua's
  -- defaults.global.infoWindowPosition) rather than whatever anchor
  -- CreateFrame left it at -- same pattern as Overlay.lua's own position
  -- restore.
  local pos = MA:GetDB().infoWindowPosition
  f:SetPoint(pos.point, UIParent, pos.relativePoint, pos.x, pos.y)

  -- ESC-to-close. Only works because the frame was given the explicit
  -- global name "PostmortemInfoFrame" above.
  tinsert(UISpecialFrames, "PostmortemInfoFrame")

  f:Hide()
  return f
end

-- Public toggle, called by MinimapButton.lua's left-click handler and by
-- the /pm slash command below. Checks _G.PostmortemInfoFrame first so
-- repeated calls don't rebuild the frame every time.
function MA:Info_Toggle()
  local f = _G.PostmortemInfoFrame or CreateInfoFrame()
  f:SetShown(not f:IsShown())
end

-- /pm and /postmortem. WoW's Lua 5.1 has no string.trim -- the gsub
-- pair below is the correct, always-available way to trim whitespace.
SLASH_POSTMORTEM1 = "/pm"
SLASH_POSTMORTEM2 = "/postmortem"
SlashCmdList["POSTMORTEM"] = function(msg)
  msg = (msg or ""):lower()
  msg = msg:gsub("^%s+", ""):gsub("%s+$", "") -- trim
  if msg == "" then
    MA:Info_Toggle()
  elseif msg == "results" or msg == "stats" then
    if MA.Results_Show then MA:Results_Show() end
  elseif msg == "link" or msg == "url" then
    MA:Info_ShowLinkPopup()
  elseif msg == "minimap" then
    if MA.MinimapButton_Toggle then MA.MinimapButton_Toggle(MA) end
  else
    print("|cffd7a94cPostmortem|r: /pm (info window), /pm results (in-game run stats), "
      .. "/pm link (copy download link), /pm minimap (toggle minimap icon)")
  end
end
