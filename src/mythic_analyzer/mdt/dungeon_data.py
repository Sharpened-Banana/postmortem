"""Dungeon enemy data: maps MDT enemy indices to NPC ids, names and forces.

MDT ships this data as Lua tables inside the addon
(``MDT.dungeonEnemies[dungeonIndex]`` etc.). Use
``mythic-analyzer extract-data <path-to-MDT-addon>`` to convert the addon's
dungeon files into the JSON this module loads. Without dungeon data the
analyzer still works — it just can't resolve planned pulls to NPC ids, so
route-vs-actual comparison is limited to what the combat log alone shows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class EnemyClone:
    x: float
    y: float
    sublevel: Optional[int] = None
    group: Optional[int] = None


@dataclass
class MapPOI:
    """A point of interest on MDT's planning canvas (dungeon entrance, boss
    marker, etc.) -- from ``MDT.mapPOIs[dungeonIndex]``, keyed by sublevel."""

    type: str
    x: float
    y: float
    size_mult: Optional[float] = None


@dataclass
class Enemy:
    enemy_idx: int
    npc_id: int
    name: str
    count: float
    health: Optional[float] = None
    creature_type: Optional[str] = None
    level: Optional[int] = None
    is_boss: bool = False
    clones: list[EnemyClone] = field(default_factory=list)


@dataclass
class DungeonData:
    dungeon_idx: int
    name: str
    short_name: Optional[str] = None
    map_id: Optional[int] = None
    zone_ids: list[int] = field(default_factory=list)
    total_count: dict[str, float] = field(default_factory=dict)
    enemies: list[Enemy] = field(default_factory=list)
    # sublevel index -> display name (MDT.dungeonSubLevels). Every current-
    # season dungeon has exactly one sublevel; carried through rather than
    # discarded, but no multi-floor UI is built on top of it (yet).
    sublevels: dict[int, str] = field(default_factory=dict)
    # sublevel index -> map texture path (MDT.dungeonMaps). Not renderable
    # in an HTML report (it's a WoW client texture path, not an image asset
    # we have access to) -- kept only so extraction round-trips it.
    map_textures: dict[int, str] = field(default_factory=dict)
    # sublevel index -> POIs on that sublevel (MDT.mapPOIs): dungeon
    # entrance, boss markers, etc.
    pois: dict[int, list[MapPOI]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_index = {e.enemy_idx: e for e in self.enemies}
        self._by_npc_id: dict[int, Enemy] = {}
        for e in self.enemies:
            self._by_npc_id.setdefault(e.npc_id, e)

    def enemy_by_index(self, enemy_idx: int) -> Optional[Enemy]:
        return self._by_index.get(enemy_idx)

    def enemy_by_npc_id(self, npc_id: int) -> Optional[Enemy]:
        return self._by_npc_id.get(npc_id)

    def npc_name(self, npc_id: int) -> Optional[str]:
        enemy = self._by_npc_id.get(npc_id)
        return enemy.name if enemy else None

    def npc_count(self, npc_id: int) -> float:
        enemy = self._by_npc_id.get(npc_id)
        return enemy.count if enemy else 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DungeonData":
        enemies = []
        for e in d.get("enemies", []):
            clones = [
                EnemyClone(
                    x=c.get("x", 0.0),
                    y=c.get("y", 0.0),
                    sublevel=c.get("sublevel"),
                    group=c.get("g"),
                )
                for c in e.get("clones", [])
            ]
            enemies.append(
                Enemy(
                    enemy_idx=int(e["enemy_idx"]),
                    npc_id=int(e["id"]),
                    name=str(e.get("name") or f"npc:{e['id']}"),
                    count=float(e.get("count") or 0.0),
                    health=e.get("health"),
                    creature_type=e.get("creature_type"),
                    level=e.get("level"),
                    is_boss=bool(e.get("is_boss")),
                    clones=clones,
                )
            )
        sublevels = {
            int(k): str(v) for k, v in (d.get("sublevels") or {}).items()
            if str(k).lstrip("-").isdigit()
        }
        map_textures = {
            int(k): str(v) for k, v in (d.get("map_textures") or {}).items()
            if str(k).lstrip("-").isdigit()
        }
        pois: dict[int, list[MapPOI]] = {}
        for k, entries in (d.get("pois") or {}).items():
            if not str(k).lstrip("-").isdigit() or not isinstance(entries, list):
                continue
            parsed = [
                MapPOI(
                    type=str(e.get("type") or "unknown"),
                    x=float(e.get("x", 0.0)),
                    y=float(e.get("y", 0.0)),
                    size_mult=e.get("size_mult"),
                )
                for e in entries if isinstance(e, dict)
            ]
            if parsed:
                pois[int(k)] = parsed

        return cls(
            dungeon_idx=int(d["dungeon_idx"]),
            name=str(d.get("name") or f"dungeon:{d['dungeon_idx']}"),
            short_name=d.get("short_name"),
            map_id=d.get("map_id"),
            zone_ids=[int(z) for z in d.get("zone_ids", [])],
            total_count={str(k): float(v) for k, v in (d.get("total_count") or {}).items()},
            enemies=enemies,
            sublevels=sublevels,
            map_textures=map_textures,
            pois=pois,
        )


class DungeonDataStore:
    """A collection of DungeonData loaded from an extracted JSON file."""

    def __init__(self, dungeons: dict[int, DungeonData]):
        self.dungeons = dungeons
        self._by_zone: dict[int, DungeonData] = {}
        self._by_map_id: dict[int, DungeonData] = {}
        for d in dungeons.values():
            for z in d.zone_ids:
                self._by_zone.setdefault(z, d)
            if d.map_id is not None:
                self._by_map_id.setdefault(d.map_id, d)

    @classmethod
    def load(cls, path: str | Path) -> "DungeonDataStore":
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        dungeons: dict[int, DungeonData] = {}
        for key, d in (payload.get("dungeons") or {}).items():
            data = DungeonData.from_dict(d)
            dungeons[data.dungeon_idx] = data
        return cls(dungeons)

    def by_dungeon_idx(self, idx: Optional[int]) -> Optional[DungeonData]:
        if idx is None:
            return None
        return self.dungeons.get(idx)

    def by_zone_id(self, zone_id: Optional[int]) -> Optional[DungeonData]:
        if zone_id is None:
            return None
        return self._by_zone.get(zone_id)

    def by_challenge_map_id(self, map_id: Optional[int]) -> Optional[DungeonData]:
        """Look up by the challengeModeID / mapID logged by
        CHALLENGE_MODE_START (MDT calls this ``mapInfo.mapID``)."""
        if map_id is None:
            return None
        return self._by_map_id.get(map_id)
