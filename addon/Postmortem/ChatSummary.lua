-- ChatSummary.lua
-- Posts a one-line completion summary to party/instance chat when a key
-- finishes -- zone, level, chest verdict with margin, deaths, and a pointer
-- to /pm results. Never fires on CHALLENGE_MODE_RESET (an abandoned key
-- isn't worth announcing).
--
-- Channel selection verified this session against
-- AngryKeystones/Gossip.lua:42-52's real, currently-shipping three-way
-- fallback: LE_PARTY_CATEGORY_INSTANCE must be checked before
-- LE_PARTY_CATEGORY_HOME, since an LFG/instance group is in BOTH
-- categories but only INSTANCE_CHAT actually reaches that party. No SAY
-- fallback (unlike AngryKeystones) -- a Mythic+ key finished solo, with no
-- group to hear a party/instance message, isn't worth announcing to
-- general chat. C_ChatInfo.SendChatMessage (the namespaced form) is used
-- rather than the bare global, matching what currently-maintained addons
-- (EnhanceQoLDungeonRaid, KeystoneLoot) use post-12.0.
--
-- Known, accepted limitation: if multiple party members run Postmortem,
-- each posts their own copy of this message. Every real addon surveyed in
-- this space (AngryKeystones included) has the same behavior; cross-client
-- dedup isn't worth the complexity for this feature.

local ADDON_NAME, MA = ...

local function SendToGroup(msg)
  if IsInGroup(LE_PARTY_CATEGORY_INSTANCE) then
    C_ChatInfo.SendChatMessage(msg, "INSTANCE_CHAT")
  elseif IsInGroup(LE_PARTY_CATEGORY_HOME) then
    C_ChatInfo.SendChatMessage(msg, "PARTY")
  end
end

local function BuildMessage()
  local state = MA.state or {}
  local ct = state.chestTimer or {}
  local run = state or {}

  local zone = "this dungeon"
  if C_ChallengeMode.GetMapUIInfo and ct.mapID then
    local name = C_ChallengeMode.GetMapUIInfo(ct.mapID)
    if name then zone = name end
  end
  local level = ct.level and ("+" .. ct.level) or ""

  local elapsed = run.elapsed or 0
  local verdict
  if ct.timeLimit then
    if MA.ChestTimer_FormatSigned and ct.t3 and elapsed <= ct.t3 then
      verdict = string.format("Beat the timer for +3 in %s.", MA.ChestTimer_FormatSigned(elapsed))
    elseif MA.ChestTimer_FormatSigned and ct.t2 and elapsed <= ct.t2 then
      verdict = string.format("Beat the timer for +2 in %s (missed +3 by %s).",
        MA.ChestTimer_FormatSigned(elapsed), MA.ChestTimer_FormatSigned(elapsed - ct.t3))
    elseif elapsed <= ct.timeLimit then
      verdict = string.format("Timed in %s (missed +2 by %s).",
        MA.ChestTimer_FormatSigned(elapsed), MA.ChestTimer_FormatSigned(elapsed - ct.t2))
    else
      verdict = string.format("Over time by %s.", MA.ChestTimer_FormatSigned(elapsed - ct.timeLimit))
    end
  else
    verdict = "Completed."
  end

  local deaths = run.deaths or 0
  local deathsPart = ""
  if deaths > 0 then
    deathsPart = string.format(" %d death(s), %.0fs lost.", deaths, run.deathTimeLost or 0)
  end

  return string.format("[Postmortem] %s %s -- %s%s Full breakdown: /pm results",
    zone, level, verdict, deathsPart)
end

local eventFrame = CreateFrame("Frame")
MA:RegisterKeyEventFrame(eventFrame)
eventFrame:RegisterEvent("CHALLENGE_MODE_COMPLETED")

eventFrame:SetScript("OnEvent", function(self, event, ...)
  if event ~= "CHALLENGE_MODE_COMPLETED" then return end
  if not MA:GetDB().announceCompletion then return end
  local ok, msg = pcall(BuildMessage)
  if not ok then
    MA:Debug("ChatSummary: failed to build message (%s)", tostring(msg))
    return
  end
  SendToGroup(msg)
end)
