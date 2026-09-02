"""Small static game-data tables: specs, bloodlust and battle-res spells."""

from __future__ import annotations

from typing import Optional

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
    # Devourer: the third Demon Hunter spec (Midnight). ID confirmed from a
    # real 2026-09 log's COMBATANT_INFO, spec name from its own kit
    # (Devour, Consume, Void Ray, Reap, Voidblade, Soul Immolation) --
    # every cast is a damage ability, so dps.
    1480: ("Demon Hunter", "Devourer", "dps"),
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


# Personal defensive COOLDOWNS: immunities, big damage-reduction cooldowns,
# and similar "this should have saved me" abilities. Not trinkets, potions,
# or passive mitigation -- those aren't spec-bound choices the way a
# cooldown is.
#
# spell_id -> (name, spec_ids)
# spec_ids is a tuple of spec ids (see SPECS above) this defensive belongs
# to. The type also allows None ("available to any spec/class", e.g. a
# racial) per WP-A3's spec, but this table deliberately has NO None-typed
# entries: stats._tag_death_defensives treats "at least one DEFENSIVES
# entry applies to this spec" as its signal that we can make a real claim
# about died_without_defensive. A spec-agnostic entry would make that
# always true for every *known* spec_id, which would defeat the safety
# fallback for a spec this table genuinely doesn't cover (e.g. Evoker,
# below) -- those should honestly report "we don't know" (None) rather
# than a false "died without a defensive" (True). A future WP that also
# tracks player race could reintroduce racials with its own guard.
#
# Correctness over completeness: this is a representative sample (the
# specs exercised by tests/conftest.py, plus a handful of other well-known
# ones), not an exhaustive list. Every spell id below is one I'm confident
# is accurate; anywhere I was less sure, that's called out in its own
# comment rather than presented as fact.
DEFENSIVES: dict[int, tuple[str, Optional[tuple[int, ...]]]] = {
    # -- Paladin: 65 Holy, 66 Protection, 70 Retribution --
    642: ("Divine Shield", (65, 66, 70)),
    31850: ("Ardent Defender", (66,)),  # Protection-only

    # -- Shaman: 262 Elemental, 263 Enhancement, 264 Restoration --
    108271: ("Astral Shift", (262, 263, 264)),

    # -- Mage: 62 Arcane, 63 Fire, 64 Frost --
    45438: ("Ice Block", (62, 63, 64)),

    # -- Warrior --
    871: ("Shield Wall", (73,)),  # Protection-only
    # Arms defensive cooldown; id believed correct but not independently
    # re-verified against a current client for this WP.
    118038: ("Die by the Sword", (71,)),

    # -- Rogue: 259 Assassination, 260 Outlaw, 261 Subtlety --
    31224: ("Cloak of Shadows", (259, 260, 261)),

    # -- Death Knight: 250 Blood, 251 Frost, 252 Unholy --
    48792: ("Icebound Fortitude", (250, 251, 252)),
    48707: ("Anti-Magic Shell", (250, 251, 252)),
    55233: ("Vampiric Blood", (250,)),  # Blood-specific

    # -- Priest --
    33206: ("Pain Suppression", (256,)),  # Discipline
    47788: ("Guardian Spirit", (257,)),  # Holy

    # -- Demon Hunter --
    198589: ("Blur", (577,)),  # Havoc
    196718: ("Darkness", (577, 581)),  # class-wide raid utility, both specs
    # Vengeance's defensive cooldown -- distinct from Havoc's offensive
    # Metamorphosis (spell id 191427, not included here).
    187827: ("Metamorphosis", (581,)),

    # -- Warlock: 265 Affliction, 266 Demonology, 267 Destruction --
    104773: ("Unending Resolve", (265, 266, 267)),

    # -- Hunter: 253 Beast Mastery, 254 Marksmanship, 255 Survival --
    186265: ("Aspect of the Turtle", (253, 254, 255)),

    # -- Druid: 102 Balance, 103 Feral, 104 Guardian, 105 Restoration --
    22812: ("Barkskin", (102, 103, 104, 105)),
    61336: ("Survival Instincts", (103, 104)),  # Feral/Guardian

    # -- Monk: 268 Brewmaster, 269 Windwalker, 270 Mistweaver --
    115203: ("Fortifying Brew", (268, 269, 270)),

    # Evoker (1467/1468/1473) is intentionally NOT covered -- see the
    # note above; a death for an Evoker correctly reports
    # died_without_defensive = None rather than a guessed True.
}


# Hard crowd-control effects: the kind of thing worth asking "was that
# caster ever CC'd" about in a M+ post-mortem -- incapacitates, roots,
# stuns, fears, banishes. Deliberately NOT interrupts (already tracked
# separately, see stats.enemy_cast_outcomes) and NOT generic slows/snares
# (a slow isn't "control" in the sense this stat cares about -- the target
# can still act). Applied *by* the group *onto* a hostile target is the
# only direction tracked (stats.compute_stats gates on is_hostile_npc(dst)).
#
# spell_id -> (name, cc_type). cc_type is a coarse label for grouping in a
# report, not a mechanical distinction the code itself branches on.
#
# Correctness over completeness, same discipline as DEFENSIVES above: a
# representative sample of spells I'm confident are accurate, not an
# exhaustive list -- a CC spell missing from this table simply doesn't
# count towards CC uptime, it's never misreported as something else.
CC_SPELLS: dict[int, tuple[str, str]] = {
    # -- Mage --
    118: ("Polymorph", "incapacitate"),
    28271: ("Polymorph", "incapacitate"),  # Turtle
    28272: ("Polymorph", "incapacitate"),  # Pig
    61305: ("Polymorph", "incapacitate"),  # Black Cat
    61780: ("Polymorph", "incapacitate"),  # Turkey
    61721: ("Polymorph", "incapacitate"),  # Rabbit
    82691: ("Ring of Frost", "incapacitate"),
    122: ("Frost Nova", "root"),

    # -- Rogue --
    6770: ("Sap", "incapacitate"),
    2094: ("Blind", "incapacitate"),
    1776: ("Gouge", "incapacitate"),

    # -- Hunter --
    3355: ("Freezing Trap", "incapacitate"),
    19386: ("Wyvern Sting", "incapacitate"),

    # -- Paladin --
    20066: ("Repentance", "incapacitate"),
    853: ("Hammer of Justice", "stun"),

    # -- Priest --
    9484: ("Shackle Undead", "incapacitate"),
    8122: ("Psychic Scream", "fear"),
    605: ("Mind Control", "incapacitate"),

    # -- Warlock --
    710: ("Banish", "incapacitate"),
    6358: ("Seduction", "incapacitate"),  # Succubus
    5782: ("Fear", "fear"),
    118699: ("Fear", "fear"),  # Howl of Terror

    # -- Druid --
    2637: ("Hibernate", "incapacitate"),
    339: ("Entangling Roots", "root"),
    5211: ("Mighty Bash", "stun"),
    99: ("Incapacitating Roar", "incapacitate"),

    # -- Shaman --
    51514: ("Hex", "incapacitate"),
    211015: ("Hex", "incapacitate"),  # Cockroach
    211010: ("Hex", "incapacitate"),  # Snake
    211004: ("Hex", "incapacitate"),  # Spider

    # -- Death Knight --
    47476: ("Strangulate", "silence"),
    108194: ("Asphyxiate", "stun"),

    # -- Monk --
    115078: ("Paralysis", "incapacitate"),

    # -- Demon Hunter --
    179057: ("Chaos Nova", "stun"),
    217832: ("Imprison", "incapacitate"),

    # -- Warrior --
    5246: ("Intimidating Shout", "fear"),

    # -- Evoker --
    360806: ("Sleep Walk", "incapacitate"),
}


def spec_info(spec_id: int | None) -> tuple[str | None, str | None, str | None]:
    if spec_id is None:
        return None, None, None
    entry = SPECS.get(spec_id)
    if entry is None:
        return None, None, None
    return entry
