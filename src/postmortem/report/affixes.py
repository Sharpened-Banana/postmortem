"""Mythic+ affix names for the report pages.

A report's ``run.affixes`` is the list of numeric ids from the log's
CHALLENGE_MODE_START line. Both pages turn those into chips: the Browse
Runs feed (report/index.py, short labels) and the run report
(report/html.py, full names). The run report embeds the tables below as JS
constants at import time, so its inline script stays static and its CSP
hash (report/csp.py) stays stable. The feed still carries its own copy in
its template; tests/test_report_header.py keeps the two identical.

Unknown ids still render, as "#id", so a new season never blanks a page.
"""

from __future__ import annotations

import json

#: id -> full name
AFFIXES: dict[int, str] = {
    9: "Tyrannical", 10: "Fortified", 147: "Xal'atath's Guile", 148: "Ascendant",
    152: "Challenger's Peril", 158: "Voidbound", 159: "Oblivion", 160: "Devour",
    162: "Xal'atath's Bargain: Pulsar", 165: "Lindormi's Guidance",
}

#: id -> chip label where space is tight
AFFIX_SHORT: dict[int, str] = {
    9: "Tyr", 10: "Fort", 147: "Guile", 148: "Asc", 152: "Peril", 158: "Void",
    159: "Obliv", 160: "Devour", 162: "Pulsar", 165: "Guidance",
}

#: id -> what the affix does, which colours the chip: base = the weekly
#: Tyrannical/Fortified pair, season = the seasonal/bargain affix, harsh =
#: one that costs time on death. (Colour, not Blizzard's icon art: the page
#: must render offline and under a CSP that allows no remote images.)
AFFIX_KIND: dict[int, str] = {
    9: "base", 10: "base", 147: "harsh", 152: "harsh",
    148: "season", 158: "season", 159: "season", 160: "season",
    162: "season", 165: "season",
}


def affix_js() -> str:
    """The three tables as JS ``const`` declarations. JSON is valid JS
    here, and none of the names can contain "</" to end the script."""
    out = []
    for name, table in (("AFFIXES", AFFIXES), ("AFFIX_SHORT", AFFIX_SHORT),
                        ("AFFIX_KIND", AFFIX_KIND)):
        body = json.dumps({str(k): v for k, v in table.items()}, ensure_ascii=True)
        assert "</" not in body
        out.append(f"const {name} = {body};")
    return "\n".join(out)
