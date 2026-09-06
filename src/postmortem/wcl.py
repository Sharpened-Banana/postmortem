"""Minimal Warcraft Logs v2 (GraphQL) client, stdlib only.

Used by ``postmortem build-spell-damage`` (see cli.py) to sample public
Mythic+ reports for "how much damage does one completed cast of this
enemy spell do at key level N" -- the community fallback for the
kick-value estimate (see analysis/spell_damage.py). This is an offline,
once-per-season *build* step run by whoever refreshes the bundled data;
the analyzer itself never talks to Warcraft Logs, exactly as with
Raider.io (opt-in, never required) and the Method.gg-derived interrupt
database (built offline, bundled).

Auth is OAuth2 client-credentials: an API client registered at
https://www.warcraftlogs.com/api/clients/ gives a client id + secret,
which are exchanged for a bearer token good for public reports. They
are read from the ``WCL_CLIENT_ID`` / ``WCL_CLIENT_SECRET`` environment
variables and never stored by this project.

Rate limiting is a points-per-hour budget (3,600 on a free account;
table queries cost more than metadata) rather than a request count, so
every query also asks for ``rateLimitData`` and the client keeps the
latest answer in ``rate_limit`` for the caller to budget against.

The GraphQL shapes below were taken from the v2 schema docs
(reportdata/report/reportfight/tabledatatype, read 2026-09-06). The
*contents* of ``table`` results are documented by Warcraft Logs as "not
frozen" -- ``_table_entries`` therefore reads only the two fields every
by-ability table has carried for years (``guid``, ``total``) and
tolerates anything else changing.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator, Optional

from .net import https_context

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

#: (url, body bytes, headers) -> parsed JSON. Injectable so tests never
#: touch the network.
Transport = Callable[[str, bytes, dict[str, str]], dict]

RATE_LIMIT_FIELD = "rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }"

#: Reports per listing page. The API caps query complexity at 50,000 and
#: a 100-report page with nested fights came in at 50,305 (live, 2026-09-06);
#: 40 leaves headroom for reports with many fights.
REPORTS_PER_PAGE = 40


class WCLError(Exception):
    """Any failure talking to Warcraft Logs: HTTP, auth, or a GraphQL
    ``errors`` array in an otherwise-200 response."""


def _default_transport(url: str, body: bytes, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30, context=https_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise WCLError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise WCLError(f"could not reach {url}: {exc}") from exc


class WCLClient:
    def __init__(self, client_id: str, client_secret: str,
                 transport: Optional[Transport] = None) -> None:
        if not client_id or not client_secret:
            raise WCLError("WCL_CLIENT_ID and WCL_CLIENT_SECRET are both required")
        self._id = client_id
        self._secret = client_secret
        self._transport = transport or _default_transport
        self._token: Optional[str] = None
        #: last {"limitPerHour", "pointsSpentThisHour", "pointsResetIn"} seen
        self.rate_limit: Optional[dict[str, Any]] = None

    def token(self) -> str:
        if self._token:
            return self._token
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        payload = self._transport(
            TOKEN_URL,
            urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            {"Authorization": f"Basic {basic}",
             "Content-Type": "application/x-www-form-urlencoded"},
        )
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise WCLError("token exchange returned no access_token -- check the "
                           "client id/secret")
        self._token = str(token)
        return self._token

    def query(self, body: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Run ``body`` (the inside of a ``query { ... }`` block, with
        ``$var`` references declared via ``variables``' types below) and
        return the ``data`` object. ``rateLimitData`` is appended to every
        query and stripped into ``self.rate_limit``."""
        decl = ""
        if variables:
            decl = "(" + ", ".join(f"${k}: {t}" for k, (t, _v) in variables.items()) + ")"
        gql = f"query{decl} {{ {body} {RATE_LIMIT_FIELD} }}"
        payload = self._transport(
            API_URL,
            json.dumps({"query": gql,
                        "variables": {k: v for k, (_t, v) in (variables or {}).items()}}).encode(),
            {"Authorization": f"Bearer {self.token()}",
             "Content-Type": "application/json"},
        )
        if not isinstance(payload, dict):
            raise WCLError("non-object GraphQL response")
        if payload.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in payload["errors"])
            raise WCLError(f"GraphQL error: {msg}")
        data = payload.get("data") or {}
        if isinstance(data.get("rateLimitData"), dict):
            self.rate_limit = data.pop("rateLimitData")
        return data

    def points_left(self) -> Optional[float]:
        rl = self.rate_limit
        if not rl:
            return None
        return float(rl.get("limitPerHour", 0)) - float(rl.get("pointsSpentThisHour", 0))


# --- queries ------------------------------------------------------------------

def find_mplus_zone(client: WCLClient) -> tuple[int, str]:
    """The current Mythic+ ranking zone: the "Mythic+" zone of the newest
    expansion (highest expansion id, then highest zone id)."""
    data = client.query("worldData { zones { id name expansion { id name } } }")
    zones = (data.get("worldData") or {}).get("zones") or []
    candidates = [
        z for z in zones
        if isinstance(z, dict) and "mythic+" in str(z.get("name", "")).lower()
    ]
    if not candidates:
        raise WCLError("no zone named like 'Mythic+' in worldData.zones -- pass --zone-id")
    best = max(candidates, key=lambda z: (int((z.get("expansion") or {}).get("id") or 0),
                                          int(z.get("id") or 0)))
    return int(best["id"]), str(best.get("name") or "")


def iter_keystone_fights(client: WCLClient, zone_id: int,
                         max_pages: int = 10) -> Iterator[dict[str, Any]]:
    """Every keystone dungeon fight in recent public reports for the
    zone (``max_pages`` pages of ``REPORTS_PER_PAGE``), newest first:
    ``{"code", "fight_id", "level", "encounter_id", "encounter", "kill"}``. Fights without a keystone
    level (raid pulls, trash) are skipped."""
    body = (
        f"reportData {{ reports(zoneID: $zone, limit: {REPORTS_PER_PAGE}, page: $page) {{ "
        "has_more_pages data { code fights(killType: Encounters) { "
        "id name encounterID keystoneLevel kill } } } }"
    )
    for page in range(1, max_pages + 1):
        data = client.query(body, {"zone": ("Int", zone_id), "page": ("Int", page)})
        reports = (data.get("reportData") or {}).get("reports") or {}
        for report in reports.get("data") or []:
            code = report.get("code")
            for fight in report.get("fights") or []:
                level = fight.get("keystoneLevel")
                if not code or not level:
                    continue
                yield {
                    "code": code,
                    "fight_id": int(fight["id"]),
                    "level": int(level),
                    "encounter_id": int(fight.get("encounterID") or 0),
                    "encounter": str(fight.get("name") or ""),
                    "kill": bool(fight.get("kill")),
                }
        if not reports.get("has_more_pages"):
            break


def _table_entries(table: Any) -> dict[int, tuple[str, int]]:
    """``{spell_id: (name, total)}`` from a by-ability table result."""
    if isinstance(table, str):
        try:
            table = json.loads(table)
        except json.JSONDecodeError:
            return {}
    entries = None
    if isinstance(table, dict):
        entries = (table.get("data") or {}).get("entries") if isinstance(table.get("data"), dict) \
            else table.get("entries")
    out: dict[int, tuple[str, int]] = {}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        try:
            sid = int(e.get("guid"))
            total = int(round(float(e.get("total") or 0)))
        except (TypeError, ValueError):
            continue
        out[sid] = (str(e.get("name") or f"spell:{sid}"), total)
    return out


def fetch_fight_tables(client: WCLClient, code: str, fight_id: int) -> dict[str, Any]:
    """Per enemy ability, for one fight: damage the group took from it,
    healing enemies got from it, and how many times it was cast --
    three by-ability tables in a single request. Returns
    ``{"damage": {id: total}, "healing": {id: total}, "casts": {id: n},
    "names": {id: name}}``."""
    body = (
        "reportData { report(code: $code) { "
        "damage: table(fightIDs: [$fight], dataType: DamageTaken, "
        "hostilityType: Friendlies, viewBy: Ability) "
        "healing: table(fightIDs: [$fight], dataType: Healing, "
        "hostilityType: Enemies, viewBy: Ability) "
        "casts: table(fightIDs: [$fight], dataType: Casts, "
        "hostilityType: Enemies, viewBy: Ability) } }"
    )
    data = client.query(body, {"code": ("String!", code), "fight": ("Int!", fight_id)})
    report = (data.get("reportData") or {}).get("report") or {}
    damage = _table_entries(report.get("damage"))
    healing = _table_entries(report.get("healing"))
    casts = _table_entries(report.get("casts"))
    names: dict[int, str] = {}
    for table in (casts, damage, healing):
        for sid, (name, _total) in table.items():
            names.setdefault(sid, name)
    return {
        "damage": {sid: total for sid, (_n, total) in damage.items()},
        "healing": {sid: total for sid, (_n, total) in healing.items()},
        "casts": {sid: total for sid, (_n, total) in casts.items()},
        "names": names,
    }
