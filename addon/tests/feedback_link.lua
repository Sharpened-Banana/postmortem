-- Info.lua: the "Send feedback" link.
--
-- An addon can neither open a browser nor send anything, so its feedback
-- button hands the player a link to the site's form to copy. What is
-- worth pinning is the link itself: it must name the addon and its
-- version (so the form can say which build the feedback is about), it
-- must never carry a character that breaks a pasted URL, and the popup
-- must actually be given it.
--
-- Run from the repo root:
--
--     lua addon/tests/feedback_link.lua
--
-- See addon/tests/README.md for how these harnesses work.

local shown = {}
StaticPopupDialogs = {}
function StaticPopup_Show(which, _, _, data) table.insert(shown, { which = which, data = data }) end
function StaticPopup_StandardEditBoxOnEscapePressed() end
CLOSE = "Close"
function CreateFrame()
  local f = {}
  function f:RegisterEvent() end
  function f:UnregisterEvent() end
  function f:SetScript() end
  return f
end

local tocVersion
C_AddOns = { GetAddOnMetadata = function() return tocVersion end }

local MA = {}
function MA:GetDB() return {} end
assert(loadfile("addon/Postmortem/Info.lua"))("Postmortem", MA)

local failures = 0
local function check(label, got, want)
  if got ~= want then
    failures = failures + 1
    print(("FAIL %s\n  got:  %s\n  want: %s"):format(label, tostring(got), tostring(want)))
  end
end

local BASE = "https://postmortem-mplus.fly.dev/feedback?source=addon"

tocVersion = "0.3.4"
check("version is attached", MA:Info_FeedbackURL(), BASE .. "&version=0.3.4")

-- Whatever the .toc says, nothing that could break or extend a pasted
-- URL survives.
tocVersion = "@project-version@ <b>&x=1"
check("unsafe characters are dropped", MA:Info_FeedbackURL(), BASE .. "&version=project-versionbx1")

tocVersion = nil
check("no version, no parameter", MA:Info_FeedbackURL(), BASE)

C_AddOns = nil
check("client without C_AddOns", MA:Info_FeedbackURL(), BASE)

MA:Info_ShowFeedbackPopup()
check("popup shown", shown[1] and shown[1].which, "POSTMORTEM_COPY_FEEDBACK_URL")
check("popup is given the link", shown[1] and shown[1].data, BASE)

local dialog = StaticPopupDialogs["POSTMORTEM_COPY_FEEDBACK_URL"]
check("dialog has an edit box to copy from", dialog and dialog.hasEditBox, 1)
check("dialog tells the player what to do with it",
  dialog and dialog.text:find("paste it into your browser", 1, true) ~= nil, true)
check("the download popup keeps its own wording",
  StaticPopupDialogs["POSTMORTEM_COPY_URL"].text, "Press Ctrl+C to copy")

if failures > 0 then
  print(failures .. " failure(s)")
  os.exit(1)
end
print("feedback_link.lua: ok")
