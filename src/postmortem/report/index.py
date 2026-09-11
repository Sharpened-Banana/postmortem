"""Historical run index: one static webpage over all your saved reports.

``postmortem index reports/`` scans a directory for report JSON files
(written by ``analyze --format json`` or ``record --analyze``) and builds a
single self-contained index.html: every run as a leaderboard-style row
(rank, dungeon, key level with chest stars, time, affixes, class-colored
party, kick/route efficiency) that expands to its deaths and stats,
filterable by dungeon, with links to the per-run HTML reports and
per-dungeon bests.

The same page is rendered from the sqlite history store (see
``history/store.py``), the desktop app's History tab, and the public
site's ``/runs`` -- all three feed ``render_index()`` rows of the exact
shape ``collect_reports()`` produces. ``party_summary()`` and
``deaths_summary()`` below are shared with the store so both producers
derive the nested fields identically.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


def party_summary(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The run's players as ``[{name, class, role}]`` for the index row.

    Skips the shared pets/guardians bucket (guid ``_pets``, see
    analysis/stats.py PET_BUCKET). Does NOT cap at five: a report can
    legitimately list more (someone left and was replaced) and the page
    decides how to show that. ``class``/``role`` are None on logs without
    COMBATANT_INFO -- the page renders those uncolored/unbucketed rather
    than dropping the player.
    """
    out = []
    for p in report.get("players") or []:
        guid = str(p.get("guid") or "")
        if guid.startswith("_"):
            continue
        out.append({
            "name": p.get("name") or "",
            "class": p.get("class"),
            "role": p.get("role"),
        })
    return out


def deaths_summary(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Each death as ``{t, player, spell}`` -- relative seconds, victim, and
    the killing-blow spell name -- for the row's expanded detail."""
    out = []
    for d in report.get("deaths") or []:
        killing_blow = d.get("killing_blow") or {}
        out.append({
            "t": d.get("t"),
            "player": d.get("player") or "",
            "spell": killing_blow.get("spell") or "Unknown",
        })
    return out


def collect_reports(directory: str | Path) -> list[dict[str, Any]]:
    """Load every run-report JSON under ``directory`` into index rows."""
    rows: list[dict[str, Any]] = []
    root = Path(directory)
    for path in sorted(root.rglob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, ValueError):
            continue
        # A *.json here isn't necessarily one of our report files: the
        # recorder writes a <run>.chapters.json sidecar (a top-level JSON
        # *list*) into this same directory, and json.load succeeds on it.
        # Guard the type before .get() -- a plain report.get("run") on a
        # list raises AttributeError (not caught above), which used to
        # crash `index`/`serve` on any directory produced by
        # `record --analyze`.
        if not isinstance(report, dict):
            continue
        run = report.get("run")
        if not isinstance(run, dict) or "zone" not in run:
            continue  # not one of our reports
        html_sibling = path.with_suffix(".html")
        forces = report.get("forces") or {}
        comparison = report.get("comparison") or {}
        enemy_casts = report.get("enemy_casts") or {}
        death_cost = report.get("death_cost") or {}
        timer = report.get("timer") or {}
        rows.append({
            "file": path.name,
            "html": html_sibling.name if html_sibling.exists() else None,
            "zone": run.get("zone"),
            "level": run.get("keystone_level"),
            "start_ts": run.get("start_ts"),
            "date": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(run["start_ts"])
            ) if run.get("start_ts") else "?",
            "completed": bool(run.get("completed")),
            "timed": run.get("timed"),
            "duration_ms": run.get("duration_ms"),
            "wall_s": run.get("wall_duration_s"),
            "deaths": len(report.get("deaths") or []),
            "death_cost_s": death_cost.get("total_s"),
            "forces_pct": forces.get("pct"),
            "adherence_pct": comparison.get("adherence_pct"),
            "kick_efficiency_pct": enemy_casts.get("kick_efficiency_pct"),
            "affixes": run.get("affixes") or [],
            "party": party_summary(report),
            "threshold": timer.get("threshold"),
            "margin_ms": timer.get("margin_ms"),
            "deaths_detail": deaths_summary(report),
        })
    rows.sort(key=lambda r: r.get("start_ts") or 0, reverse=True)
    return rows


_INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mythic+ run history</title>
<style>
:root { --bg:#14161b; --panel:#1d2027; --panel2:#232733; --line:#313746; --text:#d8dbe2;
  --dim:#8a90a0; --accent:#d7a94c; --good:#58c47c; --bad:#e06060; --warn:#e0a13c; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:14px/1.5 "Segoe UI",system-ui,sans-serif; padding:24px; }
h1 { font-size:22px; margin:0 0 16px; color:var(--accent); }
h2 { font-size:15px; margin:28px 0 10px; text-transform:uppercase;
  letter-spacing:.08em; color:var(--dim); border-bottom:1px solid var(--line);
  padding-bottom:6px; }
table { border-collapse:collapse; width:100%; }
th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line);
  white-space:nowrap; }
th { color:var(--dim); font-size:12px; text-transform:uppercase;
  letter-spacing:.05em; cursor:pointer; user-select:none; }
td.num,th.num { text-align:right; font-variant-numeric:tabular-nums; }
.wrap { overflow-x:auto; background:var(--panel); border:1px solid var(--line);
  border-radius:8px; padding:6px 4px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px; margin-bottom:16px; }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:12px 14px; }
.stat .v { font-size:20px; font-weight:700; }
.stat .l { font-size:11px; color:var(--dim); text-transform:uppercase;
  letter-spacing:.06em; }
.timed { color:var(--good); font-weight:600; }
.over { color:var(--warn); } .dnf { color:var(--bad); }
a { color:#5c9ad0; text-decoration:none; } a:hover { text-decoration:underline; }
select { background:var(--panel); color:var(--text); border:1px solid var(--line);
  border-radius:6px; padding:6px 10px; margin-bottom:12px; }
.dim { color:var(--dim); }
.charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
  gap:12px; margin-bottom:16px; }
.chart { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:10px 12px; }
.chart-title { font-size:11px; color:var(--dim); text-transform:uppercase;
  letter-spacing:.06em; display:flex; justify-content:space-between; margin-bottom:6px; }
.chart-value { color:var(--text); font-weight:600; text-transform:none;
  letter-spacing:normal; }
.chart-svg { width:100%; height:56px; display:block; }

/* Leaderboard rows. One shared grid template for the header and every row
   so columns line up; .col-score marks the Kicks/Route pair, kept as the
   last two cells so dropping them later is two cells + one rule. */
.board { min-width:860px; }
/* DPS is minmax(0,1fr), not 1fr: a bare 1fr track's minimum is its
   content width, so five long names would push Kicks/Route off the right
   edge instead of truncating. */
.run-head, .run-row { display:grid; align-items:center;
  grid-template-columns:22px 44px 70px 86px 60px 120px 104px 104px minmax(0,1fr) 56px 56px;
  gap:0 8px; padding:0 8px; }
.run-head { color:var(--dim); font-size:12px; text-transform:uppercase;
  letter-spacing:.05em; user-select:none; border-bottom:1px solid var(--line);
  padding-top:6px; padding-bottom:6px; }
.run-head > div { cursor:pointer; white-space:nowrap; }
.run-head > div.static { cursor:default; }
.run-row { border-bottom:1px solid var(--line); min-height:40px;
  white-space:nowrap; cursor:pointer; }
.run-row:hover { background:rgba(255,255,255,.025); }
.run-row.open { background:rgba(215,169,76,.06); }
.run-row .caret { color:var(--dim); font-size:12px; width:18px;
  text-align:center; }
.run-row .rank { color:var(--accent); font-weight:700; text-align:center;
  font-variant-numeric:tabular-nums; }
.run-row .rank.none { color:var(--dim); font-weight:400; }
.run-row .dungeon { font-weight:600; letter-spacing:.02em; }
.run-row .level { font-variant-numeric:tabular-nums; }
.run-row .level .stars { color:var(--accent); font-size:11px; margin-left:3px;
  letter-spacing:-1px; }
.run-row .level.over { color:var(--warn); }
.run-row .level.dnf { color:var(--bad); }
.run-row .time { font-variant-numeric:tabular-nums; }
.run-row .time.over { color:var(--warn); }
.affix { display:inline-block; font-size:10.5px; padding:1px 6px; margin-right:4px;
  border:1px solid var(--line); border-radius:4px; color:var(--dim);
  background:var(--bg); letter-spacing:.02em; cursor:default; }
.run-row .party { overflow:hidden; text-overflow:ellipsis; min-width:0; }
.pname { margin-right:8px; font-weight:500; }
.pname.unk { color:var(--dim); font-weight:400; }
.run-row .score { text-align:right; font-variant-numeric:tabular-nums; }
.run-head .score, .run-head .num { text-align:right; }
.run-detail { background:var(--panel2); border-bottom:1px solid var(--line);
  padding:10px 14px 10px 46px; font-size:13px; }
.run-detail .stats { display:flex; flex-wrap:wrap; gap:6px 22px; }
.run-detail .stats b { color:var(--text); font-weight:600; }
.run-detail .stats span { color:var(--dim); }
.run-detail .deaths { margin-top:6px; color:var(--dim); }
.run-detail .deaths b { color:var(--text); font-weight:500; }
.run-detail .open { float:right; margin-left:20px; }
</style>
</head>
<body>
<h1>Mythic+ run history</h1>
<div id="app"></div>
<script id="runs-data" type="application/json">__RUNS_JSON__</script>
<script>
// ---------------------------------------------------------------------
// Everything below renders into innerHTML, and every value in R came out
// of an uploaded combat log -- anonymously, on the public site. Two stored
// cross-site scripting holes were found here on 2026-09-11, and a scan
// afterwards counted ~70 places where a report value is interpolated
// directly. Escaping each of them by hand is a standing invitation to miss
// one (the original bug was invisible at the source level: every value DID
// go through esc(); esc() was simply incomplete).
//
// So the angle brackets are neutralised once, here, for every string in
// the report, before a single template runs. Nothing downstream can then
// open a tag, whatever context it lands in. Escaping (rather than
// stripping) keeps the characters readable, and it is deliberately ONLY
// < and > -- quotes are left for esc() at the attribute sites, because
// pre-escaping them here would double-escape the many legitimate
// apostrophes in WoW names.
function deTag(value) {
  if (typeof value === "string") return value.replace(/</g, "&lt;").replace(/>/g, "&gt;");
  if (Array.isArray(value)) return value.map(deTag);
  if (value && typeof value === "object") {
    const out = {};
    for (const k of Object.keys(value)) out[k] = deTag(value[k]);
    return out;
  }
  return value;
}

const RUNS = deTag(JSON.parse(document.getElementById("runs-data").textContent));
// Escapes the single quote and backtick as well as the obvious four.
// A zone name is raw text from the log's CHALLENGE_MODE_START line, i.e.
// fully attacker-controlled on the public site, and it used to reach an
// inline onclick handler -- where a bare ' closed the JS string literal
// and everything after it ran (2026-09-11). The inline handlers are gone
// (see the delegated listeners at the bottom of this script), but the
// escape stays complete so the next author cannot reintroduce it.
const esc = s => String(s ?? "").replace(/[&<>"'`]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;","`":"&#96;"}[c]));
// Report fields are typed only by whatever was uploaded: SQLite's loose
// typing lets a string through a column declared INTEGER, so a "number"
// here can be arbitrary text. Anything rendered as a number goes through
// this rather than being interpolated raw.
const num = (v, fallback = "—") => Number.isFinite(Number(v)) && v !== null && v !== ""
  ? String(Number(v)) : fallback;
const mmss = s => { if (s == null) return "?"; s = Math.round(s);
  const m = Math.floor(s/60);
  return `${m}:${String(s%60).padStart(2,"0")}`; };
let dungeon = "";
let sortKey = "start_ts", sortDir = -1;
// Which row's detail block is open, keyed by the run's (start_ts|zone)
// identity so it survives re-sorting. One at a time.
let openKey = null;

// Affix ids -> names, as they appear in a report's run.affixes (the
// numeric ids from the CHALLENGE_MODE_START log line). Unknown ids still
// render, as "#id", so a new season never blanks the column.
const AFFIXES = {
  9: "Tyrannical", 10: "Fortified", 147: "Xal'atath's Guile", 148: "Ascendant",
  152: "Challenger's Peril", 158: "Voidbound", 159: "Oblivion", 160: "Devour",
  162: "Pulsar",
};
const AFFIX_SHORT = {
  9: "Tyr", 10: "Fort", 147: "Guile", 148: "Asc", 152: "Peril", 158: "Void",
  159: "Obliv", 160: "Devour", 162: "Pulsar",
};
// Standard WoW class colors, keyed by class name normalized to lowercase
// with spaces removed so "Death Knight" / "DeathKnight" / "DEATHKNIGHT"
// all resolve.
const CLASS_COLORS = {
  deathknight: "#C41E3A", demonhunter: "#A330C9", druid: "#FF7C0A",
  evoker: "#33937F", hunter: "#AAD372", mage: "#3FC7EB", monk: "#00FF98",
  paladin: "#F48CBA", priest: "#FFFFFF", rogue: "#FFF468", shaman: "#0070DD",
  warlock: "#8788EE", warrior: "#C69B3A",
};
const classColor = c => c ? CLASS_COLORS[String(c).toLowerCase().replace(/[^a-z]/g, "")] : null;
const roleOf = r => { r = String(r ?? "").toLowerCase();
  if (r.startsWith("tank")) return "tank";
  if (r.startsWith("heal")) return "healer";
  if (r === "dps" || r.startsWith("damage") || r === "melee" || r === "ranged") return "dps";
  return "unknown"; };
const shortName = n => (String(n ?? "").match(/^[^-]+/) || [n])[0];
const runKey = r => `${r.start_ts}|${r.zone}`;

// 2-3 letter dungeon abbreviation from the zone name's initials, skipping
// filler words; hyphenated names split too (Ara-Kara -> A, K). Full name
// is on hover.
function abbrev(zone) {
  const stop = new Set(["of", "the", "and", "de", "la", "le"]);
  const words = String(zone ?? "").replace(/[^A-Za-z0-9\\s-]/g, " ")
    .split(/[\\s-]+/).filter(w => w && !stop.has(w.toLowerCase()));
  const s = words.map(w => w[0].toUpperCase()).join("");
  return s.slice(0, 3) || "?";
}

// Chest upgrade count for the stars: the report's timer.threshold (0-3)
// when a par time was resolved; older reports without one fall back to
// the plain timed flag. null = incomplete / unknown.
function stars(r) {
  if (!r.completed) return null;
  if (r.threshold != null) return r.threshold;
  if (r.timed === true) return 1;
  if (r.timed === false) return 0;
  return null;
}

function result(r) {
  if (!r.completed) return '<span class="dnf">incomplete</span>';
  return r.timed ? '<span class="timed">timed</span>'
                 : '<span class="over">over timer</span>';
}

// Rank = position among ALL your completed runs of the same dungeon at the
// same key level, fastest first. Computed once over the full set so
// filtering the page never renumbers anyone.
function assignRanks() {
  const groups = {};
  for (const r of RUNS) {
    r._rank = null;
    if (!r.completed || r.level == null || !r.duration_ms) continue;
    const k = `${r.zone}|${r.level}`;
    (groups[k] = groups[k] || []).push(r);
  }
  for (const g of Object.values(groups)) {
    g.sort((a, b) => a.duration_ms - b.duration_ms);
    g.forEach((r, i) => { r._rank = i + 1; });
  }
}

function partyCell(list) {
  if (!list || !list.length) return '<span class="dim">—</span>';
  return list.map(p => {
    const col = classColor(p.class);
    const style = col ? ` style="color:${col}"` : "";
    const cls = col ? "pname" : "pname unk";
    return `<span class="${cls}"${style} title="${esc(p.name)}${p.class ? " · " + esc(p.class) : ""}">${esc(shortName(p.name))}</span>`;
  }).join("");
}

function affixCell(ids) {
  if (!ids || !ids.length) return '<span class="dim">—</span>';
  return ids.map(id => `<span class="affix" title="${esc(AFFIXES[id] || ("Affix #" + id))}">${esc(AFFIX_SHORT[id] || ("#" + id))}</span>`).join("");
}

// The score columns. These read a percentage out of the report, so a
// non-number here is text an uploader chose -- never concatenate it.
function pct(v) { return v != null ? num(v, "—") + "%" : "—"; }

function detailBlock(r) {
  const parts = [];
  parts.push(`<b>${num(r.deaths, "0")}</b> <span>deaths${r.death_cost_s ? ` (−${mmss(r.death_cost_s)})` : ""}</span>`);
  if (r.forces_pct != null) parts.push(`<b>${num(r.forces_pct)}%</b> <span>forces</span>`);
  if (r.margin_ms != null) {
    const s = Math.abs(r.margin_ms) / 1000;
    parts.push(r.margin_ms >= 0
      ? `<b>+${mmss(s)}</b> <span>under timer</span>`
      : `<b class="over">${mmss(s)}</b> <span>over timer</span>`);
  } else {
    parts.push(result(r));
  }
  parts.push(`<b>${pct(r.kick_efficiency_pct)}</b> <span>kicks</span>`);
  parts.push(`<b>${pct(r.adherence_pct)}</b> <span>route</span>`);
  parts.push(`<span>${esc(r.date)}</span>`);

  const deaths = (r.deaths_detail || []).map(d =>
    `<b>${esc(shortName(d.player))}</b> → ${esc(d.spell)}${d.t != null ? ` <span>at ${mmss(d.t)}</span>` : ""}`
  ).join(" · ");

  const open = r.html ? `<a class="open" href="${esc(r.html)}">open full report →</a>`
                      : `<span class="open dim">${esc(r.file)}</span>`;
  return `<div class="run-detail">${open}
    <div class="stats">${parts.join("")}</div>
    ${deaths ? `<div class="deaths">${deaths}</div>` : ""}
  </div>`;
}

function runRow(r) {
  const key = runKey(r);
  const isOpen = openKey === key;
  const st = stars(r);
  const levelCls = !r.completed ? "level dnf" : (st === 0 ? "level over" : "level");
  const starStr = st ? "★".repeat(st) : "";
  const timeStr = r.duration_ms ? mmss(r.duration_ms/1000) : mmss(r.wall_s);
  const timeCls = (r.completed && st === 0) ? "time over" : "time";
  const party = r.party || [];
  const by = { tank: [], healer: [], dps: [], unknown: [] };
  for (const p of party) by[roleOf(p.role)].push(p);
  // Players whose role isn't known (no COMBATANT_INFO in that log) are
  // listed after the DPS column rather than dropped.
  const dpsList = by.dps.concat(by.unknown);
  // A log without COMBATANT_INFO has no roles at all -- then three
  // dash/dash/everyone cells read as broken. Span the whole party across
  // the three columns instead; the split returns as soon as roles exist.
  const noRoles = party.length > 0 && !by.tank.length && !by.healer.length && !by.dps.length;
  const partyCells = noRoles
    ? `<div class="party" style="grid-column:span 3">${partyCell(party)}</div>`
    : `<div class="party">${partyCell(by.tank)}</div>
    <div class="party">${partyCell(by.healer)}</div>
    <div class="party">${partyCell(dpsList)}</div>`;
  // The whole row toggles its detail (the caret is just the indicator) --
  // a 22px glyph is too small a click target on its own.
  return `<div class="run-row${isOpen ? " open" : ""}" data-key="${esc(key)}">
    <div class="caret">${isOpen ? "▾" : "▸"}</div>
    <div class="rank${r._rank ? "" : " none"}">${num(r._rank, "—")}</div>
    <div class="dungeon" title="${esc(r.zone)}">${esc(abbrev(r.zone))}</div>
    <div class="${levelCls}">+${num(r.level, "?")}<span class="stars">${starStr}</span></div>
    <div class="${timeCls}">${timeStr}</div>
    <div class="affixes">${affixCell(r.affixes)}</div>
    ${partyCells}
    <div class="score col-score">${pct(r.kick_efficiency_pct)}</div>
    <div class="score col-score">${pct(r.adherence_pct)}</div>
  </div>${isOpen ? detailBlock(r) : ""}`;
}

// Small inline-SVG sparkline for one trend series. `pts` is an array of
// {x, y} in chronological order; a null/undefined y is a missing value
// (e.g. a run analyzed without --route has no adherence_pct) and opens a
// gap rather than being plotted as zero. The viewBox/scale is derived from
// the data's own bounds, same as report/html.py's mapSection() -- no
// hardcoded axis range. A run of 2+ consecutive valid points draws as a
// polyline; an isolated valid point (boxed in by gaps, or the only data
// point at all) still draws as a dot rather than silently vanishing.
function sparklineChart(pts, opts) {
  const w = 300, h = 60, pad = 4;
  const valid = pts.filter(p => p.y != null);
  if (!valid.length) {
    return `<div class="chart"><div class="chart-title">${esc(opts.title)}</div><div class="dim">not enough data</div></div>`;
  }
  const xs = pts.map(p => p.x);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const spanX = (maxX - minX) || 1;
  const minY = Math.min(...valid.map(p => p.y));
  const maxY = Math.max(...valid.map(p => p.y));
  const spanY = (maxY - minY) || 1;
  const sx = x => pad + (x - minX) / spanX * (w - 2 * pad);
  const sy = y => h - pad - (y - minY) / spanY * (h - 2 * pad);

  let segs = [], cur = [];
  const flush = () => { if (cur.length) segs.push(cur); cur = []; };
  for (const p of pts) {
    if (p.y == null) { flush(); continue; }
    cur.push({ x: sx(p.x), y: sy(p.y) });
  }
  flush();

  const marks = segs.map(seg => seg.length > 1
    ? `<polyline points="${seg.map(pt => `${pt.x.toFixed(1)},${pt.y.toFixed(1)}`).join(" ")}"
        fill="none" stroke="${opts.color}" stroke-width="1.5"
        stroke-linejoin="round" stroke-linecap="round"/>`
    : `<circle cx="${seg[0].x.toFixed(1)}" cy="${seg[0].y.toFixed(1)}" r="2" fill="${opts.color}"/>`
  ).join("");

  const last = valid[valid.length - 1].y;
  return `<div class="chart">
    <div class="chart-title">${esc(opts.title)}<span class="chart-value">${esc(opts.fmt(last))}</span></div>
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" class="chart-svg">${marks}</svg>
  </div>`;
}

// Builds the four trend sparklines from a row set that is already filtered
// (by the dungeon <select>) and sorted chronologically ascending -- the
// caller (render()) owns that ordering; this function never re-sorts, so
// it can't drift from the board's own (independent, user-clickable) sort.
function chartsSection(sorted) {
  let timedSoFar = 0;
  const timedPts = sorted.map((r, i) => {
    if (r.timed) timedSoFar++;
    return { x: i, y: Math.round(1000 * timedSoFar / (i + 1)) / 10 };
  });
  const deathPts = sorted.map((r, i) => ({ x: i, y: r.deaths || 0 }));
  const adherPts = sorted.map((r, i) => ({ x: i, y: r.adherence_pct }));
  const kickPts = sorted.map((r, i) => ({ x: i, y: r.kick_efficiency_pct }));
  return `<h2>Trends</h2>
  <div class="charts">
    ${sparklineChart(timedPts, { title: "Timed rate (cumulative)", color: "var(--good)", fmt: v => v + "%" })}
    ${sparklineChart(deathPts, { title: "Deaths per run", color: "var(--bad)", fmt: v => String(v) })}
    ${sparklineChart(adherPts, { title: "Route adherence", color: "var(--accent)", fmt: v => v + "%" })}
    ${sparklineChart(kickPts, { title: "Kick efficiency", color: "var(--warn)", fmt: v => v + "%" })}
  </div>`;
}

function render() {
  assignRanks();
  const filtered = RUNS.filter(r => !dungeon || r.zone === dungeon);
  const rows = filtered.slice().sort((a, b) => {
    const va = a[sortKey] ?? -Infinity, vb = b[sortKey] ?? -Infinity;
    return (va < vb ? -1 : va > vb ? 1 : 0) * sortDir;
  });
  // Charts always read the filtered-but-chronological set, independent of
  // whatever column the board is currently sorted by.
  const chartRows = filtered.slice().sort((a, b) => (a.start_ts ?? 0) - (b.start_ts ?? 0));
  const dungeons = [...new Set(RUNS.map(r => r.zone).filter(Boolean))].sort();
  const timed = rows.filter(r => r.timed).length;
  const completed = rows.filter(r => r.completed).length;
  const deaths = rows.reduce((s, r) => s + (r.deaths || 0), 0);

  const best = {};
  for (const r of RUNS) {
    if (!r.timed || r.level == null) continue;
    if (!best[r.zone] || r.level > best[r.zone].level ||
        (r.level === best[r.zone].level && (r.duration_ms||1e12) < (best[r.zone].duration_ms||1e12)))
      best[r.zone] = r;
  }

  const stat = (v, l) => `<div class="stat"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`;
  const hd = (label, key, cls) => key
    ? `<div class="${cls || ""}" data-sort="${esc(key)}">${label}${sortKey === key ? (sortDir > 0 ? " ▲" : " ▼") : ""}</div>`
    : `<div class="static ${cls || ""}">${label}</div>`;

  document.getElementById("app").innerHTML = `
  <div class="grid">
    ${stat(rows.length, "runs")}
    ${stat(timed, "timed")}
    ${stat(completed ? Math.round(100 * timed / completed) + "%" : "—", "timed rate")}
    ${stat(deaths, "total deaths")}
  </div>
  <select id="dungeon-filter">
    <option value="">All dungeons</option>
    ${dungeons.map(d => `<option ${d === dungeon ? "selected" : ""} value="${esc(d)}">${esc(d)}</option>`).join("")}
  </select>
  ${chartsSection(chartRows)}
  <div class="wrap"><div class="board">
    <div class="run-head">
      ${hd("", null)}${hd("Rank", "_rank")}${hd("Dungeon", "zone")}${hd("Level", "level")}
      ${hd("Time", "duration_ms")}${hd("Affixes", null)}
      ${hd("🛡 Tank", null)}${hd("✚ Healer", null)}${hd("🗡 DPS", null)}
      ${hd("Kicks", "kick_efficiency_pct", "score col-score")}${hd("Route", "adherence_pct", "score col-score")}
    </div>
    ${rows.map(runRow).join("") || '<div class="run-row"><div></div><div class="dim" style="grid-column:2/-1">no runs yet</div></div>'}
  </div></div>
  <h2>Best timed key per dungeon</h2>
  <div class="wrap"><table>
    <tr><th>Dungeon</th><th class="num">Key</th><th class="num">Timer</th><th>Date</th><th></th></tr>
    ${Object.values(best).sort((a, b) => (b.level||0) - (a.level||0)).map(r => `<tr>
      <td>${esc(r.zone)}</td><td class="num">+${num(r.level, "?")}</td>
      <td class="num">${r.duration_ms ? mmss(r.duration_ms/1000) : "?"}</td>
      <td>${esc(r.date)}</td>
      <td>${r.html ? `<a href="${esc(r.html)}">open</a>` : ""}</td></tr>`).join("")
      || '<tr><td colspan="5" class="dim">no timed runs yet</td></tr>'}
  </table></div>`;
}

function toggle(key) {
  openKey = openKey === key ? null : key;
  render();
}

function sortBy(key) {
  if (sortKey === key) sortDir = -sortDir; else { sortKey = key; sortDir = -1; }
  render();
}

// One delegated listener on the container, installed once. Report data
// reaches these handlers as a data-* attribute value read back through
// the DOM, never as source text spliced into an event-handler attribute
// -- so no amount of quoting in a zone name can become code.
document.getElementById("app").addEventListener("click", ev => {
  const sorter = ev.target.closest("[data-sort]");
  if (sorter) { sortBy(sorter.getAttribute("data-sort")); return; }
  const row = ev.target.closest(".run-row[data-key]");
  if (row) toggle(row.getAttribute("data-key"));
});
document.getElementById("app").addEventListener("change", ev => {
  if (ev.target.id === "dungeon-filter") { dungeon = ev.target.value; render(); }
});
render();
</script>
</body>
</html>
"""


def render_index(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows).replace("</", "<\\/")
    return _INDEX_TEMPLATE.replace("__RUNS_JSON__", payload)


def build_index(directory: str | Path, out_path: Optional[str | Path] = None) -> Path:
    rows = collect_reports(directory)
    out = Path(out_path) if out_path else Path(directory) / "index.html"
    out.write_text(render_index(rows), encoding="utf-8")
    return out
