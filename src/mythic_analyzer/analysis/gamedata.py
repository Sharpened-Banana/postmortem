"""Small static game-data tables: specs, bloodlust and battle-res spells."""

from __future__ import annotations

# spec_id -> (class, spec, role)
SPECS: dict[int, tuple[str, str, str]] = {
    62: ("Mage", "Arcane", "dps"), 63: ("Mage", "Fire", "dps"), 64: ("Mage", "Frost", "dps"),
    65: ("Paladin", "Holy", "healer"), 66: ("Paladin", "Protection", "tank"),
    70: ("Paladin", "Retribution", "dps"),
    71: ("Warrior", "Arms", "dps"), 72: ("Warrior", "Fury", "dps"),
    73: ("Warrior", "Protection", "tank"),
    102: ("Druid", "Balance", "dps"), 103: ("Druid", "Feral", "dps"),
    104: ("Druid", "Guardian", "tank"), 105: ("Druid", "Restoration", "healer"),
    250: ("Death Knight", "Blood", "tank"), 251: ("Death Knight", "Frost", "dps"),
    252: ("Death Knight", "Unholy", "dps"),
    253: ("Hunter", "Beast Mastery", "dps"), 254: ("Hunter", "Marksmanship", "dps"),
    255: ("Hunter", "Survival", "dps"),
    256: ("Priest", "Discipline", "healer"), 257: ("Priest", "Holy", "healer"),
    258: ("Priest", "Shadow", "dps"),
    259: ("Rogue", "Assassination", "dps"), 260: ("Rogue", "Outlaw", "dps"),
    261: ("Rogue", "Subtlety", "dps"),
    262: ("Shaman", "Elemental", "dps"), 263: ("Shaman", "Enhancement", "dps"),
    264: ("Shaman", "Restoration", "healer"),
    265: ("Warlock", "Affliction", "dps"), 266: ("Warlock", "Demonology", "dps"),
    267: ("Warlock", "Destruction", "dps"),
    268: ("Monk", "Brewmaster", "tank"), 269: ("Monk", "Windwalker", "dps"),
    270: ("Monk", "Mistweaver", "healer"),
    577: ("Demon Hunter", "Havoc", "dps"), 581: ("Demon Hunter", "Vengeance", "tank"),
    1467: ("Evoker", "Devastation", "dps"), 1468: ("Evoker", "Preservation", "healer"),
    1473: ("Evoker", "Augmentation", "dps"),
}

# Bloodlust-type effects
LUST_SPELLS: dict[int, str] = {
    2825: "Bloodlust",
    32182: "Heroism",
    80353: "Time Warp",
    264667: "Primal Rage",
    272678: "Primal Rage",
    390386: "Fury of the Aspects",
    444257: "Thorim's Might",
    466904: "Harrier's Cry",
    309658: "Drums of Deathly Ferocity",
    381301: "Feral Hide Drums",
    230935: "Drums of the Mountain",
}

# Battle resurrection casts
BREZ_SPELLS: dict[int, str] = {
    20484: "Rebirth",
    61999: "Raise Ally",
    20707: "Soulstone",
    391054: "Intercession",
    345130: "Disposable Spectrophasic Reanimator",
}


def spec_info(spec_id: int | None) -> tuple[str | None, str | None, str | None]:
    if spec_id is None:
        return None, None, None
    entry = SPECS.get(spec_id)
    if entry is None:
        return None, None, None
    return entry
