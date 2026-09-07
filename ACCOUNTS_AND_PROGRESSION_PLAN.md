# Accounts and progression history — plan

Status: proposal, 2026-09-06. Nothing here is built yet. The public site
(`site/`, live at https://postmortem-mplus.fly.dev) is FastAPI over one
SQLite file on a Fly volume, with a self-issued upload token as its only
notion of "who". This plan turns that into real accounts and, on top of
them, a per-character progression history.

## What exists to build on

- **Identity today**: `uploads(run_id, token_hash)` records which upload
  token owns each run. Tokens are minted by the desktop app / CLI
  (`appdirs` `upload_token.json`) or by a cookie for browser uploads.
  There is no login, no recovery, no way to prove a run is yours.
- **Character identity in the data**: every run row has a `players`
  table with each participant's combat-log GUID (`Player-<realm id>-<hex>`,
  stable for a character across runs), name, class, spec, role, damage,
  healing, deaths, interrupts. That GUID is a free, reliable per-character
  key — progression can be grouped per character with no schema change to
  the analysis itself.
- **The logging player is known**: the combat log is written by one
  character; the run knows which participant that was. That is the one
  participant an uploader can be said to "be".
- **Raider.io enrichment** already adds current score/season best per
  player when enabled, so an external progression reference exists.
- **Scale**: one machine, one SQLite writer, a few hundred runs. Nothing
  below needs Postgres; keep `fly scale count 1`.

## Goals

1. A user can sign in, see their characters, and see every uploaded run
   those characters were in — theirs and their groupmates'.
2. A progression page per character: key level over time, timed rate,
   deaths, DPS/HPS by spec, kick efficiency and route adherence trends,
   per-dungeon bests, "this week" summary against the weekly reset.
3. Uploads from the desktop app and CLI become account-bound without the
   user managing tokens, and existing anonymous uploads can be claimed.
4. Owners get control over visibility of their own runs.

## Identity: recommendation and alternatives

**Recommended: Battle.net OAuth (Blizzard API) as the primary login.**
It is the identity WoW players already have, and its `wow.profile` scope
returns the account's character list (name, realm, class, level), which
gives verified character ownership for free — the one thing every other
provider cannot give. Cost: register an API client at
develop.battle.net (a user task, ~10 minutes), one redirect flow
(`/auth/bnet/login` → Blizzard → `/auth/bnet/callback`), and a region
choice (the profile API is per region; default `us`, settable per account).

Alternatives, in case Battle.net registration is a blocker:

- **Discord OAuth**: nearly universal among M+ players, trivial to set up,
  but proves nothing about characters. Characters would be claimed by
  "the logger of a run uploaded with your token" (see below), which only
  ever verifies the logging character.
- **Email magic link**: no third party, but needs an outbound mail
  provider and the same weak character claim as Discord.

Either alternative can be added later as a secondary login on the same
`accounts` table, so choosing Battle.net first does not close them off.

## Data model (new tables, `site/postmortem_site/db.py`)

```sql
accounts(
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,          -- 'bnet' | 'discord' | 'email'
  provider_id TEXT NOT NULL,       -- Blizzard account id, etc.
  display_name TEXT,               -- BattleTag
  region TEXT,                     -- 'us' | 'eu' | 'kr' | 'tw'
  created_at REAL, last_login_at REAL,
  UNIQUE(provider, provider_id)
)
characters(
  id INTEGER PRIMARY KEY,
  account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
  name TEXT NOT NULL, realm TEXT NOT NULL, region TEXT NOT NULL,
  class TEXT, guid TEXT,           -- guid filled in once seen in a run
  verified_by TEXT NOT NULL,       -- 'bnet_profile' | 'logger_claim'
  verified_at REAL,
  UNIQUE(region, realm, name)
)
account_tokens(
  token_hash TEXT PRIMARY KEY,     -- same sha256 the uploads table uses
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  label TEXT,                      -- 'desktop app on <host>', 'browser'
  created_at REAL, last_used_at REAL
)
sessions(
  id TEXT PRIMARY KEY,             -- random, stored in a Secure cookie
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  created_at REAL, expires_at REAL
)
-- runs gains: visibility TEXT DEFAULT 'public'  ('public' | 'unlisted')
```

`uploads.token_hash` stays as is. Ownership of a run resolves as:
`uploads.token_hash → account_tokens.account_id`. Claiming old anonymous
uploads is then just inserting the token into `account_tokens` — the
desktop app already holds the token and can send it once the user signs
in (below). No run rows move.

Character ↔ run linkage is derived, not stored: a run belongs to a
character when `players.guid = characters.guid`, or, until a guid is
known, when `players.name` matches and the run's realm matches. The first
match fills `characters.guid` in; after that it is exact.

Sessions are server-side rows referenced by an HMAC-signed cookie
(stdlib `hmac`/`secrets`; no new dependency). CSRF: same-site cookies
plus a per-session token on the few POST forms.

## Site surface

| Route | Purpose |
|---|---|
| `GET /auth/bnet/login`, `GET /auth/bnet/callback`, `POST /auth/logout` | login flow |
| `GET /me` | account home: characters (from the Blizzard profile, refreshable), linked tokens, recent runs |
| `GET /c/{region}/{realm}/{name}` | public character progression page |
| `GET /api/characters/{...}/runs` | JSON behind the progression charts |
| `POST /me/tokens/link` | link an upload token to the account (app device flow, see below) |
| `POST /runs/{id}/visibility` | owner-only: public / unlisted |

The public feed (`/runs`) stays as it is, minus unlisted runs. Report
pages get a small "also in this run" strip linking participant names to
their character pages when those characters are known.

## Progression history

Computed on read from `runs` + `players` (a few hundred rows; SQLite
handles it), cached per character for a minute. Per character:

- **Key level over time**: one point per run, coloured timed / over /
  abandoned; weekly max as a step line. Weekly boundaries follow the
  region's reset (US Tuesday 15:00 UTC, EU Wednesday 07:00 UTC).
- **Per dungeon bests**: highest timed level, fastest time at that level,
  best adherence, with links to the runs.
- **Trends**: DPS or HPS by spec (never mixed across specs), deaths per
  run, kick efficiency, route adherence, avoidable damage taken. Rolling
  five-run averages so a single bad key doesn't dominate.
- **This week**: keys run, timed count, highest level, deaths, compared
  with the previous week.
- **Group history**: the characters this character runs with most, and
  the group's timed rate together.

Charts follow the existing report's vanilla-JS, no-external-resources
convention (same as `report/html.py`), so nothing new is loaded.

## Desktop app and CLI

- **Device-code sign-in**: the app shows a short code and opens
  `https://…/link?code=XXXX`; the user signs in on the site, confirms, and
  the site binds the app's existing upload token to the account. No
  passwords in the app, and every past upload from that token is claimed
  in the same step. Watch Live keeps working unchanged.
- **CLI**: `postmortem login <url>` runs the same device flow and stores
  nothing new — the bound token is the credential already on disk.
- **Addon**: nothing required. Later, an optional `/postmortem link`
  could display the device code in game.

## Phases

1. **Foundations** (one PR): tables above, session cookie, Battle.net
   OAuth behind a `MYTHIC_SITE_BNET_CLIENT_ID/SECRET` feature flag (site
   runs exactly as today when unset), `/me` with the character list,
   token linking, tests with a fake Blizzard endpoint.
2. **Progression pages** (one PR): character page and JSON API, the
   charts above, weekly-reset logic, "also in this run" links, visibility
   toggle and feed filtering.
3. **App integration** (one PR): device-code flow in the desktop app and
   CLI, claiming of past uploads, a Settings tab entry showing the
   signed-in account.
4. **Later**: Discord as a secondary login, per-group pages, e-mail
   digests of the week.

## Decisions needed

- Battle.net as the first login provider — yes, or start with Discord?
- Default visibility for account-owned runs: public (as today) or
  unlisted until the owner flips it?
- Regions to support at launch: `us` only, or `us` + `eu`?
- Should groupmates' names on a run page link to their character pages
  even when those characters have no account yet (they are public in the
  report today already)?
