"""The public Mythic Analyzer site: FastAPI app.

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
import json
import time
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from mythic_analyzer.history.store import Store
from mythic_analyzer.report.html import render_html
from mythic_analyzer.report.index import render_index

from mythic_site import config, db

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
<title>About — Mythic Analyzer</title>
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
<h1>Mythic Analyzer — Public Runs</h1>
<p>A free, public post-mortem viewer for World of Warcraft Mythic+ runs.
Anyone can browse every uploaded run at <a href="/runs">/runs</a> — no
account needed, and reads are fully public.</p>
<h2>Uploading a run</h2>
<p>Analyze a combat log locally with
<a href="https://github.com/Sharpened-Banana/Mythic-Analyzer">mythic-analyzer</a>,
then upload the resulting report:</p>
<pre>mythic-analyzer analyze &lt;log&gt; --upload https://this-site.example/api/runs</pre>
<p>Uploads are keyed by an <code>X-Upload-Token</code> header you choose
yourself — any non-empty string works. Keep it: re-uploading the same
run later (e.g. after a corrected re-analysis) requires presenting the
same token again, and a different token can't overwrite your run.
There's no signup and no password recovery — lose the token and you
lose the ability to update that run, but it stays visible either way.</p>
<h2>Notes</h2>
<p>Uploads are rate-limited per token and per IP, and capped at 5MB.
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


def _handle_upload(
    report: dict[str, Any],
    zone: str,
    start_ts: Any,
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
        now = time.time()

        last_token = db.last_upload_at(conn, token_hash)
        if last_token is not None and now - last_token < config.UPLOAD_MIN_INTERVAL_S:
            retry_after = round(config.UPLOAD_MIN_INTERVAL_S - (now - last_token), 1)
            return 429, {
                "error": "rate limited: try again later",
                "retry_after_s": retry_after,
            }

        last_ip = db.last_upload_at_ip(conn, remote_addr)
        if last_ip is not None and now - last_ip < config.IP_MIN_INTERVAL_S:
            retry_after = round(config.IP_MIN_INTERVAL_S - (now - last_ip), 1)
            return 429, {
                "error": "rate limited: try again later",
                "retry_after_s": retry_after,
            }

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
        conn.commit()
        return 200, {"ok": True, "run_id": run_id, "url": f"/runs/{run_id}"}
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

    # Fly's own header for the real client IP behind its proxy; fall
    # back to whatever ASGI gave us directly (e.g. running locally).
    remote_addr = request.headers.get("fly-client-ip")
    if not remote_addr:
        remote_addr = request.client.host if request.client else None

    status_code, body_dict = await run_in_threadpool(
        _handle_upload, payload, zone, start_ts, token_hash, remote_addr
    )
    return JSONResponse(body_dict, status_code=status_code)
