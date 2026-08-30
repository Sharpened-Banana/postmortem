"""Shared fixtures: a synthetic dungeon, MDT route and combat log."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mythic_analyzer.mdt.decode import encode_mdt_string  # noqa: E402

# --- synthetic dungeon ------------------------------------------------------

FELWYRM = 236085
DUSKBLADE = 236086
BOSS = 236087
SHADELING = 236088  # in dungeon data but not in the route (off-route pack)
SUMMONED = 990001   # not in dungeon data at all (untracked add)

DUNGEON_DATA = {
    "source": "synthetic",
    "generated_at": "2026-08-30T00:00:00",
    "dungeons": {
        "160": {
            "dungeon_idx": 160,
            "name": "Murder Row",
            "short_name": "MR",
            "map_id": 587,
            "zone_ids": [2433, 2434, 2435],
            "total_count": {"normal": 100},
            "enemies": [
                {"enemy_idx": 1, "id": FELWYRM, "name": "Felwyrm", "count": 4,
                 "clones": [{"x": 0, "y": 0, "g": 1, "sublevel": 1}] * 4},
                {"enemy_idx": 2, "id": DUSKBLADE, "name": "Duskblade", "count": 6,
                 "clones": [{"x": 1, "y": 1, "g": 2, "sublevel": 1}] * 3},
                {"enemy_idx": 3, "id": BOSS, "name": "Big Boss", "count": 0,
                 "is_boss": True,
                 "clones": [{"x": 2, "y": 2, "sublevel": 1}]},
                {"enemy_idx": 4, "id": SHADELING, "name": "Shadeling", "count": 2,
                 "clones": [{"x": 3, "y": 3, "g": 3, "sublevel": 1}] * 4},
            ],
        }
    },
}

# plan: 1) 2x Felwyrm  2) 2x Duskblade  3) 1x Felwyrm (never pulled)  4) boss
ROUTE_PRESET = {
    "text": "Test MR Route",
    "week": 1,
    "difficulty": 10,
    "value": {
        "currentDungeonIdx": 160,
        "currentPull": 1,
        "currentSublevel": 1,
        "pulls": {
            1: {1: [1, 2], "color": "ff8000"},
            2: {2: [1, 2]},
            3: {1: [3]},
            4: {3: [1]},
        },
    },
}

# --- players ----------------------------------------------------------------

TANK = ("Player-1403-0000000A", "Thicktank-Area52", 0x511, 66)     # prot pala
HEALER = ("Player-1403-0000000B", "Bigheals-Area52", 0x512, 264)   # resto sham
DPS1 = ("Player-1403-0000000C", "Zappyboi-Area52", 0x514, 63)      # fire mage
PLAYERS = [TANK, HEALER, DPS1]

HOSTILE = 0x0A48
DAY = "8/30/2026"


class LogBuilder:
    def __init__(self):
        self.lines: list[str] = []

    @staticmethod
    def _stamp(t: float) -> str:
        base_h, base_m, base_s = 20, 0, 0.0
        total = base_h * 3600 + base_m * 60 + base_s + t
        h = int(total // 3600)
        m = int((total % 3600) // 60)
        s = total % 60
        return f"{DAY} {h}:{m:02d}:{s:06.3f}-4"

    def raw(self, t: float, payload: str) -> None:
        self.lines.append(f"{self._stamp(t)}  {payload}")

    # -- infrastructure events --

    def start(self, t: float, zone="Murder Row", instance=2830, cm=587, lvl=10):
        self.raw(t, f'CHALLENGE_MODE_START,"{zone}",{instance},{cm},{lvl},[160,9,10]')

    def end(self, t: float, success=1, lvl=10, ms=600000, instance=2830):
        self.raw(t, f"CHALLENGE_MODE_END,{instance},{success},{lvl},{ms}")

    def combatant(self, t: float, player, ilvl=630):
        guid, _name, _flags, spec = player
        stats = ",".join(["1000"] * 21)
        self.raw(t, f"COMBATANT_INFO,{guid},0,{stats},{spec},(1,2,3),(0,0,0,0),"
                    f"[1],[2],[3],{ilvl},0,0,0")

    def encounter_start(self, t: float, enc_id=3001, name="Big Boss"):
        self.raw(t, f'ENCOUNTER_START,{enc_id},"{name}",8,5,2830')

    def encounter_end(self, t: float, enc_id=3001, name="Big Boss", success=1):
        self.raw(t, f'ENCOUNTER_END,{enc_id},"{name}",8,5,{success},123456')

    # -- units --

    @staticmethod
    def npc_guid(npc_id: int, spawn: str) -> str:
        return f"Creature-0-1465-2830-12345-{npc_id}-{spawn}"

    @staticmethod
    def _advanced(info_guid: str, hp=500000, max_hp=1000000, x=100.0, y=-200.0):
        return (f"{info_guid},0000000000000000,{hp},{max_hp},2000,1000,500,0,3,"
                f"100,100,0,{x:.2f},{y:.2f},2200,1.57,80")

    # -- combat events --

    def spell_damage(self, t, src, src_name, src_flags, dst, dst_name, dst_flags,
                     spell_id, spell_name, amount, overkill=0, crit=False,
                     hp=500000):
        adv = self._advanced(dst, hp=hp)
        self.raw(t, f'SPELL_DAMAGE,{src},"{src_name}",{src_flags:#06x},0x0,'
                    f'{dst},"{dst_name}",{dst_flags:#06x},0x0,'
                    f'{spell_id},"{spell_name}",0x4,{adv},'
                    f'{amount},{amount},{overkill},0x4,0,0,0,'
                    f'{"1" if crit else "nil"},nil,nil,nil')

    def player_damage(self, t, player, dst, dst_name, spell_id, spell_name,
                      amount, overkill=0, crit=False):
        guid, name, flags, _ = player
        self.spell_damage(t, guid, name, flags, dst, dst_name, HOSTILE,
                          spell_id, spell_name, amount, overkill, crit)

    def npc_damage(self, t, npc_guid, npc_name, player, spell_id, spell_name,
                   amount, hp=400000):
        guid, name, flags, _ = player
        self.spell_damage(t, npc_guid, npc_name, HOSTILE, guid, name, flags,
                          spell_id, spell_name, amount, hp=hp)

    def npc_debuff(self, t, src_guid, src_name, dst_player, spell_id, spell_name):
        dguid, dname, dflags, _ = dst_player
        self.raw(t, f'SPELL_AURA_APPLIED,{src_guid},"{src_name}",{HOSTILE:#06x},0x0,'
                    f'{dguid},"{dname}",{dflags:#06x},0x0,'
                    f'{spell_id},"{spell_name}",0x20,DEBUFF')

    def npc_periodic_damage(self, t, src_guid, src_name, dst_player, spell_id,
                            spell_name, amount, hp=400000):
        dguid, dname, dflags, _ = dst_player
        adv = self._advanced(dguid, hp=hp)
        self.raw(t, f'SPELL_PERIODIC_DAMAGE,{src_guid},"{src_name}",{HOSTILE:#06x},0x0,'
                    f'{dguid},"{dname}",{dflags:#06x},0x0,'
                    f'{spell_id},"{spell_name}",0x20,{adv},'
                    f'{amount},{amount},0,0x20,0,0,0,nil,nil,nil,nil')

    def npc_heal(self, t, src_guid, src_name, dst_guid, dst_name, spell_id,
                 spell_name, amount):
        adv = self._advanced(dst_guid)
        self.raw(t, f'SPELL_HEAL,{src_guid},"{src_name}",{HOSTILE:#06x},0x0,'
                    f'{dst_guid},"{dst_name}",{HOSTILE:#06x},0x0,'
                    f'{spell_id},"{spell_name}",0x8,{adv},'
                    f'{amount},{amount},0,0,nil')

    def heal(self, t, src_player, dst_player, spell_id, spell_name, amount,
             overheal=0):
        sguid, sname, sflags, _ = src_player
        dguid, dname, dflags, _ = dst_player
        adv = self._advanced(dguid)
        self.raw(t, f'SPELL_HEAL,{sguid},"{sname}",{sflags:#06x},0x0,'
                    f'{dguid},"{dname}",{dflags:#06x},0x0,'
                    f'{spell_id},"{spell_name}",0x8,{adv},'
                    f'{amount},{amount},{overheal},0,nil')

    def cast(self, t, player, spell_id, spell_name, target_guid="0000000000000000",
             target_name="nil", target_flags=0x80000000, x=100.0, y=-200.0):
        guid, name, flags, _ = player
        adv = self._advanced(guid, x=x, y=y)
        self.raw(t, f'SPELL_CAST_SUCCESS,{guid},"{name}",{flags:#06x},0x0,'
                    f'{target_guid},{target_name},{target_flags:#010x},0x0,'
                    f'{spell_id},"{spell_name}",0x1,{adv}')

    def npc_cast_start(self, t, src_guid, src_name, spell_id, spell_name):
        self.raw(t, f'SPELL_CAST_START,{src_guid},"{src_name}",{HOSTILE:#06x},0x0,'
                    f'0000000000000000,nil,0x80000000,0x0,'
                    f'{spell_id},"{spell_name}",0x20')

    def npc_cast_success(self, t, src_guid, src_name, spell_id, spell_name):
        adv = self._advanced(src_guid)
        self.raw(t, f'SPELL_CAST_SUCCESS,{src_guid},"{src_name}",{HOSTILE:#06x},0x0,'
                    f'0000000000000000,nil,0x80000000,0x0,'
                    f'{spell_id},"{spell_name}",0x20,{adv}')

    def party_kill(self, t, player, dst_guid, dst_name):
        guid, name, flags, _ = player
        self.raw(t, f'PARTY_KILL,{guid},"{name}",{flags:#06x},0x0,'
                    f'{dst_guid},"{dst_name}",{HOSTILE:#06x},0x0,0')

    def aura_removed(self, t, src_player, dst_player, spell_id, spell_name,
                     kind="BUFF"):
        sguid, sname, sflags, _ = src_player
        dguid, dname, dflags, _ = dst_player
        self.raw(t, f'SPELL_AURA_REMOVED,{sguid},"{sname}",{sflags:#06x},0x0,'
                    f'{dguid},"{dname}",{dflags:#06x},0x0,'
                    f'{spell_id},"{spell_name}",0x1,{kind}')

    def dispel(self, t, player, dst_guid, dst_name, dst_flags, spell_id,
               spell_name, removed_id, removed_name, kind="BUFF"):
        guid, name, flags, _ = player
        self.raw(t, f'SPELL_DISPEL,{guid},"{name}",{flags:#06x},0x0,'
                    f'{dst_guid},"{dst_name}",{dst_flags:#010x},0x0,'
                    f'{spell_id},"{spell_name}",0x1,'
                    f'{removed_id},"{removed_name}",0x1,{kind}')

    def interrupt(self, t, player, dst, dst_name, kick_id, kick_name,
                  stopped_id, stopped_name):
        guid, name, flags, _ = player
        self.raw(t, f'SPELL_INTERRUPT,{guid},"{name}",{flags:#06x},0x0,'
                    f'{dst},"{dst_name}",{HOSTILE:#06x},0x0,'
                    f'{kick_id},"{kick_name}",0x1,'
                    f'{stopped_id},"{stopped_name}",0x40')

    def aura(self, t, src_player, dst_player, spell_id, spell_name, kind="BUFF"):
        sguid, sname, sflags, _ = src_player
        dguid, dname, dflags, _ = dst_player
        self.raw(t, f'SPELL_AURA_APPLIED,{sguid},"{sname}",{sflags:#06x},0x0,'
                    f'{dguid},"{dname}",{dflags:#06x},0x0,'
                    f'{spell_id},"{spell_name}",0x1,{kind}')

    def unit_died(self, t, guid, name, flags, unconscious=0):
        self.raw(t, f'UNIT_DIED,0000000000000000,nil,0x80000000,0x80000000,'
                    f'{guid},"{name}",{flags:#06x},0x0,{unconscious}')

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def build_run_log() -> LogBuilder:
    """A tiny but complete M+ run matching (and deviating from) ROUTE_PRESET."""
    b = LogBuilder()
    fA = b.npc_guid(FELWYRM, "000A")
    fB = b.npc_guid(FELWYRM, "000B")
    dA = b.npc_guid(DUSKBLADE, "000C")
    dB = b.npc_guid(DUSKBLADE, "000D")
    sh = b.npc_guid(SHADELING, "000E")
    boss = b.npc_guid(BOSS, "000F")
    add = b.npc_guid(SUMMONED, "0010")

    b.start(0)
    for p in PLAYERS:
        b.combatant(0.5, p)

    # --- pull 1 (t=10..40): 2x Felwyrm, plus a Duskblade pulled early ---
    for i, t in enumerate([10, 12, 14, 20, 26, 32]):
        b.player_damage(t, DPS1, fA, "Felwyrm", 133, "Fireball", 50000, crit=(i == 1))
        b.player_damage(t + 0.5, TANK, fB, "Felwyrm", 31935, "Avenger's Shield", 20000)
    b.npc_damage(11, fA, "Felwyrm", TANK, 1214966, "Fel Bite", 30000)
    b.player_damage(15, DPS1, dA, "Duskblade", 133, "Fireball", 40000)  # early!
    b.heal(16, HEALER, TANK, 8004, "Healing Surge", 25000, overheal=5000)
    b.cast(13, DPS1, 133, "Fireball", fA, '"Felwyrm"', HOSTILE)
    # movement: two more casts from new positions (+10 yd, then +30 yd)
    b.cast(20, DPS1, 133, "Fireball", fA, '"Felwyrm"', HOSTILE, x=110.0)
    b.cast(30, DPS1, 133, "Fireball", fA, '"Felwyrm"', HOSTILE, x=110.0, y=-230.0)
    b.party_kill(37.9, DPS1, fA, "Felwyrm")
    b.unit_died(38, fA, "Felwyrm", HOSTILE)
    b.player_damage(38.5, TANK, fB, "Felwyrm", 31935, "Avenger's Shield", 60000,
                    overkill=1000)
    b.party_kill(38.9, TANK, fB, "Felwyrm")
    b.unit_died(39, fB, "Felwyrm", HOSTILE)
    b.player_damage(39.5, DPS1, dA, "Duskblade", 133, "Fireball", 90000)
    b.party_kill(39.9, DPS1, dA, "Duskblade")
    b.unit_died(40, dA, "Duskblade", HOSTILE)

    # --- downtime 40..60 ---

    # --- pull 2 (t=60..80): Duskblade B + off-route Shadeling + untracked add
    b.player_damage(60, TANK, dB, "Duskblade", 31935, "Avenger's Shield", 30000)
    b.player_damage(61, DPS1, sh, "Shadeling", 133, "Fireball", 30000)
    b.player_damage(62, DPS1, add, "Summoned Thing", 133, "Fireball", 10000)
    # a pure-DoT spell: one application on the tank, three 15k ticks;
    # the healer kicks its next cast -> ~45k DoT damage prevented
    b.npc_cast_start(62.0, sh, "Shadeling", 777001, "Creeping Rot")
    b.npc_cast_success(62.5, sh, "Shadeling", 777001, "Creeping Rot")
    b.npc_debuff(62.5, sh, "Shadeling", TANK, 777001, "Creeping Rot")
    b.npc_cast_start(62.6, dB, "Duskblade", 1216538, "Dark Bolt")
    b.interrupt(63, TANK, dB, "Duskblade", 96231, "Rebuke", 1216538, "Dark Bolt")
    b.npc_cast_start(63.0, sh, "Shadeling", 777001, "Creeping Rot")
    b.interrupt(63.2, HEALER, sh, "Shadeling", 57994, "Wind Shear",
                777001, "Creeping Rot")
    b.npc_periodic_damage(63.5, sh, "Shadeling", TANK, 777001, "Creeping Rot", 15000)
    b.npc_cast_start(63.7, dB, "Duskblade", 1216538, "Dark Bolt")
    # a zero-damage debuff (pure CC): kicking it prevents an application
    b.npc_debuff(63.8, sh, "Shadeling", DPS1, 777002, "Nasty Hex")
    b.npc_cast_success(64, dB, "Duskblade", 1216538, "Dark Bolt")
    b.npc_damage(64, dB, "Duskblade", HEALER, 1216538, "Dark Bolt", 150000, hp=200000)
    # kick of an enemy heal (observed once at t=67), and one of a spell that
    # never lands in this run (no basis for an estimate)
    b.npc_cast_start(64.8, sh, "Shadeling", 888001, "Void Mending")
    b.interrupt(65, DPS1, sh, "Shadeling", 2139, "Counterspell", 888001, "Void Mending")
    b.npc_periodic_damage(65.5, sh, "Shadeling", TANK, 777001, "Creeping Rot", 15000)
    b.npc_cast_start(65.7, dB, "Duskblade", 1216538, "Dark Bolt")
    b.npc_cast_success(66, dB, "Duskblade", 1216538, "Dark Bolt")
    b.npc_damage(66, dB, "Duskblade", HEALER, 1216538, "Dark Bolt", 250000, hp=0)
    b.unit_died(66.5, HEALER[0], HEALER[1], HEALER[2])
    b.npc_cast_start(66.8, sh, "Shadeling", 888001, "Void Mending")
    b.npc_cast_success(67, sh, "Shadeling", 888001, "Void Mending")
    b.npc_heal(67, sh, "Shadeling", dB, "Duskblade", 888001, "Void Mending", 80000)
    b.npc_periodic_damage(67.5, sh, "Shadeling", TANK, 777001, "Creeping Rot", 15000)
    b.cast(68, TANK, 391054, "Intercession", HEALER[0], f'"{HEALER[1]}"', HEALER[2])
    b.interrupt(69, DPS1, sh, "Shadeling", 2139, "Counterspell", 999, "Mystery Bolt")
    b.interrupt(69.5, DPS1, sh, "Shadeling", 2139, "Counterspell", 777002, "Nasty Hex")
    b.player_damage(70, DPS1, dB, "Duskblade", 133, "Fireball", 90000)
    # purge an enemy buff, then dispel the leftover hex off the mage
    b.dispel(70.5, TANK, dB, "Duskblade", HOSTILE, 32375, "Mass Dispel",
             888003, "Enrage", kind="BUFF")
    b.party_kill(70.9, DPS1, dB, "Duskblade")
    b.unit_died(71, dB, "Duskblade", HOSTILE)
    b.dispel(71.5, TANK, DPS1[0], DPS1[1], DPS1[2], 4987, "Cleanse",
             777002, "Nasty Hex", kind="DEBUFF")
    b.player_damage(72, TANK, sh, "Shadeling", 31935, "Avenger's Shield", 50000)
    # one last heal attempt that dies with the caster -> "expired"
    b.npc_cast_start(73.5, sh, "Shadeling", 888001, "Void Mending")
    b.party_kill(73.9, TANK, sh, "Shadeling")
    b.unit_died(74, sh, "Shadeling", HOSTILE)

    # --- pull 3 (t=100..130): the boss, with bloodlust and consumables ---
    b.encounter_start(100)
    b.cast(100.5, DPS1, 431932, "Tempered Potion", x=110.0, y=-230.0)
    b.cast(101, HEALER, 2825, "Bloodlust")
    for p in PLAYERS:
        b.aura(101.5, HEALER, p, 2825, "Bloodlust")
    for t in range(102, 128, 4):
        b.player_damage(float(t), DPS1, boss, "Big Boss", 133, "Fireball", 80000)
        b.player_damage(t + 1.0, TANK, boss, "Big Boss", 31935, "Avenger's Shield",
                        25000)
    b.cast(105, HEALER, 6262, "Healthstone")
    b.npc_damage(110, boss, "Big Boss", TANK, 1221063, "Boss Smash", 90000)
    b.party_kill(128.9, DPS1, boss, "Big Boss")
    b.unit_died(129, boss, "Big Boss", HOSTILE)
    b.encounter_end(130)
    for p in PLAYERS:
        b.aura_removed(131.5, HEALER, p, 2825, "Bloodlust")

    b.end(140, success=1, ms=600000)
    return b


@pytest.fixture()
def dungeon_data_file(tmp_path) -> Path:
    path = tmp_path / "mdt_data.json"
    path.write_text(json.dumps(DUNGEON_DATA), encoding="utf-8")
    return path


@pytest.fixture()
def route_string() -> str:
    return encode_mdt_string(ROUTE_PRESET, "mdt2")


@pytest.fixture()
def log_file(tmp_path) -> Path:
    path = tmp_path / "WoWCombatLog.txt"
    path.write_text(build_run_log().text(), encoding="utf-8")
    return path
