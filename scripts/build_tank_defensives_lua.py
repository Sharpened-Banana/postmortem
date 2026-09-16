#!/usr/bin/env python3
"""Generate addon/Postmortem/TankDefensives.lua from the packaged
src/postmortem/data/tank_defensives.json.

The tank death post-mortem runs on both sides of the wall -- the Python
analyzer reads the combat log after the run, the addon watches it live --
and both need the same per-spec defensive table. Keeping one hand-written
copy in each language is exactly the drift bundled.py's header warns about
("one copy, one place, so the two can't drift apart"), so the Lua side is
generated from the JSON and never edited directly.

Run it after editing the JSON:

    python3 scripts/build_tank_defensives_lua.py

``scripts/update-for-patch.sh`` calls this too, so a patch refresh cannot
leave the addon holding last season's cooldowns.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JSON_PATH = REPO_ROOT / "src" / "postmortem" / "data" / "tank_defensives.json"
LUA_PATH = REPO_ROOT / "addon" / "Postmortem" / "TankDefensives.lua"

HEADER = """-- TankDefensives.lua
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
"""

FOOTER = """
-- Spec ids that are tanks, so a module can cheaply ask "is the player a
-- tank" without walking the table.
T.tankSpecs = {
%s
}

-- Display names, for the overlay's own labelling.
T.specNames = {
%s
}
"""


def _lua_escape(text: str) -> str:
    """Lua string literal escaping for the handful of characters that can
    appear in a spell name (apostrophes in 'Guardian of Ancient Kings'-style
    names, and defensively backslashes)."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def build_lua(payload: dict) -> str:
    by_spec: dict[int, list[dict]] = {}
    for entry in payload.get("defensives", ()):
        for spec_id in entry.get("specs", ()):
            by_spec.setdefault(int(spec_id), []).append(entry)

    out = [HEADER]
    for spec_id in sorted(by_spec):
        out.append(f"  [{spec_id}] = {{\n")
        for entry in sorted(by_spec[spec_id], key=lambda e: (e.get("category", ""), e["name"])):
            out.append(
                "    {{ id = {id}, name = \"{name}\", category = \"{cat}\", "
                "cooldown = {cd}, duration = {dur}, charges = {ch} }},\n".format(
                    id=int(entry["id"]),
                    name=_lua_escape(str(entry["name"])),
                    cat=_lua_escape(str(entry.get("category", "major"))),
                    cd=float(entry.get("cooldown_s", 0) or 0),
                    dur=float(entry.get("duration_s", 0) or 0),
                    ch=int(entry.get("charges", 1) or 1),
                )
            )
        out.append("  },\n")
    out.append("}\n\nT.externals = {\n")

    ext_by_spec: dict[int, list[dict]] = {}
    for entry in payload.get("externals", ()):
        for spec_id in entry.get("caster_specs", ()):
            ext_by_spec.setdefault(int(spec_id), []).append(entry)

    for spec_id in sorted(ext_by_spec):
        out.append(f"  [{spec_id}] = {{\n")
        for entry in sorted(ext_by_spec[spec_id], key=lambda e: e["name"]):
            out.append(
                "    {{ id = {id}, name = \"{name}\", cooldown = {cd}, "
                "duration = {dur} }},\n".format(
                    id=int(entry["id"]),
                    name=_lua_escape(str(entry["name"])),
                    cd=float(entry.get("cooldown_s", 0) or 0),
                    dur=float(entry.get("duration_s", 0) or 0),
                )
            )
        out.append("  },\n")
    out.append("}\n")

    tank_specs = "\n".join(
        f"  [{int(s)}] = true," for s in sorted(payload.get("tank_spec_ids", ()))
    )
    spec_names = "\n".join(
        f'  [{int(k)}] = "{_lua_escape(str(v.get("name", "")))}",'
        for k, v in sorted(
            (payload.get("specs") or {}).items(), key=lambda kv: int(kv[0])
        )
    )
    out.append(FOOTER % (tank_specs, spec_names))
    return "".join(out)


def main() -> int:
    with open(JSON_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    lua = build_lua(payload)
    LUA_PATH.write_text(lua, encoding="utf-8")
    specs = len({s for e in payload.get("defensives", ()) for s in e.get("specs", ())})
    print(
        f"wrote {LUA_PATH.relative_to(REPO_ROOT)} "
        f"({len(payload.get('defensives', ()))} defensives across {specs} specs, "
        f"{len(payload.get('externals', ()))} externals)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
