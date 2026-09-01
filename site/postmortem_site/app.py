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
import re
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
        "style-src 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src data:; frame-ancestors 'none'"
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


# -- shared site chrome (nav, footer, fonts) --------------------------------
#
# report/html.py's render_html() and report/index.py's render_index()
# are reused *verbatim* -- unmodified -- by the CLI (a standalone saved
# .html file has no live site behind it) and the desktop app (rendered
# in-process via analyze()/list_history() and shown in a sandboxed
# iframe -- it never makes an HTTP request to this site's routes at
# all). So this site's own nav/footer/landing-page chrome is never added
# to those shared modules -- only spliced onto *this site's* HTTP
# responses below (see _inject_chrome()), and only additively: it adds
# a few :root tokens neither module already defines (--radius,
# --accent-ink, --accent-dim) and scopes every rule to .site-* classes
# that don't exist in their own markup, so it can never override
# anything in their already-working, desktop-app-shared visual design.

_FONTS_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    "family=Chakra+Petch:wght@500;600;700"
    "&family=IBM+Plex+Sans:wght@400;500;600"
    '&display=swap" rel="stylesheet">'
)

# Chrome-only: safe to inject into report/html.py's or report/index.py's
# own complete documents (see the module note above) -- every selector
# is scoped to .site-*, and the one :root block only adds tokens neither
# module already declares, never redeclaring --bg/--panel/etc. with a
# value that could conflict with theirs.
_CHROME_STYLE = """
:root { --radius:10px; --radius-sm:6px; --accent-ink:#1c1608;
  --accent-dim:rgba(215,169,76,.14); }
.site-header { display:flex; align-items:center; justify-content:space-between;
  gap:16px; padding:0 28px; height:60px; background:var(--panel);
  border-bottom:1px solid var(--line); }
.site-brand { display:flex; align-items:center; gap:9px;
  font-family:"Chakra Petch",system-ui,sans-serif; font-size:18px;
  font-weight:700; color:var(--text); letter-spacing:.01em;
  text-decoration:none; }
.site-brand:hover { text-decoration:none; }
.site-brand .mark { color:var(--accent); font-size:20px; }
.site-nav { display:flex; align-items:center; gap:4px; }
.site-nav a { color:var(--dim); font-size:13.5px; font-weight:600;
  padding:8px 14px; border-radius:var(--radius-sm); text-decoration:none;
  transition:background .12s ease,color .12s ease; }
.site-nav a:hover { background:var(--panel2,var(--panel)); color:var(--text);
  text-decoration:none; }
.site-nav a.active { background:var(--accent-dim); color:var(--accent); }
.site-nav a.cta { background:var(--accent); color:var(--accent-ink); margin-left:6px; }
.site-nav a.cta:hover { background:#e2b75c; }
.site-footer { border-top:1px solid var(--line); padding:26px 28px;
  margin-top:56px; color:var(--dim); font-size:12.5px; text-align:center; }
.site-footer a { color:var(--dim); }
.site-footer a:hover { color:var(--text); }
.site-download-bar { padding:14px 28px 0; }
.site-download-link { color:var(--dim); font-size:13px; font-weight:600;
  text-decoration:none; }
.site-download-link:hover { color:var(--accent); text-decoration:none; }
"""

# Full page style, for pages this module builds outright (landing,
# about, upload) -- not injected into the shared renderers, so free to
# set real typography/layout without the "additive only" constraint
# _CHROME_STYLE has.
_PAGE_STYLE = _CHROME_STYLE + """
:root { --bg:#14161b; --panel:#1d2027; --panel2:#232733; --line:#313746;
  --text:#d8dbe2; --dim:#8a90a0; --accent:#d7a94c; --good:#5cb85c;
  --bad:#d9534f; --warn:#e0a13c; --blue:#5c9ad0;
  --good-dim:rgba(88,196,124,.14); --bad-dim:rgba(224,96,96,.14); }
* { box-sizing:border-box; }
html, body { margin:0; }
body { background:var(--bg); color:var(--text);
  font:15px/1.6 "IBM Plex Sans",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased; }
h1, h2, h3 { font-family:"Chakra Petch",system-ui,sans-serif; margin:0; }
a { color:var(--blue); text-decoration:none; }
a:hover { text-decoration:underline; }
code, pre { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
.wrap { max-width:1080px; margin:0 auto; padding:0 28px; }
"""


def _site_nav(active: str) -> str:
    links = [
        ("home", "/", "Home"),
        ("runs", "/runs", "Browse Runs"),
        ("guide", "/guide", "Guide"),
        ("about", "/about", "About"),
    ]
    items = "".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">{label}</a>'
        for key, href, label in links
    )
    upload_active = " active" if active == "upload" else ""
    return f"""<header class="site-header">
  <a href="/" class="site-brand"><span class="mark">⚔</span> Postmortem</a>
  <nav class="site-nav">
    {items}
    <a href="/upload" class="cta{upload_active}">Upload a run</a>
  </nav>
</header>"""


def _site_footer() -> str:
    return """<footer class="site-footer">
  Postmortem — a free, public Mythic+ post-mortem tool ·
  <a href="https://github.com/Sharpened-Banana/postmortem">Source on GitHub</a> ·
  <a href="/about">About</a>
</footer>"""


def _page(title: str, active: str, body: str) -> str:
    """Full page shell for pages this module authors directly (landing,
    about, upload): fonts, shared style, nav, body, footer."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
{_FONTS_LINK}
<style>{_PAGE_STYLE}</style>
</head>
<body>
{_site_nav(active)}
{body}
{_site_footer()}
</body>
</html>
"""


def _inject_chrome(rendered_html: str, active: str, extra_nav: str = "") -> str:
    """Splice the shared nav/footer onto report/html.py's or
    report/index.py's own already-complete HTML document -- see the
    module note above on why those two modules are never modified
    directly. Their own <style>/:root and body content render exactly
    as they always have; this only adds chrome around them.

    ``extra_nav``, when given, is a small extra chrome element (its own
    .site-* scoped markup) placed right after the header -- e.g.
    run_detail()'s "Download HTML" link. A separate element rather than
    something spliced into _site_nav() itself, since _site_nav() is
    shared by every page (including the ones this module builds outright
    via _page()) and this is specific to whichever single call site
    passes it.
    """
    with_style = rendered_html.replace(
        "</head>", f"{_FONTS_LINK}<style>{_CHROME_STYLE}</style></head>", 1,
    )
    with_nav = with_style.replace(
        "<body>", f"<body>{_site_nav(active)}{extra_nav}", 1,
    )
    return with_nav.replace("</body>", f"{_site_footer()}</body>", 1)


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


# -- landing page ------------------------------------------------------

_LANDING_CSS = """
.hero { padding:72px 0 56px; text-align:center; }
.hero h1 { font-size:44px; line-height:1.15; margin-bottom:16px; }
.hero h1 .accent { color:var(--accent); }
.hero .tagline { color:var(--dim); font-size:17px; max-width:620px;
  margin:0 auto 32px; }
.hero-ctas { display:flex; gap:12px; justify-content:center; flex-wrap:wrap; }
.btn { display:inline-flex; align-items:center; gap:8px; font:inherit;
  font-weight:600; font-size:14.5px; padding:12px 22px; border-radius:var(--radius-sm);
  border:1px solid var(--line); cursor:pointer; }
.btn:hover { text-decoration:none; }
.btn-primary { background:var(--accent); color:var(--accent-ink);
  border-color:var(--accent); }
.btn-primary:hover { background:#e2b75c; }
.btn-secondary { background:var(--panel); color:var(--text); }
.btn-secondary:hover { background:var(--panel2); }

.stat-strip { display:flex; justify-content:center; gap:48px; flex-wrap:wrap;
  padding:28px 0; border-top:1px solid var(--line);
  border-bottom:1px solid var(--line); margin-bottom:64px; }
.stat-strip .stat { text-align:center; }
.stat-strip .stat b { display:block; font-family:"Chakra Petch",system-ui,sans-serif;
  font-size:28px; color:var(--accent); }
.stat-strip .stat span { color:var(--dim); font-size:12.5px;
  text-transform:uppercase; letter-spacing:.06em; }

.section { margin-bottom:72px; }
.section h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em;
  color:var(--dim); text-align:center; margin-bottom:36px; }

.steps { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
  gap:20px; }
.step { background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius); padding:24px; }
.step .n { font-family:"Chakra Petch",system-ui,sans-serif; font-size:13px;
  font-weight:700; color:var(--accent); letter-spacing:.06em; margin-bottom:10px; }
.step h3 { font-size:16px; margin-bottom:8px; }
.step p { color:var(--dim); font-size:13.5px; margin:0; }

.feature-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
  gap:16px; }
.feature { padding:18px 20px; background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius); }
.feature h3 { font-size:14px; margin-bottom:6px; }
.feature p { color:var(--dim); font-size:13px; margin:0; }

.recent-list { display:flex; flex-direction:column; gap:8px; }
.recent-row { display:flex; align-items:center; gap:14px; padding:13px 18px;
  background:var(--panel); border:1px solid var(--line); border-radius:var(--radius-sm); }
.recent-row:hover { border-color:var(--dim); text-decoration:none; }
.recent-row .zone { font-weight:600; color:var(--text); flex:1; }
.recent-row .lvl { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  color:var(--dim); }
.recent-empty { text-align:center; color:var(--dim); padding:32px;
  background:var(--panel); border:1px dashed var(--line); border-radius:var(--radius); }

/* matches report/html.py's/report/index.py's own .badge convention,
   redeclared here since the landing page uses _PAGE_STYLE, not theirs */
.badge { display:inline-block; padding:2px 10px; border-radius:12px;
  font-weight:600; font-size:11.5px; }
.badge.timed { background:#1d3a28; color:var(--good); }
.badge.over { background:#3a2d1d; color:var(--warn); }
.badge.neutral { background:var(--panel2); color:var(--dim); }
"""


def _landing_stat(value: Any, label: str) -> str:
    return f'<div class="stat"><b>{html.escape(str(value))}</b><span>{html.escape(label)}</span></div>'


def _render_landing(rows: list[dict[str, Any]]) -> str:
    total = len(rows)
    timed = sum(1 for r in rows if r.get("timed"))
    zones = len({r.get("zone") for r in rows if r.get("zone")})

    if rows:
        recent_items = "".join(
            f'<a class="recent-row" href="{html.escape(str(r.get("html") or "/runs"))}">'
            f'<span class="zone">{html.escape(str(r.get("zone") or "Unknown"))}</span>'
            f'<span class="lvl">+{html.escape(str(r.get("level") or "?"))}</span>'
            f'<span class="badge {"timed" if r.get("timed") else ("over" if r.get("timed") is False else "neutral")}">'
            f'{"timed" if r.get("timed") else ("over timer" if r.get("timed") is False else "incomplete")}</span>'
            f"</a>"
            for r in rows[:6]
        )
        recent = f'<div class="recent-list">{recent_items}</div>'
    else:
        recent = ('<div class="recent-empty">No runs uploaded yet — '
                   '<a href="/upload">be the first</a>.</div>')

    body = f"""
<style>{_LANDING_CSS}</style>
<div class="wrap">
  <section class="hero">
    <h1>See exactly what happened<br>on your <span class="accent">Mythic+ key</span>.</h1>
    <p class="tagline">Upload a combat log and get a full breakdown: route
    deviations, deaths with killing-blow recaps, kick efficiency, forces
    progress, and timer pace — shareable with your group in one link.</p>
    <div class="hero-ctas">
      <a class="btn btn-primary" href="/upload">Upload a run</a>
      <a class="btn btn-secondary" href="/runs">Browse runs</a>
    </div>
  </section>

  <div class="stat-strip">
    {_landing_stat(total, "runs tracked")}
    {_landing_stat(timed, "timed")}
    {_landing_stat(zones, "dungeons seen")}
  </div>

  <section class="section">
    <h2>How it works</h2>
    <div class="steps">
      <div class="step"><div class="n">01</div>
        <h3>Install the addon</h3>
        <p>A companion WoW addon auto-manages combat logging — turns on the
        moment your key starts, off when it ends. Nothing to remember.</p></div>
      <div class="step"><div class="n">02</div>
        <h3>Upload your log</h3>
        <p>Drop <code>WoWCombatLog.txt</code> here directly, no install needed
        — or use the desktop app's "Watch Live" mode to upload every key
        automatically as you play.</p></div>
      <div class="step"><div class="n">03</div>
        <h3>Share the report</h3>
        <p>Get a link with the full breakdown — pulls vs. plan, deaths,
        damage, kicks, forces, timer pace. Public, no account needed.</p></div>
    </div>
  </section>

  <section class="section">
    <h2>What you get</h2>
    <div class="feature-grid">
      <div class="feature"><h3>Route vs. actual</h3>
        <p>Every pull matched against your planned MDT route — early,
        off-route, or missed packs, plus adherence %.</p></div>
      <div class="feature"><h3>Deaths, with recaps</h3>
        <p>Exact killing blow and damage taken in the 5s before every death.</p></div>
      <div class="feature"><h3>Kick efficiency</h3>
        <p>Which casts got through, prevented damage/healing, and confirmed
        uninterruptible casts filtered out automatically.</p></div>
      <div class="feature"><h3>Forces &amp; timer</h3>
        <p>Enemy-forces progress and timer pace, using real season dungeon
        data — no setup required.</p></div>
    </div>
  </section>

  <section class="section">
    <h2>Recent runs</h2>
    {recent}
  </section>
</div>
"""
    return _page("Postmortem — Mythic+ post-mortems", "home", body)


@app.get("/")
async def root() -> HTMLResponse:
    rows = await run_in_threadpool(_load_feed, None)
    return HTMLResponse(_render_landing(rows))


@app.get("/healthz")
async def healthz() -> dict:
    # Deliberately no DB touch -- used for Fly's health checks, kept
    # trivially fast.
    return {"ok": True}


_ABOUT_BODY = """
<div class="wrap" style="max-width:720px;padding-top:48px;padding-bottom:24px;">
<h1 style="font-size:26px;color:var(--accent);margin-bottom:6px;">About</h1>
<p style="color:var(--dim);">A free, public post-mortem viewer for World of
Warcraft Mythic+ runs. Anyone can browse every uploaded run at
<a href="/runs">/runs</a> — no account needed, and reads are fully public.</p>

<h2 style="font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:32px 0 10px;">Uploading a run</h2>
<p>Easiest: <a href="/upload">upload your WoWCombatLog.txt directly</a> —
no install, nothing to run. Every completed Mythic+ key in it gets
analyzed and posted automatically, with forces progress populated
automatically from real season dungeon data.</p>
<p>Or analyze a combat log locally with
<a href="https://github.com/Sharpened-Banana/postmortem">postmortem</a>,
then upload the resulting report:</p>
<pre style="background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px;overflow-x:auto;">postmortem analyze &lt;log&gt; --upload https://this-site.example/api/runs</pre>
<p>Uploads are keyed by an upload token you never have to think about — the
website upload page and the CLI both generate one automatically on first
use (an <code>X-Upload-Token</code> header, if you're calling
<code>POST /api/runs</code> directly). Keep whatever created it:
re-uploading the same run later (e.g. after a corrected re-analysis)
requires the same token, and a different one can't overwrite your run.
There's no signup and no password recovery — lose the token and you lose
the ability to update that run, but it stays visible either way.</p>

<h2 style="font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:32px 0 10px;">Notes</h2>
<p>Uploads are rate-limited per token and per IP. Analyzed reports
(<code>/api/runs</code>) are capped at 5MB; raw combat logs
(<code>/upload</code>) at 500MB per upload — a sanity limit on the whole
request, not a memory one, so a log with several separate keys in it,
even a full evening's worth, is fine well under that. The real per-run
limit is separate: any single extremely long continuous key is
analyzed up to a size that's safe to hold in memory, and reported as a
failed run (with the rest of the log's keys still processed normally)
if it goes over — the desktop app/CLI has no such limit for that
specific key. A raw log is only used to compute the
report — it's discarded immediately after, never stored.
Reports are shown exactly as uploaded — this site does no additional
verification of the underlying combat log.</p>
</div>
"""


@app.get("/about")
async def about() -> HTMLResponse:
    return HTMLResponse(_page("About — Postmortem", "about", _ABOUT_BODY))


# -- guide (non-technical, click-by-click walkthrough + FAQ) ---------------
#
# Distinct from About above: About is a project-overview/reference page
# (tokens, rate limits, exact byte caps) for anyone curious how the site
# works. This is the page to hand a non-technical friend who's never done
# this before -- illustrated steps, zero jargon, nothing assumed. --teal/
# --teal-dim are the only tokens not already in _PAGE_STYLE (via
# _CHROME_STYLE); everything else reuses the site's existing palette.
_GUIDE_CSS = """
.guide-wrap { max-width:680px; padding-top:48px; padding-bottom:8px; }
.guide-wrap { --teal:#4ecdb0; --teal-dim:rgba(78,205,176,.14); }
.guide-wrap h1 { font-size:26px; color:var(--text); margin-bottom:6px; }
.guide-wrap p.lead { color:var(--dim); max-width:480px; }
.guide-badges { display:flex; gap:10px; margin:22px 0 8px; flex-wrap:wrap; }
.guide-badge { display:flex; align-items:center; gap:7px; font-size:12.5px; font-weight:600;
  color:var(--dim); background:var(--panel); border:1px solid var(--line);
  border-radius:999px; padding:7px 13px; }
.guide-steps { display:flex; flex-direction:column; gap:16px; margin-top:28px; }
.guide-step { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius);
  padding:22px 22px 24px; }
.guide-step.done { border-color:rgba(78,205,176,.4);
  background:linear-gradient(180deg,rgba(78,205,176,.06),var(--panel)); }
.guide-step-head { display:flex; align-items:center; gap:12px; margin-bottom:6px; }
.guide-num { flex:none; width:30px; height:30px; border-radius:999px; background:var(--accent-dim);
  border:1px solid rgba(215,169,76,.4); color:var(--accent); font-weight:700; font-size:14px;
  display:flex; align-items:center; justify-content:center; }
.guide-step.done .guide-num { background:var(--teal-dim); border-color:rgba(78,205,176,.45); color:var(--teal); }
.guide-step h2 { font-size:16.5px; color:var(--text); margin:0; }
.guide-step-body { color:var(--dim); margin:0 0 0 42px; }
.guide-step-body p { margin:0 0 8px; }
.guide-step-body p:last-child { margin-bottom:0; }
.guide-step-body strong { color:var(--text); }
.guide-illus { margin:14px 0 0 42px; background:var(--panel2); border:1px solid var(--line);
  border-radius:var(--radius-sm); padding:14px 16px; }
.guide-path { display:flex; align-items:center; flex-wrap:wrap; gap:6px; font-size:13px; }
.guide-crumb { background:var(--bg); border:1px solid var(--line); border-radius:6px;
  padding:5px 9px; color:var(--text); font-weight:500; }
.guide-sep { color:var(--dim); }
.guide-check-row { display:flex; align-items:center; gap:10px; font-size:14px; color:var(--text); }
.guide-checkbox { width:16px; height:16px; border-radius:4px; background:var(--teal);
  flex:none; display:inline-block; text-align:center; line-height:16px; color:#0d2a24; font-size:11px; }
.guide-dropzone { border:2px dashed rgba(78,205,176,.45); border-radius:10px; padding:18px;
  text-align:center; background:rgba(78,205,176,.05); color:var(--dim); font-size:13.5px; }
.guide-dropzone .fname { display:inline-block; margin-top:8px; font-size:12px; color:var(--dim);
  background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:3px 8px; }
.guide-path-hint { margin:12px 0 0 42px; font-size:13px; color:var(--dim); }
.guide-cta-row { margin:16px 0 0 42px; }
.guide-cta { display:inline-block; background:var(--accent); color:var(--accent-ink);
  font-weight:700; font-size:14px; text-decoration:none; padding:10px 20px;
  border-radius:999px; }
.guide-cta:hover { background:#e2b75c; text-decoration:none; }
.guide-faq { margin-top:44px; }
.guide-faq h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--dim);
  margin-bottom:14px; }
.guide-qa { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius-sm);
  padding:14px 16px; margin-bottom:10px; }
.guide-qa .q { font-weight:600; color:var(--text); font-size:14px; margin-bottom:4px; }
.guide-qa .a { color:var(--dim); font-size:13.5px; margin:0; }
@media (max-width:520px) {
  .guide-step-body, .guide-illus, .guide-path-hint, .guide-cta-row { margin-left:0; }
}
"""

_GUIDE_BODY = f"""
<style>{_GUIDE_CSS}</style>
<div class="wrap guide-wrap">
<h1>Getting your log to Postmortem</h1>
<p class="lead">Five steps, no installs, no addons required — just your normal game and a web
browser. Send this page to anyone you want a log from.</p>

<div class="guide-badges">
  <span class="guide-badge">About 5 minutes</span>
  <span class="guide-badge">Nothing to install</span>
  <span class="guide-badge">Raw log never stored — see below</span>
</div>

<div class="guide-steps">

  <div class="guide-step">
    <div class="guide-step-head"><span class="guide-num">1</span><h2>Make sure your combat log is turned on</h2></div>
    <div class="guide-step-body">
      <p>In game, press <strong>Esc</strong> &rarr; <strong>Options</strong> &rarr;
      <strong>System</strong> &rarr; <strong>Network</strong>, and make sure
      <strong>Advanced Combat Logging</strong> is checked. You only have to do this once —
      it stays on.</p>
    </div>
    <div class="guide-illus">
      <div class="guide-check-row"><span class="guide-checkbox">✓</span> Advanced Combat Logging</div>
    </div>
  </div>

  <div class="guide-step">
    <div class="guide-step-head"><span class="guide-num">2</span><h2>Play your key or raid as normal</h2></div>
    <div class="guide-step-body">
      <p>That's it — nothing else to do while you play. The game quietly writes everything to
      a file in the background.</p>
    </div>
  </div>

  <div class="guide-step">
    <div class="guide-step-head"><span class="guide-num">3</span><h2>Find the log file</h2></div>
    <div class="guide-step-body">
      <p>When you're done playing, open the <strong>Battle.net</strong> app. Click
      <strong>World of Warcraft</strong> on the left, then the small <strong>gear icon</strong>
      next to the orange Play button, then <strong>Show in Explorer</strong> (Mac: <strong>Show
      in Finder</strong>).</p>
      <p>That opens your WoW folder directly — no typing needed. From there, open
      <strong>_retail_</strong>, then <strong>Logs</strong>. Your file is called
      <strong>WoWCombatLog.txt</strong>.</p>
    </div>
    <div class="guide-illus">
      <div class="guide-path">
        <span class="guide-crumb">gear icon</span><span class="guide-sep">&rarr;</span>
        <span class="guide-crumb">Show in Explorer</span><span class="guide-sep">&rarr;</span>
        <span class="guide-crumb">_retail_</span><span class="guide-sep">&rarr;</span>
        <span class="guide-crumb">Logs</span>
      </div>
    </div>
    <p class="guide-path-hint">Prefer to type it? On Windows it's usually at
    <code>C:\\Program Files (x86)\\World of Warcraft\\_retail_\\Logs</code>; on Mac, inside the
    WoW app's own install folder at the same relative path.</p>
  </div>

  <div class="guide-step">
    <div class="guide-step-head"><span class="guide-num">4</span><h2>Upload the file</h2></div>
    <div class="guide-step-body">
      <p>Go to the <a href="/upload">upload page</a> and drop <strong>WoWCombatLog.txt</strong>
      onto it, or click to browse and pick it. Then click <strong>Analyze &amp; upload</strong>.</p>
    </div>
    <div class="guide-illus">
      <div class="guide-dropzone">Drop your file here<br>
        <span class="fname">WoWCombatLog.txt</span></div>
    </div>
    <div class="guide-cta-row"><a class="guide-cta" href="/upload">Open the upload page</a></div>
  </div>

  <div class="guide-step done">
    <div class="guide-step-head"><span class="guide-num">✓</span><h2>Done — grab your link</h2></div>
    <div class="guide-step-body">
      <p>Every completed key in the log gets analyzed automatically. When it finishes, you'll
      see a link to each run's report — that's the one to share.</p>
    </div>
  </div>

</div>

<div class="guide-faq">
<h2>If something looks off</h2>
<div class="guide-qa">
  <div class="q">Nothing showed up after uploading</div>
  <p class="a">Advanced Combat Logging was probably off during that key — check step 1, then
  upload your next one.</p>
</div>
<div class="guide-qa">
  <div class="q">It says a key started but never finished</div>
  <p class="a">If the group voted to abandon that key, this is expected — WoW doesn't log an
  ending for an abandoned key, only for one that actually times or depletes, so there's genuinely
  nothing to analyze from it. If the key actually finished normally, the log may have been
  grabbed before WoW wrote the ending.</p>
</div>
<div class="guide-qa">
  <div class="q">It says the file is too big</div>
  <p class="a">Only happens with one very long, unbroken key. A log with several keys in it —
  even a whole night's worth — is fine well under the limit.</p>
</div>
<div class="guide-qa">
  <div class="q">Is anything private in there?</div>
  <p class="a">The raw file itself is never kept — only the finished report (damage, deaths,
  timing) gets saved, and the raw log is discarded the moment it's processed.</p>
</div>
<div class="guide-qa">
  <div class="q">Do I need the desktop app or addon?</div>
  <p class="a">No — this page and the upload form are the whole thing. The desktop app and WoW
  addon exist too, for anyone who wants automatic per-key uploads while they play, but neither
  is required just to send a log.</p>
</div>
</div>
</div>
"""


@app.get("/guide")
async def guide() -> HTMLResponse:
    return HTMLResponse(_page("Guide — Postmortem", "guide", _GUIDE_BODY))


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
    return HTMLResponse(_inject_chrome(render_index(rows), "runs"))


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
    download_bar = (
        f'<div class="wrap site-download-bar">'
        f'<a class="site-download-link" href="/runs/{run_id}/download" download>'
        f"⬇ Download HTML</a></div>"
    )
    return HTMLResponse(
        _inject_chrome(render_html(report), "runs", extra_nav=download_bar)
    )


@app.get("/api/runs/{run_id}")
async def api_run_detail(run_id: int):
    report = await run_in_threadpool(_load_report, run_id)
    if report is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(report)


def _download_filename(report: dict[str, Any]) -> str:
    """Mirrors recorder.py's own saved-run naming convention
    (stamp_zone_level) so a run downloaded from the site looks like one
    saved locally by the CLI/desktop app -- same shape, same idea."""
    run = report.get("run") or {}
    safe_zone = re.sub(r"[^A-Za-z0-9]+", "", run.get("zone") or "") or "run"
    level = run.get("keystone_level")
    start_ts = run.get("start_ts")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(start_ts)) if start_ts else "report"
    return f"{stamp}_{safe_zone}_{level if level is not None else 'x'}.html"


@app.get("/runs/{run_id}/download")
async def run_download(run_id: int):
    report = await run_in_threadpool(_load_report, run_id)
    if report is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    # The *unwrapped* render_html() output -- not _inject_chrome()'s
    # site-branded version. A downloaded file is meant to be the same
    # portable, self-contained report the CLI/desktop app produce (see
    # the module note near _inject_chrome() on why report/html.py's own
    # output is always already a complete document on its own); site nav
    # pointing back at a live URL doesn't belong in a file someone saves
    # or forwards to someone else.
    filename = _download_filename(report)
    return Response(
        content=render_html(report),
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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

        route: Optional[Route] = None
        route_warning: Optional[str] = None
        if route_str:
            try:
                route = _decode_route_string(route_str)
            except ValueError as exc:
                route_warning = str(exc)

        store = _get_dungeon_store()

        # segment_iter is consumed one segment at a time below via a
        # manual next() loop -- NOT `for segment in segment_iter`, and
        # NOT `[s for s in segment_iter if s.completed]` (a real bug,
        # fixed here: that first "fix" for this exact memory problem
        # only stopped materializing the raw *events* list, but a list
        # comprehension over segment_runs() still fully drains the
        # generator into one list holding every run's own events
        # simultaneously before analysis ever starts. Confirmed by
        # actually reproducing it: a 372MB/15,000-run synthetic log hit
        # 2.5GB peak RSS on just the first couple of runs. The manual
        # next() loop is what actually keeps peak memory bounded to one
        # run at a time, verified the same way after this fix).
        #
        # max_run_events=config.MAX_RUN_EVENTS is the real per-run
        # memory-safety cap (see config.py's own comment) -- a single
        # CONTINUOUS run gets cut off and yielded truncated=True instead
        # of accumulating unboundedly, independent of config.MAX_LOG_BYTES
        # (which only bounds the raw upload's total size).
        segment_iter = segment_runs(parse_file(log_path), max_run_events=config.MAX_RUN_EVENTS)
        results = []
        seen_any_completed = False
        # Keys that started but never got a matching CHALLENGE_MODE_END --
        # tracked separately from `results` (real analyzed/failed runs) so
        # the "nothing found" response below can tell "this log genuinely
        # has no M+ content" apart from "a key started but the log doesn't
        # show it finishing" -- a real, reported case (2026-09-01): a
        # 22-minute King's Rest key with a real CHALLENGE_MODE_START and no
        # END got the exact same generic message as an empty/non-M+ log,
        # which reads as "nothing was detected" when something very much
        # was -- just not a *completed* run.
        incomplete_runs: list[dict[str, Any]] = []
        while True:
            try:
                segment = next(segment_iter)
            except StopIteration:
                break
            except Exception:
                return 400, {"error": "could not read this as a WoWCombatLog.txt file"}

            if segment.truncated:
                # A single run that hit MAX_RUN_EVENTS -- see
                # segment_runs()/config.py's comments. Reported like any
                # other failed run rather than silently dropped, so the
                # uploader knows this specific key needs the desktop
                # app/CLI instead of just seeing it vanish from the list.
                results.append({
                    "ok": False,
                    "error": "this run was too long to analyze on the "
                             "website (a single very long continuous key) "
                             "-- the desktop app or CLI can analyze this "
                             "exact file locally with no size limit",
                    "zone": segment.zone_name,
                })
                segment.events = []
                continue

            if not segment.completed:
                # A CHALLENGE_MODE_START with no matching END. Confirmed
                # against a real report (2026-09-01) that this includes a
                # perfectly normal case, not just crashes/truncated logs:
                # a group voting to abandon the key. WoW's own vote-to-
                # abandon flow doesn't write a CHALLENGE_MODE_END at all --
                # only a timed or depleted verdict does (the real log in
                # that report ends with one player hearthing out and
                # another taking fall damage at open-world coordinates,
                # with zero CHALLENGE_MODE_END anywhere in the file). The
                # other real cause is still possible too: the key is still
                # in progress when this log was grabbed, or WoW never got
                # to write the END line (a crash, a /reload at exactly the
                # wrong moment, the log file cut off partway through).
                # There's no signal in the combat log that tells these
                # apart -- an abandon and a not-yet-finished key look
                # identical from here. Either way, there's genuinely
                # nothing to analyze -- no wall-clock end point, no
                # success/fail verdict -- but it's worth telling the
                # uploader a key WAS found, not staying silent about it.
                if segment.zone_name:
                    incomplete_runs.append({
                        "zone": segment.zone_name,
                        "level": segment.keystone_level,
                        "wall_duration_s": round(segment.wall_duration, 1),
                    })
                segment.events = []
                continue
            seen_any_completed = True

            try:
                report = analyze_run(segment, route=route, store=store)
            except Exception:
                results.append({
                    "ok": False,
                    "error": "failed to analyze this run",
                    "zone": segment.zone_name,
                })
                continue
            finally:
                # Drop this run's events explicitly rather than waiting
                # on the next loop iteration to reassign `segment` --
                # same discipline as desktop/api.py's list_runs().
                segment.events = []

            _, body = _ingest_report(conn, report, token_hash, remote_addr)
            body["zone"] = report["run"].get("zone")
            body["level"] = report["run"].get("keystone_level")
            body["timed"] = report["run"].get("timed")
            results.append(body)

            # Commit after every run, not once at the end of the whole
            # batch: a real, separate bug (found empirically alongside
            # the memory one, on the same large-file test) -- Python's
            # sqlite3 module opens an implicit write transaction on the
            # first INSERT and doesn't release it until commit(), so a
            # single commit-at-the-end left `conn`'s write lock (taken by
            # record_upload()'s INSERT OR REPLACE) held for the entire
            # batch. In WAL mode only one connection may hold the write
            # lock at a time, and the *next* run's `Store(...)` (its own,
            # separate connection -- see db.connect()'s docstring on why
            # connections aren't shared) needs that lock for its own
            # insert -- so every run after the first in any multi-run
            # batch failed with "database is locked". Committing here
            # also means a batch that fails partway through (a later
            # run's analysis raises) keeps whatever already succeeded,
            # rather than losing the whole batch to one bad run.
            conn.commit()

        if not seen_any_completed and not results:
            # `results` can be non-empty here even with seen_any_completed
            # still False -- e.g. a log with exactly one run, and that run
            # got truncated (too large) rather than genuinely completed.
            # That's a real result worth showing (see the segment.truncated
            # branch above), not the generic "nothing found" message.
            if incomplete_runs:
                found = ", ".join(
                    f"{r['zone']} (+{r['level']})" if r["level"] else r["zone"]
                    for r in incomplete_runs
                )
                return 200, {
                    "ok": True,
                    "runs": [],
                    "message": f"found {len(incomplete_runs)} key(s) that started but never "
                               f"finished in this log ({found}) -- there's nothing to analyze "
                               "without an end point. If the group voted to abandon the key, "
                               "that's normal and expected: WoW doesn't log an ending for an "
                               "abandoned key, only for one that reaches a timed or depleted "
                               "verdict, so there's genuinely nothing here to upload. If the "
                               "key actually finished normally, the log may have been grabbed "
                               "before WoW wrote the ending -- try again once the timer "
                               "verdict has actually shown up in your objective tracker.",
                }
            return 200, {
                "ok": True,
                "runs": [],
                "message": "no completed Mythic+ runs found in this log",
            }

        result: dict[str, Any] = {"ok": True, "runs": results}
        if route_warning:
            result["route_warning"] = route_warning
        return 200, result
    finally:
        conn.close()


_UPLOAD_CSS = """
.upload-wrap { max-width:640px; padding-top:48px; padding-bottom:24px; }
.upload-wrap h1 { font-size:26px; color:var(--accent); margin-bottom:6px; }
.upload-wrap p.lead { color:var(--dim); }
.dropzone { border:2px dashed var(--line); border-radius:var(--radius);
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
.upload-wrap button { background:var(--accent); color:var(--accent-ink); border:none;
  border-radius:var(--radius-sm); padding:11px 20px; font-size:14px; font-weight:600;
  cursor:pointer; margin-top:8px; }
.upload-wrap button:disabled { opacity:.5; cursor:default; }
ul.runs { list-style:none; padding:0; margin:16px 0; }
ul.runs li { background:var(--panel); border:1px solid var(--line);
  border-radius:8px; padding:12px 16px; margin-bottom:8px; }
.ok { color:var(--good); }
.bad { color:var(--bad); }
.warn { color:var(--warn); }
"""

_UPLOAD_FORM_BODY = f"""
<style>{_UPLOAD_CSS}</style>
<div class="wrap upload-wrap">
<h1>Upload a run</h1>
<p class="lead">Pick your <code>WoWCombatLog.txt</code> directly — every completed
Mythic+ key in it gets analyzed and posted automatically. No install, no app.
Not sure where to find that file? <a href="/guide">step-by-step guide</a>.</p>
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
</div>
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

    body_html = f"""
<style>{_UPLOAD_CSS}</style>
<div class="wrap upload-wrap">
<h1>Upload results</h1>
{message}
<p><a href="/upload">Upload another log</a> · <a href="/runs">Browse all runs</a></p>
</div>
"""
    return _page("Upload results — Postmortem", "upload", body_html)


@app.get("/upload")
async def upload_form() -> HTMLResponse:
    return HTMLResponse(_page("Upload a run — Postmortem", "upload", _UPLOAD_FORM_BODY))


# Chunk size for streaming an upload straight to disk (see upload_log
# below). Arbitrary but reasonable: big enough that chunk-loop overhead
# is negligible, small enough that peak memory for this step never
# exceeds ~1MB regardless of how large MAX_LOG_BYTES is configured --
# unlike a single logfile.read(MAX_LOG_BYTES + 1) call, which held the
# *entire* upload in memory at once and made raising the cap for a real
# multi-hour session's log a genuine OOM risk (this predates the 2gb VM
# bump -- see fly.toml -- and remains true regardless of VM size, since
# a big enough cap can always outgrow whatever's available).
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
            cap_mb = config.MAX_LOG_BYTES // (1024 * 1024)
            return HTMLResponse(
                _render_upload_result(413, {
                    "error": f"that file is over the {cap_mb}MB limit for a single "
                             "upload. A log with several separate keys in it, even "
                             "a full evening's worth, is fine well under this -- "
                             "each key is handled and sized independently, so this "
                             "limit is really just a sanity check on the whole "
                             "request. If a single upload is genuinely over "
                             f"{cap_mb}MB, restarting WoW starts a fresh log file, "
                             "or the desktop app/CLI can analyze this exact file "
                             "locally with no size limit at all.",
                }),
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
