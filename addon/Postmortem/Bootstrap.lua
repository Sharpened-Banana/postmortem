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

-- Default values for PostmortemDB.global. Later work packages will add
-- more settings here -- keep this list limited to what each work package
-- actually reads.
local defaults = {
  global = {
    combatLoggingEnabled = true,
    -- Debug mode (/pm debug): run every key-scoped module as if a key had
    -- just started, from login onward, so each piece can be checked in
    -- town instead of only mid-key. Persisted so a /reload keeps it on.
    debugMode = false,
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
    -- Results.lua's in-game crunched-stats window (fed by the desktop
    -- app's PostmortemResults.lua writeback); saved on drag, same as the
    -- overlay/info windows above.
    resultsWindowPosition = { point = "CENTER", relativePoint = "CENTER", x = 0, y = 0 },
  },
}

-- Default values for PostmortemSpellDB.global -- a separate
-- SavedVariables table from PostmortemDB (above) because this holds a
-- growing, self-built spell interrupt-flag database (InterruptDatabase.lua),
-- not user settings. An empty table is the only meaningful default: there's
-- nothing to pre-fill, just a well-formed table to grow into.
local spellDbDefaults = {
  global = {},
}

-- Ensures PostmortemDB (declared via ## SavedVariables in the .toc) is a
-- well-formed table before anything reads or writes it, then points MA.db
-- at the live table so the rest of the addon never touches the global
-- directly.
-- verified against MythicDungeonTools/Core/Bootstrap.lua (InitializeSavedVariables)
local function InitializeSavedVariables()
  if type(PostmortemDB) ~= "table" then PostmortemDB = {} end
  if type(PostmortemDB.global) ~= "table" then PostmortemDB.global = {} end
  applyDefaults(PostmortemDB.global, defaults.global)
  MA.db = PostmortemDB.global

  -- Same guard pattern, second SavedVariables table -- see spellDbDefaults
  -- above for why this is kept separate from PostmortemDB.
  if type(PostmortemSpellDB) ~= "table" then PostmortemSpellDB = {} end
  if type(PostmortemSpellDB.global) ~= "table" then PostmortemSpellDB.global = {} end
  applyDefaults(PostmortemSpellDB.global, spellDbDefaults.global)
  MA.spellDb = PostmortemSpellDB.global
end

-- Accessor other files should use instead of touching PostmortemDB
-- directly, so a future WP can change the storage mechanism in one place.
-- verified against MythicDungeonTools/Core/Bootstrap.lua (MDT:GetDB)
function MA:GetDB()
  return self.db
end

-- Sibling accessor for the spell interrupt-flag database (see
-- spellDbDefaults above). InterruptDatabase.lua reads/writes through this
-- rather than touching PostmortemSpellDB directly.
function MA:GetSpellDB()
  return self.spellDb
end

-- Empty init stub for later work packages to extend. Called once, after
-- SavedVariables are ready, from the ADDON_LOADED handler below.
function MA:OnInitialize()
  if MA.MinimapButton_Initialize then MA.MinimapButton_Initialize(MA) end
end

-- ---------------------------------------------------------------------------
-- Debug mode
--
-- Every key-scoped module (CombatLogging, Tracker, Interrupts, RouteImport,
-- InterruptDatabase) owns a frame that reacts to CHALLENGE_MODE_START /
-- COMPLETED / RESET. Debug mode drives those same handlers by hand: on
-- login it fires a synthetic CHALLENGE_MODE_START through every registered
-- frame, runs a 1s ticker in place of Blizzard's in-key timer hook, and
-- makes MA:IsKeyActive() answer true so the overlay shows. Turning it off
-- fires CHALLENGE_MODE_RESET the same way. Nothing in the modules changes
-- between debug and real keys except who fires the event -- that's the
-- point: what works here works on a real CHALLENGE_MODE_START.
-- ---------------------------------------------------------------------------

MA.keyEventFrames = {}

-- Each key-scoped module registers its CHALLENGE_MODE_* frame here so debug
-- mode can dispatch synthetic events to it in .toc load order.
function MA:RegisterKeyEventFrame(frame)
  table.insert(self.keyEventFrames, frame)
end

function MA:IsDebugMode()
  return self.db and self.db.debugMode and true or false
end

-- Chat output that only appears in debug mode. Every module reports its
-- start/stop and notable events through this, so the chat frame becomes a
-- trace of what actually fired.
function MA:Debug(fmt, ...)
  if not self:IsDebugMode() then return end
  local ok, msg = pcall(string.format, fmt, ...)
  print("|cffd7a94cPostmortem debug|r: " .. (ok and msg or tostring(fmt)))
end

-- True if Blizzard says a Mythic+ challenge is literally running right
-- now -- or always, in debug mode. ONLY meant for Tracker.lua's reload-
-- recovery check (was a key already going when the addon just (re)loaded?
-- MA.state.active can't answer that -- it's reset fresh on every load).
-- Everywhere else that wants "is a key active" should read MA.state.active
-- instead (kept up to date by CHALLENGE_MODE_START/COMPLETED/RESET), NOT
-- call this.
--
-- Real bug fixed 2026-09-04: this used to also fall back to "am I in a
-- Mythic+ instance" (instanceType == "party" and difficultyID == 8, per
-- MythicDungeonTools/Core/CombatLogging.lua's own such check) whenever
-- GetActiveChallengeMapID() was nil, to bridge IsChallengeModeActive()
-- flipping false a moment before our own CHALLENGE_MODE_COMPLETED handler
-- runs. That fallback is true for as long as you're standing anywhere in
-- the dungeon, key running or not -- which is most of the time between
-- keys. Overlay.lua used to call this function directly instead of
-- trusting MA.state.active, so the overlay never hid after a key ended;
-- and Tracker.lua's PLAYER_ENTERING_WORLD handler used it to decide
-- whether a /reload happened mid-key, so the documented "finish key ->
-- /reload -> /pm results" flow re-triggered CHALLENGE_MODE_START's whole
-- side effects (including turning combat logging back on) on every single
-- post-key reload, for as long as the group stayed in the instance.
-- GetActiveChallengeMapID() alone already correctly returns nil the moment
-- a challenge ends, even while still standing in the dungeon -- the
-- instance-wide fallback was solving a real (but far narrower) race with
-- a check broad enough to misfire for the rest of every key.
function MA:IsKeyActive()
  if self:IsDebugMode() then return true end
  return C_ChallengeMode.GetActiveChallengeMapID() ~= nil
end

local function DispatchKeyEvent(event)
  for _, frame in ipairs(MA.keyEventFrames) do
    local handler = frame:GetScript("OnEvent")
    if handler then
      local ok, err = pcall(handler, frame, event)
      if not ok then
        print("|cffe06060Postmortem debug|r: a module errored on " .. event .. ": " .. tostring(err))
      end
    end
  end
end

local debugTicker = nil
local debugRunning = false

function MA:Debug_Start()
  if debugRunning then return end
  debugRunning = true
  print("|cffd7a94cPostmortem|r: DEBUG MODE ON -- running every module as if a key just started. /pm debug to turn off.")
  self:Debug("dispatching CHALLENGE_MODE_START to %d module frames", #self.keyEventFrames)
  DispatchKeyEvent("CHALLENGE_MODE_START")
  -- Blizzard's ChallengeModeBlock.UpdateTime (Tracker.lua's normal tick
  -- source) only runs inside a key, so drive the same tick ourselves.
  debugTicker = C_Timer.NewTicker(1, function()
    if MA.Tracker_OnTick then MA:Tracker_OnTick() end
  end)
end

function MA:Debug_Stop()
  if not debugRunning then return end
  debugRunning = false
  if debugTicker then debugTicker:Cancel(); debugTicker = nil end
  self:Debug("dispatching CHALLENGE_MODE_RESET to %d module frames", #self.keyEventFrames)
  DispatchKeyEvent("CHALLENGE_MODE_RESET")
  print("|cffd7a94cPostmortem|r: debug mode OFF -- modules will trigger on a real key start again.")
  if MA.Overlay_Refresh then MA.Overlay_Refresh(MA) end
end

function MA:Debug_Toggle()
  local db = self:GetDB()
  if db.debugMode then
    -- Stop first, then clear the flag: the modules' own "stopped" trace
    -- lines go through MA:Debug(), which is silent once the flag is off.
    self:Debug_Stop()
    db.debugMode = false
  else
    db.debugMode = true
    self:Debug_Start()
  end
end

-- Debug mode starts on PLAYER_LOGIN (not ADDON_LOADED): by then every
-- module file has loaded and registered its frame, MDT's SavedVariables
-- (the route source) are in, and the UI is ready to draw the overlay.
--
-- Deliberately does NOT call self:UnregisterEvent("PLAYER_LOGIN") here --
-- Info.lua's firstLoadFrame hit this exact real, confirmed in-game error
-- (BugSack: "[ADDON_ACTION_FORBIDDEN] AddOn 'Postmortem' tried to call
-- the protected function 'Frame:UnregisterEvent()'") from unregistering
-- inside a PLAYER_LOGIN handler specifically -- see that file's own
-- comment on this, missed here on 2026-09-04 and reproduced live
-- (BugSack, 2026-09-05). PLAYER_LOGIN only ever fires once per UI load
-- and a fresh /reload recreates this whole frame from scratch anyway, so
-- leaving the registration in place costs nothing and avoids the taint
-- issue entirely rather than just working around it, same reasoning as
-- Info.lua's firstLoadFrame.
local loginFrame = CreateFrame("Frame")
loginFrame:RegisterEvent("PLAYER_LOGIN")
loginFrame:SetScript("OnEvent", function()
  if MA:IsDebugMode() then
    C_Timer.After(2, function() MA:Debug_Start() end)
  end
end)

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
