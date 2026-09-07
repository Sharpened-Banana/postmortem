"""Minimal Blizzard Game Data API client, used only to *build* bundled
data files (see ``postmortem build-talent-data``) -- never on the
analysis path, which stays offline and stdlib-only.

Same posture as wcl.py: credentials come from the environment
(``BNET_CLIENT_ID``/``BNET_CLIENT_SECRET``, registered at
https://develop.battle.net/access/), every call is a plain urllib
request, and failures raise a single error type the CLI turns into a
message rather than a traceback.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .net import https_context

OAUTH_URL = "https://oauth.battle.net/token"
_TIMEOUT_S = 20.0


class BlizzardApiError(Exception):
    """A credential, network or response problem talking to Blizzard."""


def credentials_from_env() -> tuple[str, str]:
    client_id = os.environ.get("BNET_CLIENT_ID", "").strip()
    client_secret = os.environ.get("BNET_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise BlizzardApiError(
            "set BNET_CLIENT_ID and BNET_CLIENT_SECRET in the environment "
            "(register a client at https://develop.battle.net/access/)"
        )
    return client_id, client_secret


class BlizzardApi:
    """Client-credentials access to the static Game Data namespace."""

    def __init__(self, client_id: str, client_secret: str, region: str = "us"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.region = region
        self._token: Optional[str] = None
        self._expires_at = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._expires_at:
            return self._token
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8")
        request = urllib.request.Request(
            OAUTH_URL, data=body,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S, context=https_context()) as resp:
                payload = json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BlizzardApiError(f"could not get an access token: {exc}") from exc
        token = payload.get("access_token")
        if not token:
            raise BlizzardApiError("token endpoint returned no access_token")
        self._token = str(token)
        self._expires_at = time.time() + max(60, int(payload.get("expires_in") or 3600) - 60)
        return self._token

    def get(self, path: str) -> dict[str, Any]:
        """GET a static-namespace path (absolute paths and full URLs both
        work -- the tree index hands back full hrefs)."""
        if path.startswith("http"):
            path = urllib.parse.urlsplit(path).path
        sep = "&" if "?" in path else "?"
        url = (f"https://{self.region}.api.blizzard.com{path}"
               f"{sep}namespace=static-{self.region}&locale=en_US")
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S, context=https_context()) as resp:
                return json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BlizzardApiError(f"{url}: {exc}") from exc


def _node_options(node: dict[str, Any]) -> list[dict[str, Any]]:
    """``[{"name", "spell_id"}]`` for one talent-tree node. Rank 1's
    tooltips name the talent; higher ranks just repeat it."""
    for rank in node.get("ranks") or []:
        tooltips = rank.get("choice_of_tooltips") or (
            [rank["tooltip"]] if rank.get("tooltip") else []
        )
        options = []
        for tip in tooltips:
            talent = tip.get("talent") or {}
            spell = (tip.get("spell_tooltip") or {}).get("spell") or {}
            if talent.get("name"):
                options.append({"name": talent["name"], "spell_id": spell.get("id")})
        if options:
            return options
    return []


def build_talent_nodes(api: BlizzardApi, echo=lambda _m: None) -> dict[str, Any]:
    """Walk every spec's talent trees and return the bundled-file payload:
    ``{"nodes": {node_id: {"options": [...]}}}``.

    Node ids are unique across trees and are exactly what COMBATANT_INFO
    logs, so the map is flat rather than nested per spec -- a decoder
    only ever has a node id to look up (see talents.py). Class trees are
    shared between a class's specs, so the same nodes are seen several
    times; identical repeats are simply overwritten.
    """
    index = api.get("/data/wow/talent-tree/index")
    trees = index.get("spec_talent_trees") or []
    nodes: dict[str, Any] = {}
    for i, entry in enumerate(trees, start=1):
        href = (entry.get("key") or {}).get("href") or ""
        if not href:
            continue
        name = entry.get("name") or "?"
        echo(f"  [{i}/{len(trees)}] {name}")
        try:
            tree = api.get(href)
        except BlizzardApiError as exc:
            echo(f"    skipped: {exc}")
            continue
        groups = [tree.get("class_talent_nodes") or [], tree.get("spec_talent_nodes") or []]
        for hero in tree.get("hero_talent_trees") or []:
            groups.append(hero.get("hero_talent_nodes") or [])
        for group in groups:
            for node in group:
                node_id = node.get("id")
                options = _node_options(node)
                if node_id and options:
                    nodes[str(node_id)] = {"options": options}
    return {
        "_comment": (
            "Talent node id -> talent option(s), built by `postmortem "
            "build-talent-data` from Blizzard's own talent-tree API. Node "
            "ids are what COMBATANT_INFO logs; a node with two options is "
            "a choice node, which the analyzer resolves using the run's "
            "own casts (see talents.py)."
        ),
        "source": "https://develop.battle.net/documentation/world-of-warcraft/game-data-apis",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "node_count": len(nodes),
        "nodes": nodes,
    }
