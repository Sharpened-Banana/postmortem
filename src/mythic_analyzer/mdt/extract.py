"""Extract dungeon enemy data from a Mythic Dungeon Tools addon folder.

MDT ships per-dungeon Lua data files (e.g. ``Midnight/MurderRow.lua``)
containing literal tables:

    local dungeonIndex = 160
    MDT.dungeonList[dungeonIndex] = L["MurderRow"]
    MDT.mapInfo[dungeonIndex] = { teleportId = ..., mapID = 587, ... }
    MDT.dungeonTotalCount[dungeonIndex] = { normal = 655 }
    MDT.dungeonEnemies[dungeonIndex] = { [1] = { name = ..., id = ..., ... } }
    MDT.dungeonSubLevels[dungeonIndex] = { [1] = "Murder Row" }
    MDT.dungeonMaps[dungeonIndex] = { [1] = { customTextures = '...' } }
    MDT.mapPOIs[dungeonIndex] = { [1] = { [1] = { type=..., x=..., y=... } } }

These are data-only literals, so a small tolerant Lua-literal parser is
enough — no Lua runtime needed. Anything that isn't a literal (function
calls, arithmetic) is skipped with a warning rather than failing the whole
extraction.

Usage:  mythic-analyzer extract-data <path-to-MythicDungeonTools> -o mdt_data.json
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

_SKIP = object()  # sentinel: value could not be parsed, skip the entry


class LuaParseError(ValueError):
    pass


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<longcomment>--\[(?P<ceq>=*)\[.*?\](?P=ceq)\])
  | (?P<comment>--[^\n]*)
  | (?P<longstring>\[(?P<eq>=*)\[.*?\](?P=eq)\])
  | (?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<number>0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<concat>\.\.)
  | (?P<punct>[\[\]{}=,;()\-+*/.<>#])
    """,
    re.VERBOSE | re.DOTALL,
)

_LUA_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b",
    "f": "\f", "v": "\v", "\\": "\\", '"': '"', "'": "'", "\n": "\n",
}


def _unescape_lua_string(raw: str) -> str:
    body = raw[1:-1]

    def sub(m: re.Match[str]) -> str:
        esc = m.group(1)
        if esc.isdigit():
            return chr(int(esc))
        return _LUA_ESCAPES.get(esc, esc)

    return re.sub(r"\\(\d{1,3}|.)", sub, body)


class _Tokens:
    def __init__(self, text: str, pos: int = 0):
        self.text = text
        self.pos = pos

    def next(self) -> tuple[str, str]:
        while self.pos < len(self.text):
            m = _TOKEN_RE.match(self.text, self.pos)
            if m is None:
                raise LuaParseError(
                    f"unexpected character {self.text[self.pos]!r} at offset {self.pos}"
                )
            self.pos = m.end()
            if m.group("ws") is not None or m.group("comment") is not None \
                    or m.group("longcomment") is not None:
                continue
            for name in ("longstring", "string", "number", "name", "concat", "punct"):
                if m.group(name) is not None:
                    return name, m.group(name)
        return "eof", ""

    def peek(self) -> tuple[str, str]:
        saved = self.pos
        tok = self.next()
        self.pos = saved
        return tok


class LuaLiteralParser:
    """Parses Lua literal expressions (tables, strings, numbers, booleans)
    with a few MDT-specific conveniences: ``L["x"]`` -> "x", ``addonName``
    -> "MythicDungeonTools", string concatenation with ``..``."""

    def __init__(self, text: str, warnings: Optional[list[str]] = None):
        self.tokens = _Tokens(text)
        self.warnings = warnings if warnings is not None else []

    def parse_value_at(self, pos: int) -> Any:
        self.tokens.pos = pos
        return self._parse_expr()

    def _parse_expr(self) -> Any:
        value = self._parse_primary()
        # handle string concatenation chains
        while True:
            kind, tok = self.tokens.peek()
            if kind == "concat":
                self.tokens.next()
                rhs = self._parse_primary()
                if isinstance(value, str) and isinstance(rhs, str):
                    value = value + rhs
                else:
                    value = _SKIP
            else:
                return value

    def _parse_primary(self) -> Any:
        kind, tok = self.tokens.next()
        if kind == "string":
            return _unescape_lua_string(tok)
        if kind == "longstring":
            body = re.match(r"\[(=*)\[(.*)\]\1\]", tok, re.DOTALL)
            return body.group(2) if body else tok
        if kind == "number":
            return self._number(tok)
        if kind == "punct" and tok == "-":
            rhs = self._parse_primary()
            if isinstance(rhs, (int, float)):
                return -rhs
            return _SKIP
        if kind == "punct" and tok == "{":
            return self._parse_table()
        if kind == "name":
            if tok == "true":
                return True
            if tok == "false":
                return False
            if tok == "nil":
                return None
            if tok == "L":
                # L["SomeName"] -> "SomeName"
                k, t = self.tokens.peek()
                if k == "punct" and t == "[":
                    self.tokens.next()
                    inner = self._parse_expr()
                    k, t = self.tokens.next()
                    if not (k == "punct" and t == "]"):
                        raise LuaParseError("expected ']' after L[...]")
                    return inner
                return _SKIP
            if tok == "addonName":
                return "MythicDungeonTools"
            # unknown identifier / function call: unsupported
            return self._skip_call_if_any()
        raise LuaParseError(f"unexpected token {tok!r} ({kind})")

    def _skip_call_if_any(self) -> Any:
        kind, tok = self.tokens.peek()
        if kind == "punct" and tok == "(":
            # consume a balanced (...) so parsing can continue
            self.tokens.next()
            depth = 1
            while depth > 0:
                k, t = self.tokens.next()
                if k == "eof":
                    raise LuaParseError("unterminated call")
                if k == "punct" and t == "(":
                    depth += 1
                elif k == "punct" and t == ")":
                    depth -= 1
        elif kind == "punct" and tok == ".":
            # dotted access like MDT.something — consume the chain
            while True:
                k, t = self.tokens.peek()
                if k == "punct" and t == ".":
                    self.tokens.next()
                    self.tokens.next()
                else:
                    break
        return _SKIP

    @staticmethod
    def _number(tok: str) -> int | float:
        if tok.lower().startswith("0x"):
            return int(tok, 16)
        if "." in tok or "e" in tok.lower():
            f = float(tok)
            return f
        return int(tok)

    def _parse_table(self) -> dict[Any, Any]:
        """Parse a table constructor. Array entries get 1-based int keys."""
        table: dict[Any, Any] = {}
        array_idx = 1
        while True:
            kind, tok = self.tokens.peek()
            if kind == "eof":
                raise LuaParseError("unterminated table")
            if kind == "punct" and tok == "}":
                self.tokens.next()
                return table
            if kind == "punct" and tok in (",", ";"):
                self.tokens.next()
                continue

            key: Any = None
            explicit_key = False
            if kind == "punct" and tok == "[":
                self.tokens.next()
                key = self._parse_expr()
                k, t = self.tokens.next()
                if not (k == "punct" and t == "]"):
                    raise LuaParseError("expected ']' in table key")
                k, t = self.tokens.next()
                if not (k == "punct" and t == "="):
                    raise LuaParseError("expected '=' after table key")
                explicit_key = True
            elif kind == "name":
                # could be `name = value` or a bareword value
                saved = self.tokens.pos
                self.tokens.next()
                k, t = self.tokens.peek()
                if k == "punct" and t == "=":
                    self.tokens.next()
                    key = tok
                    explicit_key = True
                else:
                    self.tokens.pos = saved

            try:
                value = self._parse_expr()
            except LuaParseError as exc:
                self.warnings.append(f"skipped unparseable entry: {exc}")
                self._skip_to_entry_end()
                continue

            if not explicit_key:
                key = array_idx
                array_idx += 1
            if value is not _SKIP and key is not _SKIP:
                table[key] = value

    def _skip_to_entry_end(self) -> None:
        depth = 0
        while True:
            kind, tok = self.tokens.next()
            if kind == "eof":
                return
            if kind == "punct":
                if tok in ("{", "["):
                    depth += 1
                elif tok in ("}", "]"):
                    if tok == "}" and depth == 0:
                        # end of enclosing table: back up so caller sees it
                        self.tokens.pos -= 1
                        return
                    depth -= 1
                elif tok == "," and depth == 0:
                    return


def _find_assignment(text: str, pattern: str) -> Optional[int]:
    m = re.search(pattern, text)
    return m.end() if m else None


def _lua_array(table: Any) -> list[Any]:
    if not isinstance(table, dict):
        return []
    out = []
    idx = 1
    while idx in table:
        out.append(table[idx])
        idx += 1
    if not out:  # non-contiguous: fall back to sorted int keys
        out = [table[k] for k in sorted(k for k in table if isinstance(k, int))]
    return out


def extract_dungeon_file(path: Path) -> Optional[dict[str, Any]]:
    """Extract one MDT dungeon data file into a JSON-ready dict."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if "MDT.dungeonEnemies[" not in text:
        return None
    warnings: list[str] = []
    parser = LuaLiteralParser(text, warnings)

    m = re.search(r"local\s+dungeonIndex\s*=\s*(\d+)", text)
    if not m:
        return None
    dungeon_idx = int(m.group(1))

    def parse_after(pattern: str) -> Any:
        pos = _find_assignment(text, pattern)
        if pos is None:
            return None
        try:
            return parser.parse_value_at(pos)
        except LuaParseError as exc:
            warnings.append(f"{path.name}: failed to parse {pattern!r}: {exc}")
            return None

    name = parse_after(r"MDT\.dungeonList\[dungeonIndex\]\s*=\s*")
    map_info = parse_after(r"MDT\.mapInfo\[dungeonIndex\]\s*=\s*")
    total_count = parse_after(r"MDT\.dungeonTotalCount\[dungeonIndex\]\s*=\s*")
    enemies_table = parse_after(r"MDT\.dungeonEnemies\[dungeonIndex\]\s*=\s*")
    sublevels_table = parse_after(r"MDT\.dungeonSubLevels\[dungeonIndex\]\s*=\s*")
    maps_table = parse_after(r"MDT\.dungeonMaps\[dungeonIndex\]\s*=\s*")
    pois_table = parse_after(r"MDT\.mapPOIs\[dungeonIndex\]\s*=\s*")

    zone_ids = [int(z) for z in re.findall(r"MDT\.zoneIdToDungeonIdx\[(\d+)\]", text)]
    zones_m = re.search(r"local\s+zones\s*=\s*\{([\d,\s]*)\}", text)
    if zones_m:
        zone_ids.extend(int(z) for z in re.findall(r"\d+", zones_m.group(1)))
    zone_ids = sorted(set(zone_ids))

    enemies: list[dict[str, Any]] = []
    if isinstance(enemies_table, dict):
        for enemy_idx in sorted(k for k in enemies_table if isinstance(k, int)):
            e = enemies_table[enemy_idx]
            if not isinstance(e, dict) or "id" not in e:
                continue
            clones = []
            for clone in _lua_array(e.get("clones")):
                if isinstance(clone, dict):
                    clones.append({
                        "x": clone.get("x"),
                        "y": clone.get("y"),
                        "g": clone.get("g"),
                        "sublevel": clone.get("sublevel"),
                    })
            enemies.append({
                "enemy_idx": enemy_idx,
                "id": e.get("id"),
                "name": e.get("name"),
                "count": e.get("count", 0),
                "health": e.get("health"),
                "creature_type": e.get("creatureType"),
                "level": e.get("level"),
                "is_boss": bool(e.get("isBoss")),
                "clones": clones,
            })

    result: dict[str, Any] = {
        "dungeon_idx": dungeon_idx,
        "name": name if isinstance(name, str) else path.stem,
        "enemies": enemies,
        "zone_ids": zone_ids,
    }
    if isinstance(map_info, dict):
        result["map_id"] = map_info.get("mapID")
        result["short_name"] = map_info.get("shortName") \
            if isinstance(map_info.get("shortName"), str) else None
        if isinstance(map_info.get("englishName"), str):
            result["name"] = map_info["englishName"]
    if isinstance(total_count, dict):
        result["total_count"] = {
            str(k): v for k, v in total_count.items() if isinstance(v, (int, float))
        }

    # dungeonSubLevels[dungeonIndex] = { [1] = "Murder Row", ... } -- sublevel
    # index -> display name. Every current-season dungeon has exactly one
    # sublevel; still extracted so nothing is silently thrown away.
    if isinstance(sublevels_table, dict):
        sublevels = {
            str(k): v for k, v in sublevels_table.items()
            if isinstance(k, int) and isinstance(v, str)
        }
        if sublevels:
            result["sublevels"] = sublevels

    # dungeonMaps[dungeonIndex] = { [1] = { customTextures = '...' }, ... } --
    # sublevel index -> map texture path. Not renderable (it's a WoW client
    # texture, not an image we have), kept for round-trip completeness only.
    if isinstance(maps_table, dict):
        map_textures: dict[str, Any] = {}
        for k, v in maps_table.items():
            if not isinstance(k, int):
                continue
            texture = None
            if isinstance(v, dict):
                texture = v.get("customTextures") or v.get("textures") \
                    or v.get("texture")
            elif isinstance(v, str):
                texture = v
            if isinstance(texture, str):
                map_textures[str(k)] = texture
        if map_textures:
            result["map_textures"] = map_textures

    # mapPOIs[dungeonIndex] = { [1] = { [1] = { type=..., x=..., y=...,
    # sizeMult=... }, ... }, ... } -- sublevel index -> list of POIs
    # (dungeon entrance, boss markers, etc).
    if isinstance(pois_table, dict):
        pois: dict[str, list[dict[str, Any]]] = {}
        for k, v in pois_table.items():
            if not isinstance(k, int):
                continue
            entries = []
            for poi in _lua_array(v):
                if not isinstance(poi, dict) or "x" not in poi or "y" not in poi:
                    continue
                entry: dict[str, Any] = {
                    "type": poi.get("type") if isinstance(poi.get("type"), str) else "unknown",
                    "x": poi.get("x"),
                    "y": poi.get("y"),
                }
                if isinstance(poi.get("sizeMult"), (int, float)):
                    entry["size_mult"] = poi["sizeMult"]
                entries.append(entry)
            if entries:
                pois[str(k)] = entries
        if pois:
            result["pois"] = pois

    if warnings:
        result["warnings"] = warnings
    return result


def extract_addon(addon_path: str | Path) -> dict[str, Any]:
    """Scan an MDT addon folder and extract every dungeon data file."""
    root = Path(addon_path)
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")
    dungeons: dict[str, Any] = {}
    for lua_file in sorted(root.rglob("*.lua")):
        try:
            data = extract_dungeon_file(lua_file)
        except Exception as exc:  # tolerant: one bad file shouldn't kill the run
            print(f"warning: {lua_file}: {exc}", file=sys.stderr)
            continue
        if data is not None:
            dungeons[str(data["dungeon_idx"])] = data
    return {
        "source": str(root),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dungeons": dungeons,
    }


def write_dungeon_data(addon_path: str | Path, out_path: str | Path) -> dict[str, Any]:
    payload = extract_addon(addon_path)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return payload
