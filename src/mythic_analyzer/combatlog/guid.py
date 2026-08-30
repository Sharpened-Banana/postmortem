"""Unit GUID parsing.

Retail GUID shapes:
    Player-<connectedRealmID>-<hex>
    Creature-0-<serverID>-<instanceID>-<zoneUID>-<npcID>-<spawnUID>
    Vehicle-0-...same as Creature...
    Pet-0-<serverID>-<instanceID>-<zoneUID>-<npcID>-<spawnUID>
    GameObject-0-...
    Corpse-..., Item-..., Vignette-..., 0000000000000000 (none)
"""

from __future__ import annotations

from functools import lru_cache
from typing import NamedTuple, Optional

NPC_UNIT_TYPES = frozenset({"Creature", "Vehicle"})


class GUID(NamedTuple):
    raw: str
    unit_type: str
    npc_id: Optional[int]
    spawn_uid: Optional[str]

    @property
    def is_player(self) -> bool:
        return self.unit_type == "Player"

    @property
    def is_npc(self) -> bool:
        return self.unit_type in NPC_UNIT_TYPES

    @property
    def is_pet(self) -> bool:
        return self.unit_type == "Pet"

    @property
    def is_none(self) -> bool:
        return self.unit_type == ""


@lru_cache(maxsize=65536)
def parse_guid(raw: str) -> GUID:
    if not raw or raw == "nil" or set(raw) == {"0"}:
        return GUID(raw, "", None, None)
    head, _, rest = raw.partition("-")
    if head in ("Creature", "Vehicle", "Pet", "GameObject"):
        parts = raw.split("-")
        npc_id = None
        spawn_uid = None
        if len(parts) >= 7:
            try:
                npc_id = int(parts[5])
            except ValueError:
                npc_id = None
            spawn_uid = parts[6]
        return GUID(raw, head, npc_id, spawn_uid)
    return GUID(raw, head, None, None)


def npc_id_of(raw: str) -> Optional[int]:
    return parse_guid(raw).npc_id
