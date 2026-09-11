-- Minimal WoW-API stubs: load Overlay.lua and drive MA:Overlay_Refresh(),
-- then assert the rendered row order, the countdown text and its colour.
local widgets = {}
local function NewWidget(kind, parent)
  local w = {kind = kind, parent = parent, points = {}, shown = true, height = 0, text = ""}
  function w:SetPoint(p, rel, relP, x, y) table.insert(self.points, {p = p, rel = rel, relP = relP, x = x, y = y}) end
  function w:ClearAllPoints() self.points = {} end
  function w:SetAllPoints() end
  function w:GetParent() return self.parent end
  function w:Show() self.shown = true end
  function w:Hide() self.shown = false end
  function w:IsShown() return self.shown end
  function w:SetHeight(h) self.height = h end
  function w:GetHeight() return self.height end
  function w:SetWidth() end
  function w:SetSize(a, b) self.height = b or 0 end
  function w:SetAlpha() end
  function w:SetUserPlaced() end
  function w:StartMoving() end
  function w:StopMovingOrSizing() end
  function w:GetCenter() return 0, 0 end
  function w:SetResizable() end
  function w:SetShown(v) self.shown = v and true or false end
  function w:SetText(t) self.text = tostring(t) end
  function w:GetText() return self.text end
  function w:GetStringHeight() return 12 end
  function w:SetTextColor(r, g, b) self.color = {r, g, b} end
  function w:SetFontObject() end
  function w:SetJustifyH() end
  function w:SetStatusBarTexture() end
  function w:SetStatusBarColor() end
  function w:SetMinMaxValues() end
  function w:SetValue(v) self.value = v end
  function w:SetColorTexture() end
  function w:SetBackdrop() end
  function w:SetMovable() end
  function w:EnableMouse() end
  function w:RegisterForDrag() end
  function w:SetScript() end
  function w:SetClampedToScreen() end
  function w:SetFrameStrata() end
  function w:SetToplevel() end
  function w:RegisterEvent() end
  function w:UnregisterEvent() end
  function w:GetPoint() return self.points[1] and self.points[1].p end
  function w:CreateFontString() local fs = NewWidget("FontString", self); table.insert(widgets, fs); return fs end
  function w:CreateTexture() local t = NewWidget("Texture", self); table.insert(widgets, t); return t end
  -- Any widget method this harness hasn't modelled is a no-op; only the
  -- ones above return meaningful values.
  setmetatable(w, {__index = function(_, k)
    if type(k) == "string" and k:match("^%u") then return function() end end
    return nil
  end})
  table.insert(widgets, w)
  return w
end
function CreateFrame(kind, name, parent) return NewWidget(kind or "Frame", parent) end
UIParent = NewWidget("Frame")
GameFontNormalLarge, GameFontHighlightSmall, GameFontHighlight = {}, {}, {}
GetTime = function() return 1000 end
time = os.time

local db = {
  showChestTimer = true, showBossSplits = true, showPullProgress = true,
  showInterrupts = true, tagDeaths = true,
  overlayPosition = {point = "CENTER", relativePoint = "CENTER", x = 0, y = 0},
}
local MA = {
  GetDB = function() return db end,
  Debug = function() end,
  SavePosition = function() end,
  RegisterKeyEventFrame = function() end,
  INFO = {recapLine = "Companion app: postmortem-mplus.fly.dev"},
  state = {},
}

local chunk = assert(loadfile("addon/Postmortem/Overlay.lua"))
chunk("Postmortem", MA)

-- A live key: 30:00 limit, 12:47 elapsed.
MA.state = {
  active = true,
  elapsed = 767,
  forces = {current = 142, total = 265, percent = 53.6},
  deaths = 1, deathTimeLost = 15,
  interrupts = {total = 8},
  chestTimer = {timeLimit = 1800, t2 = 1440, t3 = 1080},
}
-- The real formatter ChestTimer.lua publishes, so the +2/+3 row renders
-- exactly as it will in game.
MA.ChestTimer_FormatSigned = function(seconds)
  seconds = seconds or 0
  local sign = seconds < 0 and "-" or ""
  local whole = math.floor(math.abs(seconds))
  return string.format("%s%02d:%02d", sign, math.floor(whole / 60), whole % 60)
end
local before = #widgets
MA:Overlay_Refresh()
-- The overlay frame is the first widget CreateOverlayFrame() builds.
local f = widgets[before + 1]
assert(f and rawget(f, "timerFS"), "overlay frame not found")

assert(f.timerFS:GetText() == "17:13", "countdown wrong: " .. f.timerFS:GetText())
assert(f.elapsedFS:GetText() == "12:47", "elapsed wrong: " .. f.elapsedFS:GetText())
assert(f.elapsedFS.shown, "elapsed should show when a limit is known")
assert(f.chestTimerFS:GetText() == "+2 11:13   +3 05:13",
  "chest row wrong: " .. f.chestTimerFS:GetText())
assert(f.chestTimerFS.shown, "chest row should show")

-- With ChestTimer.lua absent, Overlay.lua's own copy of the formatter
-- still renders the overall countdown (the +2/+3 row simply drops out).
local saved = MA.ChestTimer_FormatSigned
MA.ChestTimer_FormatSigned = nil
MA:Overlay_Refresh()
assert(f.timerFS:GetText() == "17:13", "countdown without ChestTimer.lua: " .. f.timerFS:GetText())
assert(f.chestTimerFS.shown == false, "+2/+3 row needs ChestTimer.lua")
MA.ChestTimer_FormatSigned = saved
MA:Overlay_Refresh()

-- Row order: walk each visible row's TOP anchor back to the top anchor.
local function anchorOf(w)
  for _, pt in ipairs(w.points) do if pt.p == "TOP" then return pt.rel end end
end
local order, seen = {}, {}
local names = {
  [f.timerRow] = "timerRow", [f.chestTimerFS] = "chestTimer", [f.forcesBar] = "forcesBar",
  [f.statsRow] = "statsRow", [f.splitFS] = "split", [f.pullFS] = "pull",
  [f.statusFS] = "status", [f.deathCauseFS] = "deathCause", [f.companionFS] = "companion",
  [f.topAnchor] = "topAnchor",
}
local node = f.topAnchor
for _ = 1, 20 do
  local nextNode
  for w, nm in pairs(names) do
    if not seen[w] and w.shown and anchorOf(w) == node then nextNode = w; break end
  end
  if not nextNode then break end
  seen[nextNode] = true
  table.insert(order, names[nextNode])
  node = nextNode
end
print("row order: " .. table.concat(order, " -> "))
assert(order[1] == "timerRow", "overall dungeon time must be first")
assert(order[2] == "chestTimer", "+2/+3 must follow the overall time")
assert(order[3] == "forcesBar", "forces count/percent must follow the chest timers")
-- Full-width rows keep their sides after reflow.
for _, w in ipairs({f.timerRow, f.forcesBar, f.statsRow}) do
  local l, r = false, false
  for _, pt in ipairs(w.points) do
    if pt.p == "LEFT" then l = true elseif pt.p == "RIGHT" then r = true end
  end
  assert(l and r, "a wide row lost its side anchors in reflow")
end

-- Overtime: the countdown goes negative and red.
MA.state.elapsed = 1900
MA:Overlay_Refresh()
assert(f.timerFS:GetText() == "-01:40", "overtime wrong: " .. f.timerFS:GetText())
assert(f.timerFS.color[1] == 1.0 and f.timerFS.color[2] == 0.3, "overtime should be red")

-- No resolved time limit: fall back to elapsed, hide the duplicate.
MA.state.elapsed = 767
MA.state.chestTimer = nil
MA:Overlay_Refresh()
assert(f.timerFS:GetText() == "12:47", "fallback wrong: " .. f.timerFS:GetText())
assert(f.elapsedFS.shown == false, "elapsed must hide when it is the only figure shown")

print("ok: countdown, overtime, fallback and row order all correct")

-- A fuller state: splits, a loaded route, and the post-key recap window,
-- so every optional row below the header is visible at once.
MA.state.chestTimer = {timeLimit = 1800, t2 = 1440, t3 = 1080}
MA.state.splits = {{name = "Second Boss", delta = -12}}
MA.state.route = {plannedPulls = {1, 2, 3}, currentPullIndex = 2}
MA.state.active = false
MA.state.recapUntil = GetTime() + 30
MA.state.combatLogWasOn = true
MA.state.lastDeathCause = {name = "Fingers of Gul'dan", avoidable = true}
MA:Overlay_Refresh()

local order2, seen2 = {}, {}
local node2 = f.topAnchor
for _ = 1, 20 do
  local nextNode
  for w, nm in pairs(names) do
    if not seen2[w] and w.shown and anchorOf(w) == node2 then nextNode = w; break end
  end
  if not nextNode then break end
  seen2[nextNode] = true
  table.insert(order2, names[nextNode])
  node2 = nextNode
end
print("recap row order: " .. table.concat(order2, " -> "))
local expected = "timerRow -> chestTimer -> forcesBar -> statsRow -> split -> pull -> status -> deathCause -> companion"
assert(table.concat(order2, " -> ") == expected, "recap order wrong")
print("ok: every optional row reflows in order below the header")
