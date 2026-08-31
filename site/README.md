# Mythic Analyzer — public run tracker (`site/`)

A small FastAPI service that lets anyone upload a `mythic-analyzer`
report and browse everyone's uploaded runs publicly. This is the site
behind `mythic-analyzer analyze <log> --upload <url>`.

Reads are fully public — no account needed to browse. Writes are
self-served: see [Auth model](#auth-model) below.

## Local development

From the repo root:

```bash
pip install -e ".[dev,site]"

# The default MYTHIC_SITE_DB (/data/runs.db) is a production path meant
# for a Fly volume -- point it somewhere writable for local dev instead:
export MYTHIC_SITE_DB="$(pwd)/site/.dev-runs.db"

python -m uvicorn mythic_site.app:app --reload --app-dir site
```

This starts the service at `http://127.0.0.1:8000`. `--app-dir site`
puts `site/` on `sys.path` so `mythic_site` is importable without being
a pip-installed package (it's a plain directory, same trick used by
`site/tests/conftest.py`). Verified working this session (`GET
/healthz` and `GET /runs` both returned `200` against this exact
invocation).

Run the site's tests:

```bash
python -m pytest site/tests -q
```

(`site/tests/conftest.py` skips the whole suite cleanly if `fastapi`
isn't installed, so `python -m pytest tests site/tests -q` from the repo
root is always safe to run regardless of which extras are installed.)

## Deploying to Fly.io

This assumes you already have a Fly.io account and are logged in
locally (`fly auth login`). Everything below is a command *you* run from
your own terminal — nothing here was run against a real account as part
of building this.

The repo root already has a hand-authored `fly.toml` and `Dockerfile`
(there's no local Docker on the machine that authored them, so the
image itself has never actually been built — see
[What hasn't been verified](#what-hasnt-been-verified)).

1. **`fly launch --no-deploy`** (from the repo root).
   Since `fly.toml` already exists, current `flyctl` detects it and
   should offer to reuse it / link it to an app rather than scaffolding
   a fresh one from scratch — but the exact prompt flow depends on your
   installed `flyctl` version, and this wasn't exercised against a real
   account as part of this work package (that's explicitly out of scope
   here). Treat this step as "let `flyctl` sanity-check what's checked
   in," not "blindly accept whatever it generates" — diff anything it
   proposes changing in `fly.toml`/`Dockerfile` against what's already
   committed before accepting. In particular, double-check it doesn't
   silently drop the `[mounts]` block or the `/healthz` check.

2. **Create the data volume**, matching whatever `primary_region` ends
   up being in `fly.toml` (placeholder: `iad`):

   ```bash
   fly volumes create mythic_data --size 1 --region <region>
   ```

   `1` is gigabytes — the SQLite DB is small; this is comfortably more
   than v1 needs. `mythic_data` must match `[mounts].source` in
   `fly.toml`.

3. **Pin the machine count to 1:**

   ```bash
   fly scale count 1
   ```

   This matters more than it looks: the whole service is one SQLite file
   on one volume (`/data/runs.db`). SQLite is not safe to have two
   machines writing to concurrently the way this app is built (no
   replication, no WAL-over-network, no lock coordination across
   machines) — a second machine sharing that file would risk a corrupted
   database. Fly's own autoscaling defaults could otherwise spin up a
   second machine under load; this step keeps that from happening. Don't
   raise this past `1` without moving off SQLite first (e.g. Postgres —
   see [Known limitations](#known-limitations)).

4. **Deploy:**

   ```bash
   fly deploy
   ```

   With no local Docker available, this will use Fly's **remote
   builder** automatically — `fly deploy` detects there's no local
   Docker daemon and builds the image on Fly's infrastructure instead.
   This is the first point at which the Dockerfile actually gets built
   and exercised at all; watch the build log for surprises.

5. **Open it / wire up the CLI:**

   ```bash
   fly open
   ```

   or just note the printed `https://<your-app>.fly.dev` URL, then set
   it as your default upload target:

   ```bash
   mythic-analyzer analyze <log> --upload https://<your-app>.fly.dev
   ```

## Endpoints

| Route              | Method | Description                                                        |
|---------------------|--------|----------------------------------------------------------------------|
| `/`                 | GET    | Redirects to `/runs`.                                               |
| `/healthz`          | GET    | Trivial liveness check (no DB touch); used by Fly's health checks.  |
| `/runs`             | GET    | Public feed (HTML) — newest-first, optional `?zone=` filter.        |
| `/runs/{run_id}`    | GET    | Full per-run report (HTML).                                         |
| `/api/runs`         | GET    | Public feed (JSON) — same rows as `/runs`, minus the full report.   |
| `/api/runs/{run_id}`| GET    | Full per-run report (JSON) — the same payload a client uploaded.    |
| `/about`            | GET    | Static about/help page (HTML).                                     |
| `/api/runs`         | POST   | Upload a report. Requires `X-Upload-Token` header. See below.       |

## Auth model

There is **no real login system**. `X-Upload-Token` is a self-issued
string the uploader picks themselves (`mythic-analyzer analyze --upload
<url>` handles this automatically — see `src/mythic_analyzer/upload.py`
and `appdirs.py` for where the token is generated/stored). It provides:

- **Attribution / update-your-own-run protection**: uploading a report
  for the same `(zone, start_ts)` a second time (e.g. after a corrected
  re-analysis) requires presenting the *same* token that uploaded it the
  first time; a different token gets `409 already submitted by another
  uploader`.
- **Nothing else.** Reads are entirely public — anyone can browse every
  uploaded run, no token needed. Anyone can upload a brand-new run with
  any token they like. Losing a token means losing the ability to
  overwrite that specific run later, but the run itself stays visible
  either way. There's no password, no signup, no recovery, and no way to
  prove a run "really" belongs to a given player beyond whoever happened
  to hold that token at upload time.

Treat this the way you'd treat a wiki-with-an-edit-key, not a real
account system.

## Known limitations

- **SQLite on one volume, deliberately, for v1.** No Postgres or
  scale-out path yet. This is why the deploy runbook above insists on
  `fly scale count 1` and why the volume mount matters — there is
  exactly one writer.
- **No moderation or takedown tooling** beyond the per-token ownership
  check on overwrites. Anything uploaded stays visible; there's no admin
  UI, no report/flag mechanism, no delete endpoint.
- **The public feed is capped** at the 200 most recent runs
  (`MYTHIC_SITE_FEED_LIMIT`, default 200), optionally filtered by
  `?zone=`. There is no further pagination in v1 — older runs eventually
  fall off the feed (though they remain reachable directly at
  `/runs/{run_id}` if you have the id/link).

## What hasn't been verified

- **The Docker image has never actually been built.** This machine has
  no Docker installed. The Dockerfile has been reviewed by eye for
  structural correctness (paths, `WORKDIR`, `PYTHONPATH`, stage
  boundaries) but the only way to actually validate it is `fly deploy`'s
  remote builder, or a local Docker install if you have one
  (`docker build -t mythic-analyzer-site .` from the repo root).
- **The non-root user's write access to the Fly volume is unconfirmed.**
  The Dockerfile creates and runs as an `appuser` (not root) and
  `chown`s `/data` at build time as a best-effort default, but a Fly
  volume mount can override that directory's on-disk ownership at mount
  time (mountpoints aren't part of the image layer). If the deployed
  service can't write `/data/runs.db` (check `fly logs` after the first
  deploy), fix ownership once with:

  ```bash
  fly ssh console -C "chown -R appuser:appuser /data"
  ```

  or, as a fallback, remove the `USER appuser` line from the Dockerfile
  and redeploy running as root.
- **`fly launch`'s exact behavior against an already-existing `fly.toml`**
  wasn't exercised against a real account (see step 1 of the runbook
  above) — described accurately to the best of current knowledge, not
  guessed at confidently.
