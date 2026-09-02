-- Results.lua
-- Reads the crunched post-run stats the desktop companion app writes into
-- this addon's folder (PostmortemResults.lua, a plain global table -- see
-- the Python side's addon_results.py, and RaiderIO's own externally-
-- generated db/*.lua files for the same pattern) and shows them in-game.
--
-- WoW only re-reads that data file on /reload, so the flow is: finish a key
-- (desktop app analyzes and writes the file) -> /reload -> /pm results. The
-- global is read fresh on every Results_Show so a /reload always surfaces
-- the latest.
--
-- Window build (backdrop shape/colors, drag+position persistence, ESC-close
-- via UISpecialFrames, UIPanelCloseButtonNoScripts) is the same idiom as
-- InfoWindow.lua -- this is a sibling window to that one and should look
-- like the same addon built it. See InfoWindow.lua's own header for the
-- real-addon citations behind each of those choices.

local ADDON_NAME, MA = ...

-- Short human number: 1234567 -> "1.2M", 153914 -> "153.9k". Below 1000 is
-- shown as an integer. Guards nil/non-number to "-" so a missing field
-- never errors the window.
local function FormatShort(n)
  if type(n) ~= "number" then return "-" end
  if n >= 1e6 then
    return string.format("%.1fM", n / 1e6)
  elseif n >= 1e3 then
    return string.format("%.1fk", n / 1e3)
  end
  return string.format("%d", n)
end

-- Milliseconds -> "MM:SS". Nil-safe.
local function FormatDuration(ms)
  if type(ms) ~= "number" then return "-" end
  local total = math.floor(ms / 1000)
  return string.format("%02d:%02d", math.floor(total / 60), total % 60)
end

-- One headline text block from the results' run/forces/verdict fields.
local function BuildHeadline(r)
  local run = r.run or {}
  local lines = {}
  local zone = run.zone or "Unknown"
  local level = run.level and ("+" .. run.level) or ""
  table.insert(lines, string.format("%s %s", zone, level))

  local verdict
  if run.completed == false then
    verdict = "|cffe0a020Not completed (abandoned)|r"
  elseif run.timed == true then
    verdict = "|cff58c47cTimed|r"
  elseif run.timed == false then
    verdict = "|cffe06060Over time (depleted)|r"
  else
    verdict = "Completed"
  end
  table.insert(lines, string.format("%s   Duration %s", verdict, FormatDuration(run.duration_ms)))

  local forces = r.forces or {}
  if type(forces.pct) == "number" then
    table.insert(lines, string.format("Forces: %.1f%%", forces.pct))
  end
  local statBits = {}
  table.insert(statBits, string.format("Deaths: %d", r.deaths or 0))
  if type(r.kick_efficiency_pct) == "number" then
    table.insert(statBits, string.format("Kick efficiency: %.0f%%", r.kick_efficiency_pct))
  end
  if type(r.adherence_pct) == "number" then
    table.insert(statBits, string.format("Route adherence: %.0f%%", r.adherence_pct))
  end
  table.insert(lines, table.concat(statBits, "   "))
  return table.concat(lines, "\n")
end

-- One line per player: name, role, deaths, interrupts, DPS/HPS, avoidable.
local function BuildPlayerLines(r)
  local players = r.players or {}
  if #players == 0 then return "(no per-player data)" end
  local lines = {}
  for _, p in ipairs(players) do
    local name = p.name or "?"
    -- Strip the -Realm suffix for compactness; keep it if parsing fails.
    local shortName = name:match("^([^-]+)") or name
    local dpsOrHps
    if (p.hps or 0) > (p.dps or 0) then
      dpsOrHps = FormatShort(p.hps) .. " HPS"
    else
      dpsOrHps = FormatShort(p.dps) .. " DPS"
    end
    table.insert(lines, string.format(
      "%s  -  %s   %d deaths, %d kicks, %s avoidable",
      shortName, dpsOrHps, p.deaths or 0, p.interrupts or 0,
      FormatShort(p.avoidable_damage_taken)
    ))
  end
  return table.concat(lines, "\n")
end

local TITLE_BAR_HEIGHT = 40
local BUTTON_HEIGHT = 22

local function CreateResultsFrame()
  local f = CreateFrame("Frame", "PostmortemResultsFrame", UIParent, "BackdropTemplate")
  f:SetSize(460, 1) -- height computed from content below
  f:SetFrameStrata("DIALOG")
  f:SetClampedToScreen(true)
  f:SetBackdrop({
    bgFile = "Interface\\Buttons\\WHITE8x8",
    edgeFile = "Interface\\Buttons\\WHITE8x8",
    edgeSize = 1,
  })
  f:SetBackdropColor(0.05, 0.04, 0.08, 0.9)
  f:SetBackdropBorderColor(0.15, 0.15, 0.15, 0.6)

  f:SetMovable(true)
  f:EnableMouse(true)
  f:RegisterForDrag("LeftButton")
  f:SetScript("OnDragStart", f.StartMoving)
  f:SetScript("OnDragStop", function(self)
    self:StopMovingOrSizing()
    local point, _, relativePoint, x, y = self:GetPoint()
    local db = MA:GetDB()
    db.resultsWindowPosition = db.resultsWindowPosition or {}
    db.resultsWindowPosition.point = point
    db.resultsWindowPosition.relativePoint = relativePoint
    db.resultsWindowPosition.x = x
    db.resultsWindowPosition.y = y
  end)

  local titleBar = CreateFrame("Frame", nil, f)
  titleBar:SetHeight(TITLE_BAR_HEIGHT)
  titleBar:SetPoint("TOPLEFT", f, "TOPLEFT", 0, 0)
  titleBar:SetPoint("TOPRIGHT", f, "TOPRIGHT", 0, 0)

  local titleFS = titleBar:CreateFontString(nil, "OVERLAY")
  titleFS:SetFontObject(GameFontNormal)
  titleFS:SetPoint("LEFT", titleBar, "LEFT", 16, 0)
  titleFS:SetPoint("RIGHT", titleBar, "RIGHT", -32, 0)
  titleFS:SetJustifyH("LEFT")
  titleFS:SetText("Postmortem -- run stats")

  local closeBtn = CreateFrame("Button", nil, titleBar, "UIPanelCloseButtonNoScripts")
  closeBtn:SetPoint("TOPRIGHT", titleBar, "TOPRIGHT", -2, -2)
  closeBtn:RegisterForClicks("LeftButtonUp")
  closeBtn:SetScript("OnClick", function() f:Hide() end)

  local headlineFS = f:CreateFontString(nil, "OVERLAY")
  headlineFS:SetFontObject(GameFontHighlight)
  headlineFS:SetPoint("TOPLEFT", titleBar, "BOTTOMLEFT", 16, -8)
  headlineFS:SetPoint("RIGHT", f, "RIGHT", -16, 0)
  headlineFS:SetJustifyH("LEFT")
  headlineFS:SetJustifyV("TOP")
  headlineFS:SetSpacing(4)
  f.headlineFS = headlineFS

  local playersFS = f:CreateFontString(nil, "OVERLAY")
  playersFS:SetFontObject(GameFontHighlightSmall)
  playersFS:SetPoint("TOPLEFT", headlineFS, "BOTTOMLEFT", 0, -14)
  playersFS:SetPoint("RIGHT", f, "RIGHT", -16, 0)
  playersFS:SetJustifyH("LEFT")
  playersFS:SetJustifyV("TOP")
  playersFS:SetSpacing(4)
  f.playersFS = playersFS

  local footerFS = f:CreateFontString(nil, "OVERLAY")
  footerFS:SetFontObject(GameFontDisableSmall)
  footerFS:SetPoint("TOPLEFT", playersFS, "BOTTOMLEFT", 0, -14)
  footerFS:SetPoint("RIGHT", f, "RIGHT", -16, 0)
  footerFS:SetJustifyH("LEFT")
  footerFS:SetText("Full breakdown (route, pulls, per-pull damage) is on the site "
    .. "and in the desktop app. /reload after a run to refresh these numbers.")
  f.footerFS = footerFS

  local closeButton = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
  closeButton:SetSize(80, BUTTON_HEIGHT)
  closeButton:SetText(CLOSE)
  closeButton:SetPoint("TOPRIGHT", footerFS, "BOTTOMRIGHT", 0, -14)
  closeButton:SetScript("OnClick", function() f:Hide() end)
  f.closeButton = closeButton

  local db = MA:GetDB()
  local pos = db.resultsWindowPosition
      or { point = "CENTER", relativePoint = "CENTER", x = 0, y = 0 }
  f:SetPoint(pos.point, UIParent, pos.relativePoint, pos.x, pos.y)

  tinsert(UISpecialFrames, "PostmortemResultsFrame")
  f:Hide()
  return f
end

-- Populate + size the frame from the current PostmortemResults global, then
-- show it. Read fresh each call so a /reload's newer data is always used.
function MA:Results_Show()
  local r = _G.PostmortemResults
  if type(r) ~= "table" or type(r.run) ~= "table" then
    print("|cffd7a94cPostmortem|r: no analyzed run yet. Finish a key with the "
      .. "desktop app watching, then |cffffff00/reload|r and try again.")
    return
  end

  local f = _G.PostmortemResultsFrame or CreateResultsFrame()
  f.headlineFS:SetText(BuildHeadline(r))
  f.playersFS:SetText(BuildPlayerLines(r))

  -- Height from the actual rendered content (same approach as
  -- InfoWindow.lua): every FontString has its final SetText and its
  -- width-defining anchors by now, so GetStringHeight is real.
  local totalHeight = TITLE_BAR_HEIGHT
    + 8 + f.headlineFS:GetStringHeight()
    + 14 + f.playersFS:GetStringHeight()
    + 14 + f.footerFS:GetStringHeight()
    + 14 + BUTTON_HEIGHT + 12
  f:SetHeight(totalHeight)
  f:Show()
end

-- On load, if fresh results are present, print a one-line pointer (once) so
-- the player knows the in-game stats are available without having to guess
-- the command.
local function AnnounceIfPresent()
  local r = _G.PostmortemResults
  if type(r) == "table" and type(r.run) == "table" then
    local run = r.run
    local label = (run.zone or "run") .. (run.level and (" +" .. run.level) or "")
    print(string.format(
      "|cffd7a94cPostmortem|r: stats loaded for %s -- |cffffff00/pm results|r to view.",
      label
    ))
  end
end

local frame = CreateFrame("Frame")
frame:RegisterEvent("PLAYER_LOGIN")
frame:SetScript("OnEvent", function()
  -- PLAYER_LOGIN fires after SavedVariables + all addon files (including
  -- the PostmortemResults.lua data file) have loaded, so the global is
  -- populated by now.
  AnnounceIfPresent()
end)
