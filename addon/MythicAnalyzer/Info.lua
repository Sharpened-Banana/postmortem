-- Info.lua
-- Shared "what's live vs. what needs the companion app" data (MA.INFO) plus
-- the two StaticPopups that read it: a copy-URL popup (addons can't open a
-- browser) and a first-load popup shown once per WoW account. Later work
-- packages (a minimap button, a full info window) render MA.INFO themselves
-- and call MA:Info_ShowLinkPopup() / define MA.Info_Toggle -- this file is
-- the single source of truth for the text so it's never typed twice.
--
-- StaticPopupDialogs shape (hasEditBox + OnShow SetText/HighlightText/
-- SetFocus, preferredIndex = 3) verified this session against a real,
-- currently-installed addon's copy-URL popup -- this is the standard,
-- verified-real WoW pattern for letting a user select/copy a URL, since
-- addons cannot open a web browser directly. preferredIndex = 3 is a real,
-- deliberate convention some addons use to avoid a taint-related edge case
-- with the default StaticPopup stack position.

local ADDON_NAME, MA = ...

-- Single source of truth for every piece of "what's live vs. what needs the
-- companion app" text anywhere in this addon. A later WP's minimap tooltip,
-- info window, and the recap-panel reminder all read from this table --
-- none of them should ever have this text typed a second time.
MA.INFO = {
  url = "https://github.com/Sharpened-Banana/Mythic-Analyzer/releases",

  headline = "Mythic-Analyzer -- live in-game half",
  subhead = "The deep post-mortem runs in the free companion desktop app.",

  liveTitle = "Live, in the addon",
  liveLines = {
    "Auto combat logging on/off at key start and end",
    "Enemy forces % and run timer",
    "Deaths, with timer seconds lost",
    "Interrupt count for the group",
    "Pull N / M vs. your MDT route (coarse, needs MDT)",
    "15s recap after the key: was the run recorded?",
  },

  appTitle = "Full post-mortem -- companion app (free, separate download)",
  appLines = {
    "Per-player damage, healing, DPS/HPS and damage taken",
    "Per-spell and per-pull breakdowns for every player",
    "Kick value: damage/healing each interrupt actually prevented",
    "Kick efficiency: every enemy cast kicked, missed or outlived",
    "Death recaps: killing blow, last hits, remaining HP",
    "Avoidable damage tagged per player, with hit counts",
    "Route deviation by pack: early, late, off-route, never pulled",
    "Boss attempt history, kills, wipes and durations",
    "Run history across sessions, filterable and sortable",
    "Raider.io score enrichment for the whole group",
  },

  footer = "The addon cannot do this on its own: WoW blocks addons from reading WoWCombatLog.txt, and MDT's per-dungeon NPC data is private to that addon. Both live outside the game -- so the analysis does too.",

  -- Shown on the post-key recap panel (Overlay.lua, a later WP) -- kept to
  -- an explicit two lines (not left to word-wrap) since that panel is only
  -- 220px wide.
  recapLine = "Full analysis: companion app\n(minimap icon or /ma)",

  popupText = "Mythic-Analyzer tracks your key live: forces, timer, deaths, interrupts, and auto combat logging.\n\nThe full post-mortem -- kick value, per-player breakdowns, death recaps, route deviation by pack -- runs in the free companion desktop app.\n\nTake a look at what it adds?",
}

-- Standard copy-URL popup: addons cannot open a web browser directly, so the
-- established pattern is a StaticPopup with an edit box pre-filled and
-- highlighted so Ctrl+C works immediately.
-- verified this session against a real installed addon's hasEditBox
-- StaticPopup.
StaticPopupDialogs["MYTHICANALYZER_COPY_URL"] = {
  text = "Press Ctrl+C to copy",
  button1 = CLOSE,
  hasEditBox = 1,
  editBoxWidth = 350,
  maxLetters = 0,
  OnShow = function(self, data)
    local editBox = self:GetEditBox()
    editBox:SetText(data)
    editBox:HighlightText()
    editBox:SetFocus()
  end,
  EditBoxOnEnterPressed = function(self) self:GetParent():Hide() end,
  EditBoxOnEscapePressed = StaticPopup_StandardEditBoxOnEscapePressed,
  exclusive = true,
  whileDead = true,
  hideOnEscape = true,
  preferredIndex = 3,
}

function MA:Info_ShowLinkPopup()
  StaticPopup_Show("MYTHICANALYZER_COPY_URL", nil, nil, MA.INFO.url)
end

-- Shown exactly once per WoW account, ever (gated by db.infoPopupSeen; see
-- the first-load frame below), then never again.
--
-- infoPopupSeen is set in OnShow, not in OnAccept/OnCancel: hideOnEscape = 1
-- means the user can dismiss this popup via ESC, /reload, or logout without
-- ever pressing a button -- if the flag were only set in a button handler,
-- the popup would come back every single login until they explicitly click
-- something. Setting it in OnShow makes every dismissal path terminal. This
-- is deliberate -- do not change it to only fire on button click.
StaticPopupDialogs["MYTHICANALYZER_FIRST_LOAD"] = {
  -- Static text, not set dynamically via self.text:SetText() in OnShow --
  -- confirmed via a real in-game error (BugSack: "MythicAnalyzer/Info.lua:
  -- attempt to index field 'text' (a nil value)") that self.text is NOT a
  -- reliable accessor on a StaticPopup frame at OnShow time, despite it
  -- looking plausible from other addons' unrelated use of a `.text`
  -- FontString field on their OWN custom frames. MA.INFO is already fully
  -- defined above in this same file by the time this table is built, so
  -- there's no need to set this dynamically at all -- just reference it
  -- directly, the same way MYTHICANALYZER_COPY_URL's `text` field above
  -- does.
  text = MA.INFO.popupText,
  button1 = "Show me",
  button2 = CLOSE,
  OnShow = function(self)
    local db = MA:GetDB()
    if db then db.infoPopupSeen = true end
  end,
  -- Info_Toggle is defined by a later WP (a full info window) that doesn't
  -- exist yet, so this is called through this addon's established guarded
  -- cross-file-call idiom rather than directly. Falling back to
  -- Info_ShowLinkPopup() when it's absent keeps this WP fully functional
  -- and shippable on its own.
  OnAccept = function()
    if MA.Info_Toggle then
      MA.Info_Toggle(MA)
    else
      MA:Info_ShowLinkPopup()
    end
  end,
  timeout = 0,
  whileDead = 1,
  hideOnEscape = 1,
  preferredIndex = 3,
}

-- One-shot PLAYER_LOGIN handler for the first-load popup. Only fires if the
-- player hasn't seen it AND isn't currently in an active Mythic+ key (a
-- /reload mid-key also fires PLAYER_LOGIN -- popping a modal dialog mid-pull
-- would be a real, bad UX bug). Same file-scope-event-frame convention as
-- Tracker.lua's own event frames, decoupled from other files.
--
-- Deliberately does NOT call self:UnregisterEvent()/UnregisterAllEvents()
-- here -- confirmed via a real in-game error (BugSack:
-- "[ADDON_ACTION_FORBIDDEN] AddOn 'MythicAnalyzer' tried to call the
-- protected function 'Frame:UnregisterEvent()'") that doing so from inside
-- this handler is treated as a protected/forbidden action. There's no
-- actual need to unregister anyway: PLAYER_LOGIN only ever fires once per
-- UI load, and a fresh /reload recreates this whole frame from scratch, so
-- leaving the registration in place costs nothing and avoids the taint
-- issue entirely rather than just working around it.
--
-- The 5-second delay and the re-check-after-the-delay (both before AND
-- after the C_Timer.After) are deliberate -- PLAYER_LOGIN can fire before
-- other UI is settled, and re-checking infoPopupSeen/challenge-mode status
-- after the delay guards against a race where the state changed during
-- those 5 seconds. Keep both checks.
local firstLoadFrame = CreateFrame("Frame")
firstLoadFrame:RegisterEvent("PLAYER_LOGIN")
firstLoadFrame:SetScript("OnEvent", function(self)
  local db = MA:GetDB()
  if not db or db.infoPopupSeen then return end
  C_Timer.After(5, function()
    if C_ChallengeMode.GetActiveChallengeMapID() then return end
    local db2 = MA:GetDB()
    if not db2 or db2.infoPopupSeen then return end
    StaticPopup_Show("MYTHICANALYZER_FIRST_LOAD")
  end)
end)
