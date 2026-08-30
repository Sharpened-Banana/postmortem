"""Route model: a decoded MDT preset normalized into something usable."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from .dungeon_data import DungeonData


class RouteError(ValueError):
    pass


def _int_keyed(table: Any) -> dict[int, Any]:
    """Normalize a Lua table that may arrive as a list (CBOR array) or a
    dict with int (or numeric string) keys into {int: value}."""
    if table is None:
        return {}
    if isinstance(table, list):
        return {i + 1: v for i, v in enumerate(table) if v is not None}
    if isinstance(table, dict):
        out: dict[int, Any] = {}
        for k, v in table.items():
            if isinstance(k, bool):
                continue
            if isinstance(k, (int, float)) and float(k).is_integer():
                out[int(k)] = v
            elif isinstance(k, str) and k.lstrip("-").isdigit():
                out[int(k)] = v
        return out
    raise RouteError(f"expected table, got {type(table).__name__}")


@dataclass
class Pull:
    """One planned pull: enemy index -> which clones of that enemy to kill."""

    index: int
    enemies: dict[int, list[int]] = field(default_factory=dict)
    color: Optional[str] = None

    def clone_count(self) -> int:
        return sum(len(v) for v in self.enemies.values())

    def npc_counter(self, data: Optional[DungeonData]) -> Counter[int]:
        """Multiset of NPC ids in this pull (needs dungeon data to resolve
        MDT enemy indices to NPC ids)."""
        counter: Counter[int] = Counter()
        if data is None:
            return counter
        for enemy_idx, clones in self.enemies.items():
            enemy = data.enemy_by_index(enemy_idx)
            if enemy is not None:
                counter[enemy.npc_id] += len(clones)
        return counter

    def forces(self, data: Optional[DungeonData]) -> float:
        total = 0.0
        if data is None:
            return total
        for enemy_idx, clones in self.enemies.items():
            enemy = data.enemy_by_index(enemy_idx)
            if enemy is not None:
                total += enemy.count * len(clones)
        return total


@dataclass
class Route:
    name: str
    dungeon_idx: Optional[int]
    week: Optional[int]
    difficulty: Optional[int]
    pulls: list[Pull]
    raw: Any = None

    @classmethod
    def from_preset(cls, preset: Any) -> "Route":
        if not isinstance(preset, dict):
            raise RouteError(
                "decoded MDT data is not a preset table; "
                "is this a full route export?"
            )
        value = preset.get("value")
        if not isinstance(value, dict):
            raise RouteError("preset has no 'value' table — not an MDT route export")

        pulls_raw = _int_keyed(value.get("pulls"))
        pulls: list[Pull] = []
        for pull_idx in sorted(pulls_raw):
            entry = pulls_raw[pull_idx]
            if not isinstance(entry, dict):
                if isinstance(entry, list):
                    entry = {i + 1: v for i, v in enumerate(entry)}
                else:
                    continue
            enemies: dict[int, list[int]] = {}
            color = None
            for k, v in entry.items():
                if k == "color":
                    color = str(v) if v is not None else None
                    continue
                if isinstance(k, str) and not k.lstrip("-").isdigit():
                    continue  # other metadata keys
                try:
                    enemy_idx = int(k)
                except (TypeError, ValueError):
                    continue
                clones = sorted(
                    int(c) for c in _int_keyed(v).values()
                    if isinstance(c, (int, float))
                )
                if clones:
                    enemies[enemy_idx] = clones
            pulls.append(Pull(index=pull_idx, enemies=enemies, color=color))

        dungeon_idx = value.get("currentDungeonIdx")
        return cls(
            name=str(preset.get("text") or "unnamed route"),
            dungeon_idx=int(dungeon_idx) if isinstance(dungeon_idx, (int, float)) else None,
            week=_maybe_int(preset.get("week")),
            difficulty=_maybe_int(preset.get("difficulty")),
            pulls=pulls,
            raw=preset,
        )

    def total_forces(self, data: Optional[DungeonData]) -> float:
        return sum(p.forces(data) for p in self.pulls)

    def npc_counters(self, data: Optional[DungeonData]) -> list[Counter[int]]:
        return [p.npc_counter(data) for p in self.pulls]

    def summary(self, data: Optional[DungeonData] = None) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": self.name,
            "dungeon_idx": self.dungeon_idx,
            "week": self.week,
            "difficulty": self.difficulty,
            "pull_count": len(self.pulls),
            "pulls": [],
        }
        if data is not None:
            info["dungeon"] = data.name
            total_needed = data.total_count.get("normal")
            planned = self.total_forces(data)
            info["planned_forces"] = planned
            info["required_forces"] = total_needed
            if total_needed:
                info["planned_forces_pct"] = round(100.0 * planned / total_needed, 1)
        running = 0.0
        for pull in self.pulls:
            entry: dict[str, Any] = {
                "pull": pull.index,
                "clones": pull.clone_count(),
                "color": pull.color,
            }
            if data is not None:
                running += pull.forces(data)
                entry["forces"] = pull.forces(data)
                entry["forces_cumulative"] = running
                total_needed = data.total_count.get("normal")
                if total_needed:
                    entry["forces_pct_cumulative"] = round(100.0 * running / total_needed, 1)
                entry["enemies"] = [
                    {
                        "npc_id": enemy.npc_id,
                        "name": enemy.name,
                        "n": len(clones),
                    }
                    for enemy_idx, clones in sorted(pull.enemies.items())
                    if (enemy := data.enemy_by_index(enemy_idx)) is not None
                ]
            else:
                entry["enemy_indices"] = {
                    str(k): len(v) for k, v in sorted(pull.enemies.items())
                }
            info["pulls"].append(entry)
        return info


def _maybe_int(v: Any) -> Optional[int]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None
