"""Historical run index: one static webpage over all your saved reports.

``postmortem index reports/`` scans a directory for report JSON files
(written by ``analyze --format json`` or ``record --analyze``) and builds a
single self-contained index.html: every run with result, time, deaths,
route adherence and kick efficiency, filterable by dungeon, with links to
the per-run HTML reports and per-dungeon bests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


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
        run = report.get("run")
        if not isinstance(run, dict) or "zone" not in run:
            continue  # not one of our reports
        html_sibling = path.with_suffix(".html")
        forces = report.get("forces") or {}
        comparison = report.get("comparison") or {}
        enemy_casts = report.get("enemy_casts") or {}
        death_cost = report.get("death_cost") or {}
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
:root { --bg:#14161b; --panel:#1d2027; --line:#313746; --text:#d8dbe2;
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
</style>
</head>
<body>
<h1>Mythic+ run history</h1>
<div id="app"></div>
<script id="runs-data" type="application/json">__RUNS_JSON__</script>
<script>
const RUNS = JSON.parse(document.getElementById("runs-data").textContent);
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const mmss = s => { if (s == null) return "?"; s = Math.round(s);
  const m = Math.floor(s/60);
  return `${m}:${String(s%60).padStart(2,"0")}`; };
let dungeon = "";
let sortKey = "start_ts", sortDir = -1;

function result(r) {
  if (!r.completed) return '<span class="dnf">incomplete</span>';
  return r.timed ? '<span class="timed">timed</span>'
                 : '<span class="over">over timer</span>';
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
// it can't drift from the table's own (independent, user-clickable) sort.
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
  const filtered = RUNS.filter(r => !dungeon || r.zone === dungeon);
  const rows = filtered.slice().sort((a, b) => {
    const va = a[sortKey] ?? -Infinity, vb = b[sortKey] ?? -Infinity;
    return (va < vb ? -1 : va > vb ? 1 : 0) * sortDir;
  });
  // Charts always read the filtered-but-chronological set, independent of
  // whatever column the table is currently sorted by.
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

  const stat = (v, l) => `<div class="stat"><div class="v">${v}</div><div class="l">${esc(l)}</div></div>`;
  const th = (label, key, numeric) =>
    `<th class="${numeric ? "num" : ""}" onclick="sortBy('${key}')">${label}${sortKey === key ? (sortDir > 0 ? " ▲" : " ▼") : ""}</th>`;

  document.getElementById("app").innerHTML = `
  <div class="grid">
    ${stat(rows.length, "runs")}
    ${stat(timed, "timed")}
    ${stat(completed ? Math.round(100 * timed / completed) + "%" : "—", "timed rate")}
    ${stat(deaths, "total deaths")}
  </div>
  <select onchange="dungeon=this.value;render()">
    <option value="">All dungeons</option>
    ${dungeons.map(d => `<option ${d === dungeon ? "selected" : ""} value="${esc(d)}">${esc(d)}</option>`).join("")}
  </select>
  ${chartsSection(chartRows)}
  <div class="wrap"><table>
    <tr>${th("Date", "start_ts")}${th("Dungeon", "zone")}${th("Key", "level", 1)}
    <th>Result</th>${th("Timer", "duration_ms", 1)}${th("Deaths", "deaths", 1)}
    ${th("Forces", "forces_pct", 1)}${th("Route", "adherence_pct", 1)}
    ${th("Kicks", "kick_efficiency_pct", 1)}<th>Report</th></tr>
    ${rows.map(r => `<tr>
      <td>${esc(r.date)}</td><td>${esc(r.zone)}</td>
      <td class="num">+${r.level ?? "?"}</td><td>${result(r)}</td>
      <td class="num">${r.duration_ms ? mmss(r.duration_ms/1000) : mmss(r.wall_s)}</td>
      <td class="num${r.deaths ? " dnf" : ""}">${r.deaths}</td>
      <td class="num">${r.forces_pct != null ? r.forces_pct + "%" : ""}</td>
      <td class="num">${r.adherence_pct != null ? r.adherence_pct + "%" : ""}</td>
      <td class="num">${r.kick_efficiency_pct != null ? r.kick_efficiency_pct + "%" : ""}</td>
      <td>${r.html ? `<a href="${esc(r.html)}">open</a>` : `<span class="dim">${esc(r.file)}</span>`}</td>
    </tr>`).join("")}
  </table></div>
  <h2>Best timed key per dungeon</h2>
  <div class="wrap"><table>
    <tr><th>Dungeon</th><th class="num">Key</th><th class="num">Timer</th><th>Date</th><th></th></tr>
    ${Object.values(best).sort((a, b) => (b.level||0) - (a.level||0)).map(r => `<tr>
      <td>${esc(r.zone)}</td><td class="num">+${r.level}</td>
      <td class="num">${r.duration_ms ? mmss(r.duration_ms/1000) : "?"}</td>
      <td>${esc(r.date)}</td>
      <td>${r.html ? `<a href="${esc(r.html)}">open</a>` : ""}</td></tr>`).join("")
      || '<tr><td colspan="5" class="dim">no timed runs yet</td></tr>'}
  </table></div>`;
}

function sortBy(key) {
  if (sortKey === key) sortDir = -sortDir; else { sortKey = key; sortDir = -1; }
  render();
}
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
