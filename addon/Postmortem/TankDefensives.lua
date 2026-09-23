-- TankDefensives.lua
--
-- GENERATED FILE -- DO NOT EDIT BY HAND.
-- Source: src/postmortem/data/tank_defensives.json
-- Regenerate: python3 scripts/build_tank_defensives_lua.py
--
-- The per-spec tank defensive table shared by the live addon module
-- (TankDeath.lua) and the Python analyzer (analysis/tank_death.py). Editing
-- this file directly would let the two drift apart, which is the one thing
-- generating it is meant to prevent -- edit the JSON and re-run the script.
--
-- Shape:
--   MA.TankDefensives.bySpec[specID] = {
--     { id, name, category, cooldown, duration, charges }, ...
--   }
--   MA.TankDefensives.externals[specID] = { { id, name, cooldown }, ... }
--   MA.TankDefensives.tankSpecs[specID] = true
--
-- `cooldown` of 0 means resource-gated (rage/Holy Power/runes): tracked for
-- usage, never scored as "available and unused", because the client's own
-- cooldown API says nothing about whether the resource was there. See
-- analysis/tank_death.py's Defensive.scorable for the same rule.

local ADDON_NAME, MA = ...

MA.TankDefensives = MA.TankDefensives or {}
local T = MA.TankDefensives

T.bySpec = {
  [66] = {
    { id = 53600, name = "Shield of the Righteous", category = "active_mitigation", cooldown = 0.0, duration = 4.5, charges = 3 },
    { id = 633, name = "Lay on Hands", category = "heal", cooldown = 600.0, duration = 0.0, charges = 1 },
    { id = 85673, name = "Word of Glory", category = "heal", cooldown = 0.0, duration = 0.0, charges = 1 },
    { id = 642, name = "Divine Shield", category = "immunity", cooldown = 300.0, duration = 8.0, charges = 1 },
    { id = 31850, name = "Ardent Defender", category = "major", cooldown = 120.0, duration = 8.0, charges = 1 },
    { id = 498, name = "Divine Protection", category = "major", cooldown = 60.0, duration = 8.0, charges = 1 },
    { id = 86659, name = "Guardian of Ancient Kings", category = "major", cooldown = 300.0, duration = 8.0, charges = 1 },
  },
  [73] = {
    { id = 190456, name = "Ignore Pain", category = "active_mitigation", cooldown = 0.0, duration = 12.0, charges = 1 },
    { id = 2565, name = "Shield Block", category = "active_mitigation", cooldown = 0.0, duration = 6.0, charges = 2 },
    { id = 1160, name = "Demoralizing Shout", category = "major", cooldown = 45.0, duration = 8.0, charges = 1 },
    { id = 12975, name = "Last Stand", category = "major", cooldown = 180.0, duration = 15.0, charges = 1 },
    { id = 871, name = "Shield Wall", category = "major", cooldown = 240.0, duration = 8.0, charges = 1 },
    { id = 23920, name = "Spell Reflection", category = "major", cooldown = 25.0, duration = 5.0, charges = 1 },
  },
  [104] = {
    { id = 22842, name = "Frenzied Regeneration", category = "active_mitigation", cooldown = 0.0, duration = 3.0, charges = 2 },
    { id = 192081, name = "Ironfur", category = "active_mitigation", cooldown = 0.0, duration = 7.0, charges = 1 },
    { id = 22812, name = "Barkskin", category = "major", cooldown = 60.0, duration = 12.0, charges = 1 },
    { id = 102558, name = "Incarnation: Guardian of Ursoc", category = "major", cooldown = 180.0, duration = 30.0, charges = 1 },
    { id = 200851, name = "Rage of the Sleeper", category = "major", cooldown = 90.0, duration = 10.0, charges = 1 },
    { id = 61336, name = "Survival Instincts", category = "major", cooldown = 180.0, duration = 6.0, charges = 2 },
  },
  [250] = {
    { id = 49998, name = "Death Strike", category = "active_mitigation", cooldown = 0.0, duration = 0.0, charges = 1 },
    { id = 194679, name = "Rune Tap", category = "active_mitigation", cooldown = 0.0, duration = 4.0, charges = 2 },
    { id = 48707, name = "Anti-Magic Shell", category = "major", cooldown = 60.0, duration = 10.0, charges = 1 },
    { id = 49028, name = "Dancing Rune Weapon", category = "major", cooldown = 120.0, duration = 8.0, charges = 1 },
    { id = 48792, name = "Icebound Fortitude", category = "major", cooldown = 180.0, duration = 8.0, charges = 1 },
    { id = 55233, name = "Vampiric Blood", category = "major", cooldown = 90.0, duration = 10.0, charges = 1 },
  },
  [268] = {
    { id = 119582, name = "Purifying Brew", category = "active_mitigation", cooldown = 20.0, duration = 0.0, charges = 2 },
    { id = 322507, name = "Celestial Brew", category = "major", cooldown = 60.0, duration = 8.0, charges = 1 },
    { id = 122278, name = "Dampen Harm", category = "major", cooldown = 120.0, duration = 10.0, charges = 1 },
    { id = 122783, name = "Diffuse Magic", category = "major", cooldown = 90.0, duration = 6.0, charges = 1 },
    { id = 115203, name = "Fortifying Brew", category = "major", cooldown = 420.0, duration = 15.0, charges = 1 },
    { id = 115176, name = "Zen Meditation", category = "major", cooldown = 300.0, duration = 8.0, charges = 1 },
  },
  [581] = {
    { id = 203720, name = "Demon Spikes", category = "active_mitigation", cooldown = 20.0, duration = 6.0, charges = 2 },
    { id = 212084, name = "Fel Devastation", category = "heal", cooldown = 60.0, duration = 2.0, charges = 1 },
    { id = 196718, name = "Darkness", category = "major", cooldown = 300.0, duration = 8.0, charges = 1 },
    { id = 204021, name = "Fiery Brand", category = "major", cooldown = 60.0, duration = 10.0, charges = 1 },
    { id = 187827, name = "Metamorphosis", category = "major", cooldown = 180.0, duration = 15.0, charges = 1 },
  },
}

T.externals = {
  [65] = {
    { id = 1022, name = "Blessing of Protection", cooldown = 300.0, duration = 10.0 },
    { id = 6940, name = "Blessing of Sacrifice", cooldown = 120.0, duration = 12.0 },
  },
  [66] = {
    { id = 1022, name = "Blessing of Protection", cooldown = 300.0, duration = 10.0 },
    { id = 6940, name = "Blessing of Sacrifice", cooldown = 120.0, duration = 12.0 },
  },
  [70] = {
    { id = 1022, name = "Blessing of Protection", cooldown = 300.0, duration = 10.0 },
    { id = 6940, name = "Blessing of Sacrifice", cooldown = 120.0, duration = 12.0 },
  },
  [71] = {
    { id = 97462, name = "Rallying Cry", cooldown = 180.0, duration = 10.0 },
  },
  [72] = {
    { id = 97462, name = "Rallying Cry", cooldown = 180.0, duration = 10.0 },
  },
  [73] = {
    { id = 97462, name = "Rallying Cry", cooldown = 180.0, duration = 10.0 },
  },
  [105] = {
    { id = 102342, name = "Ironbark", cooldown = 90.0, duration = 12.0 },
  },
  [250] = {
    { id = 51052, name = "Anti-Magic Zone", cooldown = 120.0, duration = 10.0 },
  },
  [251] = {
    { id = 51052, name = "Anti-Magic Zone", cooldown = 120.0, duration = 10.0 },
  },
  [252] = {
    { id = 51052, name = "Anti-Magic Zone", cooldown = 120.0, duration = 10.0 },
  },
  [256] = {
    { id = 33206, name = "Pain Suppression", cooldown = 180.0, duration = 8.0 },
    { id = 62618, name = "Power Word: Barrier", cooldown = 180.0, duration = 10.0 },
  },
  [257] = {
    { id = 47788, name = "Guardian Spirit", cooldown = 180.0, duration = 10.0 },
  },
  [264] = {
    { id = 98008, name = "Spirit Link Totem", cooldown = 180.0, duration = 6.0 },
  },
  [270] = {
    { id = 116849, name = "Life Cocoon", cooldown = 120.0, duration = 12.0 },
  },
}

-- Spec ids that are tanks, so a module can cheaply ask "is the player a
-- tank" without walking the table.
T.tankSpecs = {
  [66] = true,
  [73] = true,
  [104] = true,
  [250] = true,
  [268] = true,
  [581] = true,
}

-- Display names, for the overlay's own labelling.
T.specNames = {
  [66] = "Protection Paladin",
  [73] = "Protection Warrior",
  [104] = "Guardian Druid",
  [250] = "Blood Death Knight",
  [268] = "Brewmaster Monk",
  [581] = "Vengeance Demon Hunter",
}
