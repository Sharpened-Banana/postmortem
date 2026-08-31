-- MinimapButton.lua
-- Minimap icon / Data Broker launcher for Mythic-Analyzer, built on the
-- vendored LibDataBroker-1.1 + LibDBIcon-1.0 (see libs/, loaded first via
-- libs/embeds.xml). Left-click will open the full info window once a later
-- WP defines MA.Info_Toggle; right-click shows the copy-download-link popup
-- from Info.lua (this WP calls into it, does not redefine it).
--
-- LibStub(..., true) (the trailing `true`) makes a missing/broken library
-- return nil instead of erroring; combined with the guard below, a broken or
-- partial library copy degrades to "no minimap button" rather than a hard
-- load error that would break the whole addon.
--
-- IsRegistered() guard: LibDBIcon-1.0's own Register() calls error() on a
-- duplicate registration (verified this session by reading its source).
-- MA:OnInitialize() should only ever run once per session (see Bootstrap.lua's
-- ADDON_LOADED handler), but this guard is cheap insurance.
--
-- OnClick(_, button) / OnTooltipShow(tooltip) signatures match how
-- LibDBIcon-1.0 actually calls a data object's handlers internally
-- (verified this session against its own source).

local ADDON_NAME, MA = ...

function MA:MinimapButton_Initialize()
  local LDB = LibStub and LibStub("LibDataBroker-1.1", true)
  local LDBIcon = LibStub and LibStub("LibDBIcon-1.0", true)
  if not (LDB and LDBIcon) then return end
  if LDBIcon:IsRegistered(ADDON_NAME) then return end

  local dataObject = LDB:NewDataObject(ADDON_NAME, {
    type = "launcher",
    text = "Mythic-Analyzer",
    icon = "Interface\\Icons\\INV_Relics_Hourglass_02",
    OnClick = function(_, button)
      if button == "RightButton" then
        MA:Info_ShowLinkPopup()
      elseif MA.Info_Toggle then
        MA.Info_Toggle(MA)
      else
        MA:Info_ShowLinkPopup()
      end
    end,
    OnTooltipShow = function(tooltip)
      local version = C_AddOns.GetAddOnMetadata(ADDON_NAME, "Version")
      tooltip:AddLine("Mythic-Analyzer" .. (version and (" " .. version) or ""))
      tooltip:AddLine("Left-click: what the companion app adds", 1, 1, 1)
      tooltip:AddLine("Right-click: copy download link", 1, 1, 1)
    end,
  })

  LDBIcon:Register(ADDON_NAME, dataObject, MA:GetDB().minimapIcon)
  MA.minimapIcon = LDBIcon
end

function MA:MinimapButton_Toggle()
  local LDBIcon = MA.minimapIcon
  if not LDBIcon then return end
  local db = MA:GetDB()
  db.minimapIcon.hide = not db.minimapIcon.hide
  if db.minimapIcon.hide then
    LDBIcon:Hide(ADDON_NAME)
  else
    LDBIcon:Show(ADDON_NAME)
  end
end
