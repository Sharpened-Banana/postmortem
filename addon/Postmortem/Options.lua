-- Options.lua
-- Native Blizzard Settings panel (Escape > Options > AddOns > Postmortem),
-- one checkbox/slider per feature toggle in Bootstrap.lua's defaults table,
-- all bound directly to PostmortemDB.global (MA:GetDB()) via
-- Settings.RegisterAddOnSetting's variableTbl/variableKey arguments -- no
-- separate options-only storage.
--
-- Pattern verified this session against two real, currently-shipping
-- addons in the same "~15-20 toggles" bucket as this one, both of which use
-- native Settings.* widgets rather than a hand-rolled canvas window (bigger
-- suites -- EllesmereUI, Plumber, RaiderIO -- use a canvas stub instead,
-- not the right model here): BugSack/config.lua and ClassCodex/Settings.lua.
-- Settings.RegisterVerticalLayoutSubcategory has ZERO real usage across
-- every addon installed on this machine -- grouping is done with
-- CreateSettingsListSectionHeaderInitializer instead
-- (ClassCodex/Settings.lua:36-39), which is what this file does.
--
-- Every default value restated here MUST match Bootstrap.lua's defaults
-- table exactly -- Settings.RegisterAddOnSetting requires its own default
-- argument (there's no way to ask it to read Bootstrap.lua's table), so
-- this single duplication is a real API requirement, not drift-prone
-- laziness. If you add a new toggle, add it to BOTH files.
--
-- Built from MA:OnInitialize() (Bootstrap.lua), not at this file's own
-- load time: MA:GetDB() only returns a real table once ADDON_LOADED has
-- run InitializeSavedVariables(), and MA:OnInitialize() is the documented
-- extension point for exactly that "after SavedVariables are ready" timing
-- -- calling Settings.RegisterAddOnSetting with a nil variableTbl at plain
-- file-load time would error.

local ADDON_NAME, MA = ...

local category
local categoryID

local function RefreshLiveState()
  if MA.Overlay_Refresh then MA:Overlay_Refresh() end
end

local function Header(layout, label)
  layout:AddInitializer(CreateSettingsListSectionHeaderInitializer(label))
end

-- Registers one checkbox bound to MA:GetDB()[variable]. onChange (optional)
-- runs after the value has already been written -- read the new value back
-- off MA:GetDB() rather than assuming which way it flipped, same idiom
-- ClassCodex/Settings.lua's own check() helper uses.
local function Checkbox(variable, name, tooltip, default, onChange)
  local setting = Settings.RegisterAddOnSetting(
    category, "Postmortem_" .. variable, variable, MA:GetDB(),
    type(default), name, default
  )
  Settings.CreateCheckbox(category, setting, tooltip)
  Settings.SetOnValueChangedCallback("Postmortem_" .. variable, function()
    if onChange then onChange() end
    RefreshLiveState()
  end)
end

local function Slider(variable, name, tooltip, default, minValue, maxValue, step)
  local setting = Settings.RegisterAddOnSetting(
    category, "Postmortem_" .. variable, variable, MA:GetDB(),
    type(default), name, default
  )
  local options = Settings.CreateSliderOptions(minValue, maxValue, step)
  options:SetLabelFormatter(MinimalSliderWithSteppersMixin.Label.Right)
  Settings.CreateSlider(category, setting, options, tooltip)
end

local function BuildSettingsPanel()
  if not Settings or not Settings.RegisterVerticalLayoutCategory then
    -- Client too old for the modern Settings API (pre-11.0) -- nothing to
    -- register; MA:Options_Open() below just prints a message instead.
    return
  end

  local layout
  category, layout = Settings.RegisterVerticalLayoutCategory("Postmortem")

  Header(layout, "General")
  Checkbox("combatLoggingEnabled", "Auto combat logging",
    "Turn on combat logging (and advanced combat logging) for the duration of every Mythic+ key, and back off afterward.",
    true)
  Checkbox("warnLoggingConflicts", "Warn about other logging addons",
    "Print a one-time notice per login if another installed addon (MDT, EnhanceQoL Dungeon & Raid, EllesmereUI QoL, Hindsight) also manages combat logging.",
    true)

  Header(layout, "Overlay")
  Checkbox("showChestTimer", "Show +2 / +3 chest timer",
    "Show the live +2/+3 chest countdown row on the overlay.", true)
  Checkbox("showBossSplits", "Show boss/objective splits",
    "Show the most recently completed boss's split time and personal-best delta on the overlay.", true)
  Checkbox("showPullProgress", "Show pull progress",
    "Show \"Pull N / M\" against your currently selected Mythic Dungeon Tools route.", true)
  Checkbox("showInterrupts", "Show interrupt count",
    "Show the live group interrupt count on the overlay.", true)
  Checkbox("tagDeaths", "Tag death cause in recap",
    "After a death, show which spell killed you (and whether it was avoidable) in the post-key recap.", true)

  Header(layout, "Run History")
  Checkbox("saveRunHistory", "Save run history",
    "Keep a local log of past keys, viewable with /pm history.", true)
  Slider("runHistoryLimit", "Runs to keep",
    "How many past runs to keep before the oldest are trimmed.", 50, 10, 200, 10)

  Header(layout, "Announcements")
  Checkbox("announceCompletion", "Announce completion to party",
    "Post a one-line summary (chest verdict, deaths) to party/instance chat when a key finishes.", true)

  Header(layout, "Advanced")
  -- Not Debug_Toggle() here -- that function reads db.debugMode to decide
  -- which way to flip, but this checkbox has ALREADY written the new value
  -- into db.debugMode by the time this callback runs (same "already
  -- written, read it back" ordering the RefreshLiveState pattern above
  -- relies on) -- calling Debug_Toggle() here would treat the new value as
  -- the old one and invert the checkbox's own effect. Debug_Start/Stop
  -- themselves don't touch db.debugMode (only Debug_Toggle does), so
  -- calling the one that matches the already-set value directly is correct.
  Checkbox("debugMode", "Debug mode",
    "Run every module now, as if a key just started (same as /pm debug).", false, function()
      if MA:GetDB().debugMode then
        MA:Debug_Start()
      else
        MA:Debug_Stop()
      end
    end)

  Settings.RegisterAddOnCategory(category)
  categoryID = category:GetID()
end

-- Called from Bootstrap.lua's MA:OnInitialize(), after SavedVariables are
-- ready (see this file's header for why it can't run at plain file-load
-- time).
function MA:Options_Initialize()
  BuildSettingsPanel()
end

-- /pm options.
function MA:Options_Open()
  if categoryID and Settings and Settings.OpenToCategory then
    Settings.OpenToCategory(categoryID)
  else
    print("|cffd7a94cPostmortem|r: settings panel isn't available on this client.")
  end
end
