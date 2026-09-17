-- Results.lua offline check: the in-game results window's "Snapshots"
-- block. The desktop app writes PostmortemResults.lua with a `snapshots`
-- list of headlines (addon_results.py's build_results_payload); the window
-- must list them as "t  role -- line", cap the block, and take no space at
-- all when the run had none (the honest empty state). Only the text
-- builder is exercised: the frame itself needs a live client.
local function NewWidget()
  local w = {}
  setmetatable(w, {__index = function(_, k)
    if type(k) == "string" and k:match("^%u") then return function() end end
    return nil
  end})
  return w
end
function CreateFrame() return NewWidget() end
UIParent = NewWidget()
GameFontNormal, GameFontHighlight, GameFontHighlightSmall, GameFontDisableSmall = {}, {}, {}, {}
UISpecialFrames = {}
CLOSE = "Close"
tinsert = table.insert

local MA = { db = {}, GetDB = function(self) return self.db end, Debug = function() end }
local chunk = assert(loadfile("addon/Postmortem/Results.lua"))
chunk("Postmortem", MA)

local failures = 0
local function check(cond, msg)
  if not cond then
    failures = failures + 1
    print("FAIL: " .. msg)
  end
end

local build = MA.Results_BuildSnapshotLines
check(type(build) == "function", "Results.lua exposes the snapshot line builder")

-- honest empty state: no key, an empty list, or a non-table all give nil
check(build({}) == nil, "no snapshots key -> nil (section takes no height)")
check(build({ snapshots = {} }) == nil, "empty snapshots -> nil")
check(build({ snapshots = "nope" }) == nil, "non-table snapshots -> nil")

-- the normal case: one line per headline, time and role first
local text = build({ snapshots = {
  { n = 1, role = "healer", t = "1:06", line = "1.2M effective healing, 20% overheal" },
  { n = 2, role = "tank", t = "1:50", line = "peak 45.0k DTPS" },
} })
check(text ~= nil and text:sub(1, 10) == "Snapshots:", "block starts with its heading")
check(text:find("  1:06  healer -- 1.2M effective healing, 20% overheal", 1, true) ~= nil,
  "healer line reads 't  role -- line'")
check(text:find("  1:50  tank -- peak 45.0k DTPS", 1, true) ~= nil, "tank line present")
check(select(2, text:gsub("\n", "")) == 2, "exactly one line per snapshot plus the heading")

-- a headline without a line still renders rather than erroring
local bare = build({ snapshots = { { role = "general", t = "3:00" } } })
check(bare:find("3:00  general -- (no summary)", 1, true) ~= nil, "missing line degrades to a placeholder")

-- the block is capped so a marker-happy run cannot grow the window unbounded
local many = {}
for i = 1, 9 do many[i] = { role = "healer", t = string.format("%d:00", i), line = "x" } end
local capped = build({ snapshots = many })
check(select(2, capped:gsub("\n", "")) == 7, "six lines plus heading plus the overflow note")
check(capped:find("(+3 more in the desktop app)", 1, true) ~= nil, "overflow note names the remainder")

if failures > 0 then
  print(failures .. " check(s) failed")
  os.exit(1)
end
print("results_window.lua: all checks passed")
