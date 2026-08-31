-- Bootstrap.lua
-- Addon entry point: shared addon table, SavedVariables init, and the
-- ADDON_LOADED lifecycle event. Every other file in this addon receives the
-- same MA table via the "..." vararg idiom below, so attach shared state
-- and methods to MA rather than to globals.
--
-- Pattern verified against MythicDungeonTools/Core/Bootstrap.lua and
-- MythicDungeonTools/Core/Lifecycle.lua (real, currently-shipping addon
-- source), adapted to this addon's own name/table/SavedVariables.

local ADDON_NAME, MA = ...

-- Recursively fills in any keys missing from `target` using `defaults`,
-- without clobbering values the user (or a previous session) already set.
-- Table-valued defaults are deep-copied so multiple SavedVariables entries
-- never share the same underlying table.
-- verified against MythicDungeonTools/Core/Bootstrap.lua (applyDefaults)
local function applyDefaults(target, defaults)
  for key, value in pairs(defaults) do
    if target[key] == nil then
      target[key] = type(value) == "table" and CopyTable(value) or value
    elseif type(target[key]) == "table" and type(value) == "table" then
      applyDefaults(target[key], value)
    end
  end
end

-- Default values for MythicAnalyzerDB.global. Later work packages will add
-- more settings here -- keep this list limited to what each work package
-- actually reads.
local defaults = {
  global = {
    combatLoggingEnabled = true,
    -- Overlay.lua's draggable status frame: saved on OnDragStop via
    -- frame:GetPoint(), restored via frame:SetPoint() when the overlay
    -- frame is first created.
    overlayPosition = { point = "CENTER", relativePoint = "CENTER", x = 0, y = 0 },
    -- Below: shape reserved for Info.lua (this WP) and two later work
    -- packages (a minimap button and an info window) so those WPs don't
    -- need to touch Bootstrap.lua again. infoPopupSeen is set by Info.lua's
    -- first-load popup; minimapIcon and infoWindowPosition are consumed by
    -- the not-yet-built minimap button and info window respectively -- no
    -- functionality for either is implemented here.
    infoPopupSeen = false,
    minimapIcon = { hide = false, showInCompartment = true },
    infoWindowPosition = { point = "CENTER", relativePoint = "CENTER", x = 0, y = 0 },
  },
}

-- Default values for MythicAnalyzerSpellDB.global -- a separate
-- SavedVariables table from MythicAnalyzerDB (above) because this holds a
-- growing, self-built spell interrupt-flag database (InterruptDatabase.lua),
-- not user settings. An empty table is the only meaningful default: there's
-- nothing to pre-fill, just a well-formed table to grow into.
local spellDbDefaults = {
  global = {},
}

-- Ensures MythicAnalyzerDB (declared via ## SavedVariables in the .toc) is a
-- well-formed table before anything reads or writes it, then points MA.db
-- at the live table so the rest of the addon never touches the global
-- directly.
-- verified against MythicDungeonTools/Core/Bootstrap.lua (InitializeSavedVariables)
local function InitializeSavedVariables()
  if type(MythicAnalyzerDB) ~= "table" then MythicAnalyzerDB = {} end
  if type(MythicAnalyzerDB.global) ~= "table" then MythicAnalyzerDB.global = {} end
  applyDefaults(MythicAnalyzerDB.global, defaults.global)
  MA.db = MythicAnalyzerDB.global

  -- Same guard pattern, second SavedVariables table -- see spellDbDefaults
  -- above for why this is kept separate from MythicAnalyzerDB.
  if type(MythicAnalyzerSpellDB) ~= "table" then MythicAnalyzerSpellDB = {} end
  if type(MythicAnalyzerSpellDB.global) ~= "table" then MythicAnalyzerSpellDB.global = {} end
  applyDefaults(MythicAnalyzerSpellDB.global, spellDbDefaults.global)
  MA.spellDb = MythicAnalyzerSpellDB.global
end

-- Accessor other files should use instead of touching MythicAnalyzerDB
-- directly, so a future WP can change the storage mechanism in one place.
-- verified against MythicDungeonTools/Core/Bootstrap.lua (MDT:GetDB)
function MA:GetDB()
  return self.db
end

-- Sibling accessor for the spell interrupt-flag database (see
-- spellDbDefaults above). InterruptDatabase.lua reads/writes through this
-- rather than touching MythicAnalyzerSpellDB directly.
function MA:GetSpellDB()
  return self.spellDb
end

-- Empty init stub for later work packages to extend. Called once, after
-- SavedVariables are ready, from the ADDON_LOADED handler below.
function MA:OnInitialize()
  if MA.MinimapButton_Initialize then MA.MinimapButton_Initialize(MA) end
end

-- True if the Mythic Dungeon Tools addon is fully loaded. Wired here
-- (rather than in a later file) because it is small and multiple future
-- work packages (route import, etc.) will need it.
-- verified: C_AddOns.IsAddOnLoaded("MythicDungeonTools") is used exactly
-- this way (real, currently-shipping code) in MythicDungeonTools/Core/
-- Bootstrap.lua:111, Modules/Conflicts.lua:93, and Details_MythicPlus/
-- dummytails.lua:204-208. It returns TWO values, (loadedOrLoading, loaded)
-- -- MDT's own Bootstrap.lua:111 destructures both explicitly. This
-- function returns only the second ("actually loaded", not just
-- loading-in-progress) as a single boolean, so callers get an unambiguous
-- true/false rather than accidentally depending on Lua's silent
-- truncate-to-first-value behavior.
function MA:IsMDTPresent()
  local _, loaded = C_AddOns.IsAddOnLoaded("MythicDungeonTools")
  return loaded and true or false
end

-- ADDON_LOADED fires once per addon as it loads; filter to our own name and
-- self-unregister so the handler doesn't run again for other addons.
-- verified against MythicDungeonTools/Core/Lifecycle.lua
local eventFrame = CreateFrame("Frame")
eventFrame:RegisterEvent("ADDON_LOADED")
eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event == "ADDON_LOADED" then
    local loadedAddonName = ...
    if loadedAddonName ~= ADDON_NAME then return end
    InitializeSavedVariables()
    MA:OnInitialize() -- later WPs hook additional init here
    self:UnregisterEvent("ADDON_LOADED")
  end
end)
