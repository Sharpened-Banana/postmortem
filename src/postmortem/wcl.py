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
import time
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


#: Per-request timeout. Table queries over a long dungeon run can take a
#: while server-side; 30 s produced a spurious "read operation timed out"
#: mid-build on the first full live run (2026-09-06).
REQUEST_TIMEOUT_S = 90
#: Transient failures (timeouts, connection resets, HTTP 429/5xx) are
#: retried this many times with a growing pause before giving up.
RETRIES = 4


class _Transient(Exception):
    """A failure worth retrying, as opposed to a 4xx that won't change."""


def _with_retries(attempt: Callable[[], dict], retries: int = RETRIES,
                  sleep: Callable[[float], None] = time.sleep) -> dict:
    """Call ``attempt`` until it returns, retrying only ``_Transient``
    failures with backoff (2, 4, 8 ... seconds). Anything else propagates
    at once."""
    last: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            return attempt()
        except _Transient as exc:
            last = exc
            if i < retries:
                sleep(2.0 * (2 ** i))
    raise WCLError(f"gave up after {retries + 1} attempts: {last}")


def _default_transport(url: str, body: bytes, headers: dict[str, str]) -> dict:
    def attempt() -> dict:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S,
                                        context=https_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if exc.code == 429 or exc.code >= 500:
                raise _Transient(f"HTTP {exc.code} from {url}: {detail}") from exc
            raise WCLError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # includes socket timeouts and connection resets
            raise _Transient(f"could not reach {url}: {exc}") from exc
    return _with_retries(attempt)


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


#: The API refuses listing pages past this ("until the performance of
#: paginated queries can be improved", live 2026-09-06), so a listing is
#: walked in time windows: 25 pages, then a new window ending just before
#: the oldest report seen.
MAX_LISTING_PAGE = 25


def iter_keystone_fights(client: WCLClient, zone_id: int,
                         max_pages: int = 10) -> Iterator[dict[str, Any]]:
    """Every keystone dungeon fight in recent public reports for the
    zone, newest first, scanning at most ``max_pages`` pages of
    ``REPORTS_PER_PAGE`` reports in total across as many time windows as
    that takes: ``{"code", "fight_id", "level", "encounter_id",
    "encounter", "kill"}``. Only whole keystone runs: fights without a
    keystone level (raid pulls, trash) and boss pulls inside a key are
    skipped."""
    pages_used = 0
    end_time: Optional[float] = None
    while pages_used < max_pages:
        oldest: Optional[float] = None
        exhausted = False
        for page in range(1, MAX_LISTING_PAGE + 1):
            if pages_used >= max_pages:
                return
            variables: dict[str, Any] = {"zone": ("Int", zone_id), "page": ("Int", page)}
            window = ""
            if end_time is not None:
                variables["end"] = ("Float", end_time)
                window = ", endTime: $end"
            body = (
                f"reportData {{ reports(zoneID: $zone, limit: {REPORTS_PER_PAGE}, "
                f"page: $page{window}) {{ "
                "has_more_pages data { code startTime fights(killType: Encounters) { "
                "id name encounterID keystoneLevel kill countRequired } } } }"
            )
            data = client.query(body, variables)
            pages_used += 1
            reports = (data.get("reportData") or {}).get("reports") or {}
            for report in reports.get("data") or []:
                code = report.get("code")
                try:
                    started = float(report.get("startTime"))
                    oldest = started if oldest is None else min(oldest, started)
                except (TypeError, ValueError):
                    pass
                for fight in report.get("fights") or []:
                    level = fight.get("keystoneLevel")
                    if not code or not level:
                        continue
                    # A boss pull inside a key also carries keystoneLevel
                    # but is a subset of the run's fight; only the whole
                    # run has an enemy-forces requirement (live 2026-09-06:
                    # "T'zala / Mchimba the Embalmer" fights showed up as
                    # separate +8 Kings' Rest samples).
                    if "countRequired" in fight and not fight.get("countRequired"):
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
                exhausted = True
                break
        if exhausted or oldest is None:
            return
        # next window: everything that started before the oldest report seen
        end_time = oldest - 1


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
