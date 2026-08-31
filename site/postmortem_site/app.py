"""The public Postmortem site: FastAPI app.

Fully public reads, no accounts. Writes (``POST /api/runs``) are
self-served: the uploader picks their own opaque ``X-Upload-Token`` and
must present the same one again to update a run they already uploaded.

Every DB-touching handler opens its own connection/``Store`` rather than
sharing one across requests -- see ``db.connect()``'s docstring for why
(plain ``sqlite3.connect()``, no ``check_same_thread=False``, and
FastAPI's threadpool dispatches concurrent requests to different
threads). All synchronous SQLite work runs via ``run_in_threadpool`` so
it never blocks the event loop.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from postmortem.analysis.run_analyzer import analyze_run
from postmortem.combatlog.parser import parse_file
from postmortem.combatlog.segmenter import segment_runs
from postmortem.history.store import Store
from postmortem.mdt.decode import MDTDecodeError, decode_mdt_string
from postmortem.mdt.dungeon_data import DungeonDataStore
from postmortem.mdt.route import Route
from postmortem.report.html import render_html
from postmortem.report.index import render_index

from postmortem_site import config, db

# Cookie holding a browser's auto-generated upload token (see
# GET/POST /upload) -- separate from the X-Upload-Token header the
# JSON API (POST /api/runs) uses, since a plain HTML upload form has no
# way to set a custom header. Not marked Secure so it still works over
# plain http:// for local dev; force_https in fly.toml covers prod.
_UPLOAD_COOKIE = "pm_upload_token"

app = FastAPI(title=config.SITE_TITLE)

# The report pages embed a full JSON report and compress well as text
# (~86KB from ~1.2MB measured this session, roughly gzip's usual ratio
# on JSON/HTML). minimum_size=1000 skips compressing tiny responses
# (healthz, redirects) where gzip overhead isn't worth it.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# render_html()/render_index() embed the report/feed as JSON inside a
# <script> tag with only a minimal `</` escape -- not a full HTML
# sanitizer. These headers are cheap defense-in-depth against that
# known-imperfect escaping (e.g. inline-script execution stays possible
# via 'unsafe-inline' since the pages rely on inline <script>, but
# fetching third-party resources or framing the page is blocked). Fixing
# the embedding itself is out of scope here -- report/html.py and
# report/index.py are reused verbatim, not modified.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
}


@app.middleware("http")
async def _add_security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        for key, value in _SECURITY_HEADERS.items():
            response.headers[key] = value
    return response


def _ensure_runs_schema() -> None:
    """Make sure ``runs``/``players``/``deaths`` exist before a read path
    queries them directly (bypassing ``Store``). ``db.connect()``
    deliberately doesn't create these (see its docstring) -- normally
    they come into being via the first ``POST /api/runs``, but a brand
    new deployment's very first request could just as easily be
    ``GET /runs``, so every DB-touching handler calls this first.
    Constructing a ``Store`` just to run its idempotent
    ``CREATE TABLE IF NOT EXISTS`` DDL and closing it again is cheap.
    """
    Store(config.DB_PATH).close()


# -- simple pages ------------------------------------------------------


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/runs")


@app.get("/healthz")
async def healthz() -> dict:
    # Deliberately no DB touch -- used for Fly's health checks, kept
    # trivially fast.
    return {"ok": True}


_ABOUT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>About — Postmortem</title>
<style>
:root { --bg:#14161b; --panel:#1d2027; --line:#313746; --text:#d8dbe2;
  --dim:#8a90a0; --accent:#d7a94c; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:14px/1.6 "Segoe UI",system-ui,sans-serif; padding:32px;
  max-width:720px; }
h1 { color:var(--accent); font-size:22px; margin-bottom:4px; }
h2 { font-size:15px; margin:28px 0 8px; text-transform:uppercase;
  letter-spacing:.06em; color:var(--dim); }
code, pre { background:var(--panel); border:1px solid var(--line);
  border-radius:6px; }
code { padding:2px 6px; }
pre { padding:12px; overflow-x:auto; }
a { color:#5c9ad0; }
</style>
</head>
<body>
<h1>Postmortem — Public Runs</h1>
<p>A free, public post-mortem viewer for World of Warcraft Mythic+ runs.
Anyone can browse every uploaded run at <a href="/runs">/runs</a> — no
account needed, and reads are fully public.</p>
<h2>Uploading a run</h2>
<p>Easiest: <a href="/upload">upload your WoWCombatLog.txt directly</a> —
no install, nothing to run. Every completed Mythic+ key in it gets
analyzed and posted automatically.</p>
<p>Or analyze a combat log locally with
<a href="https://github.com/Sharpened-Banana/Postmortem">postmortem</a>,
then upload the resulting report:</p>
<pre>postmortem analyze &lt;log&gt; --upload https://this-site.example/api/runs</pre>
<p>Uploads are keyed by an <code>X-Upload-Token</code> header you choose
yourself — any non-empty string works. Keep it: re-uploading the same
run later (e.g. after a corrected re-analysis) requires presenting the
same token again, and a different token can't overwrite your run.
There's no signup and no password recovery — lose the token and you
lose the ability to update that run, but it stays visible either way.</p>
<h2>Notes</h2>
<p>Uploads are rate-limited per token and per IP. Analyzed reports
(<code>/api/runs</code>) are capped at 5MB; raw combat logs
(<code>/upload</code>) at 60MB. A raw log is only used to compute the
report — it's discarded immediately after, never stored.
Reports are shown exactly as uploaded — this site does no additional
verification of the underlying combat log.</p>
</body>
</html>
"""


@app.get("/about")
async def about() -> HTMLResponse:
    return HTMLResponse(_ABOUT_HTML)


# -- feed ----------------------------------------------------------------


def _load_feed(zone: Optional[str]) -> list[dict[str, Any]]:
    _ensure_runs_schema()
    conn = db.connect(config.DB_PATH)
    try:
        return db.feed_rows(conn, zone=zone, limit=config.FEED_LIMIT)
    finally:
        conn.close()


@app.get("/runs")
async def runs_page(zone: Optional[str] = None) -> HTMLResponse:
    rows = await run_in_threadpool(_load_feed, zone)
    return HTMLResponse(render_index(rows))


@app.get("/api/runs")
async def api_runs(zone: Optional[str] = None) -> JSONResponse:
    rows = await run_in_threadpool(_load_feed, zone)
    return JSONResponse(rows)


# -- per-run detail --------------------------------------------------------


def _load_report(run_id: int) -> Optional[dict[str, Any]]:
    store = Store(config.DB_PATH)
    try:
        return store.get_report(run_id)
    finally:
        store.close()


@app.get("/runs/{run_id}")
async def run_detail(run_id: int):
    report = await run_in_threadpool(_load_report, run_id)
    if report is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return HTMLResponse(render_html(report))


@app.get("/api/runs/{run_id}")
async def api_run_detail(run_id: int):
    report = await run_in_threadpool(_load_report, run_id)
    if report is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(report)


# -- upload ----------------------------------------------------------------


def _check_rate_limit(
    conn: Any, token_hash: str, remote_addr: Optional[str]
) -> Optional[dict[str, Any]]:
    """None if `token_hash`/`remote_addr` may upload right now, else the
    429 body to return. Split out of _handle_upload so a multi-run batch
    (see _handle_log_upload) can check this exactly once per upload
    *event* instead of once per run it contains -- otherwise the second
    run in the same log would immediately look rate-limited by the first
    run's own just-recorded upload.
    """
    now = time.time()

    last_token = db.last_upload_at(conn, token_hash)
    if last_token is not None and now - last_token < config.UPLOAD_MIN_INTERVAL_S:
        retry_after = round(config.UPLOAD_MIN_INTERVAL_S - (now - last_token), 1)
        return {"error": "rate limited: try again later", "retry_after_s": retry_after}

    last_ip = db.last_upload_at_ip(conn, remote_addr)
    if last_ip is not None and now - last_ip < config.IP_MIN_INTERVAL_S:
        retry_after = round(config.IP_MIN_INTERVAL_S - (now - last_ip), 1)
        return {"error": "rate limited: try again later", "retry_after_s": retry_after}

    return None


def _ingest_report(
    conn: Any,
    report: dict[str, Any],
    token_hash: str,
    remote_addr: Optional[str],
) -> tuple[int, dict[str, Any]]:
    """Ownership-checked ingest of one already-analyzed report. Assumes
    the caller already passed the rate-limit check and owns committing
    `conn` afterwards -- shared by the single-report JSON path
    (_handle_upload) and the multi-run log-upload path
    (_handle_log_upload), which commit once per request either way.
    """
    run = report.get("run")
    zone = run.get("zone") if isinstance(run, dict) else None
    start_ts = run.get("start_ts") if isinstance(run, dict) else None

    existing = db.existing_run(conn, zone, start_ts)
    if existing is not None:
        existing_run_id, existing_token_hash = existing
        if existing_token_hash is not None and existing_token_hash != token_hash:
            return 409, {"error": "already submitted by another uploader"}

    # Store opens its own connection against the same db_path -- see
    # db.connect()'s docstring on why connections aren't shared.
    # Store.ingest() commits internally before returning, so the
    # `runs` row record_upload() below references is already durable.
    store = Store(config.DB_PATH)
    try:
        run_id = store.ingest(report)
    finally:
        store.close()

    db.record_upload(conn, run_id, token_hash, remote_addr)
    return 200, {"ok": True, "run_id": run_id, "url": f"/runs/{run_id}"}


def _handle_upload(
    report: dict[str, Any],
    token_hash: str,
    remote_addr: Optional[str],
) -> tuple[int, dict[str, Any]]:
    """The synchronous half of POST /api/runs: rate-limit + ownership
    checks, then the actual ingest. Runs inside a threadpool (see the
    handler below) since it's all blocking SQLite work.

    Returns (status_code, body).
    """
    _ensure_runs_schema()
    conn = db.connect(config.DB_PATH)
    try:
        limited = _check_rate_limit(conn, token_hash, remote_addr)
        if limited is not None:
            return 429, limited

        status, body = _ingest_report(conn, report, token_hash, remote_addr)
        conn.commit()
        return status, body
    finally:
        conn.close()


@app.post("/api/runs")
async def create_run(request: Request) -> JSONResponse:
    # async def specifically so we can inspect the raw body/headers for
    # the size guard before FastAPI would otherwise parse it as a model.

    # Cheap up-front rejection when the client sends an honest
    # Content-Length -- avoids reading a huge body into memory at all.
    # Starlette does not enforce any body size cap on its own.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > config.MAX_BODY_BYTES:
                return JSONResponse({"error": "payload too large"}, status_code=413)
        except ValueError:
            pass

    body = await request.body()
    if len(body) > config.MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)

    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    for key in ("run", "dungeon", "players"):
        if key not in payload:
            return JSONResponse(
                {"error": f"missing required key: {key}"}, status_code=400
            )

    run = payload.get("run")
    if not isinstance(run, dict):
        return JSONResponse({"error": "run must be an object"}, status_code=400)

    zone = run.get("zone")
    start_ts = run.get("start_ts")
    # zone must be non-empty; start_ts just non-null (0 isn't a real
    # epoch timestamp for this game, but "is not None" is the actually
    # correct check, not truthiness). Both matter because Store.ingest()
    # keys its upsert on (zone, start_ts) using SQL `IS`, under which two
    # rows that both have NULL zone/start_ts would collide onto the same
    # row -- rejecting nulls here closes that off before it ever reaches
    # ingest().
    if not isinstance(zone, str) or not zone:
        return JSONResponse({"error": "run.zone is required"}, status_code=400)
    if start_ts is None:
        return JSONResponse({"error": "run.start_ts is required"}, status_code=400)

    token = (request.headers.get("x-upload-token") or "").strip()
    if not token:
        return JSONResponse(
            {"error": "X-Upload-Token header is required"}, status_code=401
        )
    # Never store/log the raw token -- only its hash.
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    status_code, body_dict = await run_in_threadpool(
        _handle_upload, payload, token_hash, _remote_addr(request)
    )
    return JSONResponse(body_dict, status_code=status_code)


# -- raw combat log upload (no local app/CLI needed) ------------------------
#
# The JSON API above (POST /api/runs) expects an already-analyzed report,
# which is what the desktop app and `postmortem analyze --upload` send.
# This section instead accepts a raw WoWCombatLog.txt directly from a
# plain HTML <input type=file> and does the analysis server-side --
# parse_file/segment_runs/analyze_run are pure stdlib Python with no WoW
# client dependency (the same reason the CLI has always worked outside
# the game), so nothing here is reimplemented, only reused. The raw log
# itself is never persisted: it's written to a temp file for parsing and
# deleted again before the response goes out -- only the derived report
# (same shape/size as what the JSON API stores today) ends up in the DB.


def _remote_addr(request: Request) -> Optional[str]:
    """Real client IP behind Fly's proxy, falling back to whatever ASGI
    gave us directly (e.g. running locally). Shared by every upload path."""
    remote_addr = request.headers.get("fly-client-ip")
    if not remote_addr:
        remote_addr = request.client.host if request.client else None
    return remote_addr


def _upload_token(request: Request) -> tuple[str, Optional[str]]:
    """(token, cookie_value_to_set) for a browser-based upload.

    A plain HTML form has no way to set the X-Upload-Token header the
    JSON API uses, so this mints one automatically and remembers it in a
    cookie -- the tester never sees or manages a token themselves.
    Returns (token, None) when the browser already has one (nothing new
    to set), or (token, token) the first time, so the caller knows to
    call response.set_cookie(...).
    """
    existing = request.cookies.get(_UPLOAD_COOKIE)
    if existing:
        return existing, None
    minted = secrets.token_hex(16)
    return minted, minted


_dungeon_store: Optional[DungeonDataStore] = None
_dungeon_store_loaded = False


def _get_dungeon_store() -> Optional[DungeonDataStore]:
    """The bundled MDT dungeon/enemy data (see config.DUNGEON_DATA_PATH),
    loaded once and cached -- it's static data, re-parsing a ~400KB JSON
    file on every upload would be pure waste. Tolerant of a missing/
    corrupt bundle (same "don't crash on our own local state" bar as
    every other loader in this codebase): forces/route-adherence just
    won't populate rather than the upload failing outright.

    A plain module-level cache, not per-request -- correct because this
    file never changes at runtime (only a redeploy with a re-extracted
    bundle changes it, which restarts the process anyway).
    """
    global _dungeon_store, _dungeon_store_loaded
    if not _dungeon_store_loaded:
        try:
            _dungeon_store = DungeonDataStore.load(config.DUNGEON_DATA_PATH)
        except (OSError, ValueError, KeyError):
            _dungeon_store = None
        _dungeon_store_loaded = True
    return _dungeon_store


def _decode_route_string(text: str) -> Route:
    """Decode a pasted MDT export string for /upload.

    Deliberately NOT cli.py's _load_route(): that helper also treats its
    argument as a possible *filesystem path* and reads whatever file
    exists there -- fine for a local CLI flag, but this text comes
    straight from an anonymous web upload, and silently reading an
    arbitrary local file that happens to match the pasted string would
    be a real (if narrow) local-file-disclosure primitive on a public
    server. Website uploaders can only ever paste a route string
    directly, never point at a path -- so this calls decode_mdt_string()
    directly instead, reusing the real MDT decoder without the file-path
    branch.

    Raises ValueError (not SystemExit -- there's no process to exit)
    with a message safe to show the uploader.
    """
    try:
        preset = decode_mdt_string(text)
    except MDTDecodeError as exc:
        raise ValueError(f"could not decode MDT route string: {exc}")
    return Route.from_preset(preset)


def _handle_log_upload(
    log_path: Path,
    token_hash: str,
    remote_addr: Optional[str],
    route_str: Optional[str] = None,
) -> tuple[int, dict[str, Any]]:
    """The synchronous half of POST /upload: parse the raw log, analyze
    every completed Mythic+ run it contains, and ingest each one.

    Every run gets the bundled dungeon data (see _get_dungeon_store()),
    so forces progress and (if `route_str` decodes) route-adherence
    comparison populate the same way a CLI/desktop-app analysis with
    --dungeon-data would -- a raw-log upload otherwise has no way to
    supply either. A bad/unparseable `route_str` is a soft failure: the
    batch still analyzes and uploads without route comparison, surfaced
    back as a "route_warning" rather than rejecting real data over one
    bad paste.

    Rate-limited once per upload *event* (see _check_rate_limit's
    docstring), not once per run -- a single log routinely contains
    several keys from one farming session. An ownership conflict on one
    run (e.g. a groupmate already uploaded the same key) only skips that
    run, not the whole batch. The one pasted route (if any) applies to
    every run in the batch -- fine for the common case (one key, or a
    farming session all in the same dungeon), not meaningful if the log
    spans multiple different dungeons.
    """
    _ensure_runs_schema()
    conn = db.connect(config.DB_PATH)
    try:
        limited = _check_rate_limit(conn, token_hash, remote_addr)
        if limited is not None:
            return 429, limited

        try:
            # parse_file(...) is a generator, fed straight into
            # segment_runs() without ever materializing the whole file's
            # events as a list -- segment_runs() itself only accumulates
            # events for whichever one run is currently open, so peak
            # memory here is bounded by one run's worth of the log, not
            # the entire (possibly hours-long) session's worth.
            segments = [s for s in segment_runs(parse_file(log_path)) if s.completed]
        except Exception:
            return 400, {"error": "could not read this as a WoWCombatLog.txt file"}

        if not segments:
            return 200, {
                "ok": True,
                "runs": [],
                "message": "no completed Mythic+ runs found in this log",
            }

        route: Optional[Route] = None
        route_warning: Optional[str] = None
        if route_str:
            try:
                route = _decode_route_string(route_str)
            except ValueError as exc:
                route_warning = str(exc)

        store = _get_dungeon_store()

        results = []
        for segment in segments:
            try:
                report = analyze_run(segment, route=route, store=store)
            except Exception:
                results.append({
                    "ok": False,
                    "error": "failed to analyze this run",
                    "zone": segment.zone_name,
                })
                continue
            _, body = _ingest_report(conn, report, token_hash, remote_addr)
            body["zone"] = report["run"].get("zone")
            body["level"] = report["run"].get("keystone_level")
            body["timed"] = report["run"].get("timed")
            results.append(body)

        conn.commit()
        result: dict[str, Any] = {"ok": True, "runs": results}
        if route_warning:
            result["route_warning"] = route_warning
        return 200, result
    finally:
        conn.close()


_UPLOAD_STYLE = """
:root { --bg:#14161b; --panel:#1d2027; --line:#313746; --text:#d8dbe2;
  --dim:#8a90a0; --accent:#d7a94c; --good:#5cb85c; --bad:#d9534f;
  --warn:#e0a13c; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:14px/1.6 "Segoe UI",system-ui,sans-serif; padding:32px;
  max-width:640px; }
h1 { color:var(--accent); font-size:22px; margin-bottom:4px; }
p.lead { color:var(--dim); }
a { color:#5c9ad0; }
.dropzone { border:2px dashed var(--line); border-radius:10px;
  padding:32px; text-align:center; margin:20px 0; background:var(--panel); }
.dropzone.drag { border-color:var(--accent); }
input[type=file] { color:var(--text); }
.field { margin:20px 0; }
.field label { display:block; font-size:13px; font-weight:600;
  margin-bottom:6px; }
.field textarea { width:100%; box-sizing:border-box; background:var(--panel);
  color:var(--text); border:1px solid var(--line); border-radius:8px;
  padding:10px 12px; font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  min-height:64px; resize:vertical; }
button { background:var(--accent); color:#14161b; border:none;
  border-radius:6px; padding:10px 18px; font-size:14px; font-weight:600;
  cursor:pointer; margin-top:12px; }
button:disabled { opacity:.5; cursor:default; }
ul.runs { list-style:none; padding:0; margin:16px 0; }
ul.runs li { background:var(--panel); border:1px solid var(--line);
  border-radius:8px; padding:12px 16px; margin-bottom:8px; }
.ok { color:var(--good); }
.bad { color:var(--bad); }
.warn { color:var(--warn); }
code { background:var(--panel); border:1px solid var(--line);
  border-radius:6px; padding:2px 6px; }
"""

_UPLOAD_FORM_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upload a run — Postmortem</title>
<style>{_UPLOAD_STYLE}</style>
</head>
<body>
<h1>Upload a run</h1>
<p class="lead">Pick your <code>WoWCombatLog.txt</code> directly — every completed
Mythic+ key in it gets analyzed and posted automatically. No install, no app.</p>
<form method="post" action="/upload" enctype="multipart/form-data">
  <div class="dropzone">
    <input type="file" name="logfile" accept=".txt" required>
  </div>
  <div class="field">
    <label>MDT route (optional)</label>
    <textarea name="route" placeholder="Paste an MDT export string here to also get route-adherence comparison for this log."></textarea>
  </div>
  <button type="submit">Analyze &amp; upload</button>
</form>
<p class="lead">Usually at
<code>World of Warcraft/_retail_/Logs/WoWCombatLog.txt</code>
(Mac: inside the WoW app's install folder; Windows: same relative path
under wherever WoW is installed).</p>
<p class="lead">Forces progress works automatically, no route needed —
every run is checked against the current season's dungeon data. Pasting
a route additionally compares your actual pulls against the plan (one
route applies to every run found in the log, so this is most useful for
a single-key upload).</p>
</body>
</html>
"""


def _run_result_li(run: dict[str, Any]) -> str:
    if run.get("ok"):
        zone = html.escape(str(run.get("zone") or "Unknown"))
        level = run.get("level")
        level_str = f"+{level} " if level is not None else ""
        timed = run.get("timed")
        verdict = "timed" if timed else ("depleted" if timed is not None else "")
        url = html.escape(str(run.get("url", "")))
        return (
            f'<li class="ok">✓ <a href="{url}">{level_str}{zone}</a> {verdict}</li>'
        )
    zone = html.escape(str(run.get("zone") or "a run"))
    error = html.escape(str(run.get("error", "upload failed")))
    return f'<li class="bad">✗ {zone}: {error}</li>'


def _render_upload_result(status_code: int, body: dict[str, Any]) -> str:
    if status_code == 429:
        message = (
            f"<p class=\"bad\">{html.escape(body.get('error', 'rate limited'))} "
            f"— try again in {body.get('retry_after_s', '?')}s.</p>"
        )
    elif status_code != 200:
        message = f"<p class=\"bad\">{html.escape(body.get('error', 'upload failed'))}</p>"
    elif not body.get("runs"):
        message = f"<p>{html.escape(body.get('message', 'no completed runs found'))}</p>"
    else:
        items = "".join(_run_result_li(r) for r in body["runs"])
        message = f"<ul class=\"runs\">{items}</ul>"

    route_warning = body.get("route_warning")
    if route_warning:
        message += (
            f'<p class="warn">⚠ Uploaded without route comparison — '
            f"{html.escape(route_warning)}</p>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upload results — Postmortem</title>
<style>{_UPLOAD_STYLE}</style>
</head>
<body>
<h1>Upload results</h1>
{message}
<p><a href="/upload">Upload another log</a> · <a href="/runs">Browse all runs</a></p>
</body>
</html>
"""


@app.get("/upload")
async def upload_form() -> HTMLResponse:
    return HTMLResponse(_UPLOAD_FORM_HTML)


# Chunk size for streaming an upload straight to disk (see upload_log
# below). Arbitrary but reasonable: big enough that chunk-loop overhead
# is negligible, small enough that peak memory for this step never
# exceeds ~1MB regardless of how large MAX_LOG_BYTES is configured --
# unlike a single logfile.read(MAX_LOG_BYTES + 1) call, which held the
# *entire* upload in memory at once and made raising the cap for a real
# multi-hour session's log a genuine OOM risk on this service's 512MB VM.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


@app.post("/upload")
async def upload_log(
    request: Request,
    logfile: UploadFile = File(...),
    route: str = Form(""),
) -> HTMLResponse:
    fd, tmp_name = tempfile.mkstemp(suffix=".txt")
    total = 0
    too_large = False
    try:
        with os.fdopen(fd, "wb") as fh:
            while True:
                chunk = await logfile.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.MAX_LOG_BYTES:
                    too_large = True
                    break
                fh.write(chunk)

        if too_large:
            return HTMLResponse(
                _render_upload_result(413, {"error": "that file is too large"}),
                status_code=413,
            )
        if total == 0:
            return HTMLResponse(
                _render_upload_result(400, {"error": "no file received"}),
                status_code=400,
            )

        token, new_cookie = _upload_token(request)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        remote_addr = _remote_addr(request)

        status_code, body = await run_in_threadpool(
            _handle_log_upload, Path(tmp_name), token_hash, remote_addr, route.strip() or None,
        )
    finally:
        os.unlink(tmp_name)

    response = HTMLResponse(
        _render_upload_result(status_code, body),
        status_code=status_code if status_code in (413, 400, 429) else 200,
    )
    if new_cookie is not None:
        response.set_cookie(
            _UPLOAD_COOKIE, new_cookie,
            max_age=3650 * 24 * 3600, httponly=True, samesite="lax",
        )
    return response
