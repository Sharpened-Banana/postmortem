"""Self-contained HTML post-mortem report.

The full report JSON is embedded in the page; a small amount of vanilla JS
renders the tables and the pull timeline. No external resources.
"""

from __future__ import annotations

import html
import json
from typing import Any

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #14161b; --panel: #1d2027; --panel2: #232733; --line: #313746;
  --text: #d8dbe2; --dim: #8a90a0; --accent: #d7a94c; --good: #58c47c;
  --bad: #e06060; --warn: #e0a13c; --blue: #5c9ad0;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 "Segoe UI", system-ui, sans-serif; padding: 24px; }
h1 { font-size: 22px; margin: 0 0 4px; color: var(--accent); }
h2 { font-size: 15px; margin: 28px 0 10px; text-transform: uppercase;
  letter-spacing: .08em; color: var(--dim); border-bottom: 1px solid var(--line);
  padding-bottom: 6px; }
.sub { color: var(--dim); margin-bottom: 18px; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 12px;
  font-weight: 600; font-size: 12px; margin-right: 8px; }
.badge.timed { background: #1d3a28; color: var(--good); }
.badge.over { background: #3a2d1d; color: var(--warn); }
.badge.abandoned { background: #3a1d1d; color: var(--bad); }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 5px 10px; border-bottom: 1px solid var(--line);
  white-space: nowrap; }
th { color: var(--dim); font-weight: 600; font-size: 12px;
  text-transform: uppercase; letter-spacing: .05em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.wrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 4px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px; margin-bottom: 8px; }
.stat { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 14px; }
.stat .v { font-size: 20px; font-weight: 700; color: var(--text); }
.stat .l { font-size: 11px; color: var(--dim); text-transform: uppercase;
  letter-spacing: .06em; }
.timeline { position: relative; background: var(--panel);
  border: 1px solid var(--line); border-radius: 8px; padding: 14px 10px 6px; }
.tl-row { position: relative; height: 22px; }
.tl-pull { position: absolute; top: 3px; height: 16px; border-radius: 3px;
  background: var(--blue); opacity: .85; min-width: 2px; }
.tl-pull.boss { background: var(--accent); }
.tl-pull.dev { outline: 2px solid var(--bad); }
.tl-death { position: absolute; top: 0; width: 2px; height: 22px;
  background: var(--bad); }
.tl-lust { position: absolute; top: 0; width: 2px; height: 22px;
  background: var(--good); }
.tl-axis { position: relative; height: 16px; color: var(--dim); font-size: 11px; }
.tl-axis span { position: absolute; transform: translateX(-50%); }
.dev-early { color: var(--warn); } .dev-late { color: var(--blue); }
.dev-off { color: var(--bad); } .ok { color: var(--good); }
.dim { color: var(--dim); }
details { margin: 4px 0; }
summary { cursor: pointer; }
.legend { font-size: 12px; color: var(--dim); margin-top: 6px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin: 0 4px 0 12px; vertical-align: -1px; }
.map-wrap { padding: 0; }
.map-wrap svg { display: block; width: 100%; height: auto; max-height: 560px;
  background: var(--panel2); }
</style>
</head>
<body>
<div id="app">Loading…</div>
<script id="report-data" type="application/json">__REPORT_JSON__</script>
<script>
const R = JSON.parse(document.getElementById("report-data").textContent);
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = n => n == null ? "?" :
  Math.abs(n) >= 1e9 ? (n/1e9).toFixed(2)+"b" :
  Math.abs(n) >= 1e6 ? (n/1e6).toFixed(2)+"m" :
  Math.abs(n) >= 1e3 ? (n/1e3).toFixed(1)+"k" : Math.round(n).toString();
const mmss = s => { if (s == null) return "?"; s = Math.round(s);
  const m = Math.floor(s/60), sec = s%60;
  return m >= 60 ? `${Math.floor(m/60)}:${String(m%60).padStart(2,"0")}:${String(sec).padStart(2,"0")}`
                 : `${m}:${String(sec).padStart(2,"0")}`; };
const npcs = list => (list||[]).map(e =>
  `${e.n}x ${esc(e.name || ("npc:"+e.npc_id))}`).join(", ");

function render() {
  const run = R.run, forces = R.forces || {}, dt = R.downtime || {};
  const dur = run.wall_duration_s;
  let badge = '<span class="badge abandoned">INCOMPLETE / ABANDONED</span>';
  if (run.completed) badge = run.timed
    ? '<span class="badge timed">TIMED</span>'
    : '<span class="badge over">OVER TIMER</span>';

  let html = `<h1>${esc(run.zone || R.dungeon.name)} +${run.keystone_level ?? "?"}</h1>
  <div class="sub">${badge}
    ${run.duration_ms ? "In-game timer " + mmss(run.duration_ms/1000) + " · " : ""}
    wall clock ${mmss(dur)}
    ${run.affixes && run.affixes.length ? " · affixes " + run.affixes.join(", ") : ""}</div>`;

  html += timerInfo();

  html += `<div class="grid">
    ${forces.required ? stat(num(forces.killed), `forces killed / ${num(forces.required)} (${forces.pct}%)`) : ""}
    ${stat((R.deaths||[]).length, "player deaths")}
    ${stat(mmss(dt.combat_s), "combat time")}
    ${stat(mmss(dt.total_s), "downtime")}
    ${stat((R.pulls||[]).length, "actual pulls")}
    ${R.route ? stat(R.route.pull_count, "planned pulls") : ""}
    ${R.comparison && R.comparison.adherence_pct != null ? stat(R.comparison.adherence_pct + "%", "route adherence") : ""}
    ${R.kick_value && R.kick_value.total_estimated_prevented_damage ? stat("~" + num(R.kick_value.total_estimated_prevented_damage), "dmg prevented by kicks (est.)") : ""}
    ${R.enemy_casts && R.enemy_casts.kick_efficiency_pct != null ? stat(R.enemy_casts.kick_efficiency_pct + "%", "kick efficiency") : ""}
    ${R.death_cost && R.death_cost.deaths ? stat("-" + mmss(R.death_cost.total_s), "timer lost to deaths") : ""}
  </div>`;

  html += timeline();
  html += playersTable();
  html += avoidableDamage();
  if (R.comparison && !R.comparison.error) html += comparison();
  else if (R.route) html += `<h2>Route</h2><div class="dim">${esc((R.comparison||{}).error || "")}</div>` + routeOnly();
  html += mapSection();
  html += pullsTable();
  html += enemyCasts();
  html += encounters();
  html += deaths();
  html += closeCalls();
  html += utility();
  html += downtime();
  document.getElementById("app").innerHTML = html;
}

const stat = (v, l) => `<div class="stat"><div class="v">${v}</div><div class="l">${esc(l)}</div></div>`;

function timerInfo() {
  const t = R.timer;
  if (!t) return "";
  const parLabel = `par ${mmss(t.par_ms/1000)} (+2 at ${mmss(t.threshold_2_ms/1000)}`
    + ` · +3 at ${mmss(t.threshold_3_ms/1000)})`;
  if (t.margin_ms == null) return `<div class="sub">${esc(parLabel)}</div>`;
  const cls = t.margin_ms >= 0 ? "ok" : "dev-off";
  const verb = t.margin_ms >= 0 ? "beat timer by" : "over timer by";
  const thr = t.threshold ? ` (+${t.threshold})` : "";
  return `<div class="sub"><span class="${cls}">${verb} ${mmss(Math.abs(t.margin_ms)/1000)}${thr}</span>`
    + ` · ${esc(parLabel)}</div>`;
}

function timeline() {
  const pulls = R.pulls || [];
  if (!pulls.length) return "";
  const span = Math.max(...pulls.map(p => p.t_end), 1);
  const x = t => (100 * t / span).toFixed(2) + "%";
  const devByPull = {};
  (R.comparison && R.comparison.pulls || []).forEach(m => {
    devByPull[m.actual_pull] = m.deviations > 0; });
  let bars = pulls.map(p => {
    const w = Math.max(0.15, 100 * (p.t_end - p.t_start) / span).toFixed(2) + "%";
    const cls = "tl-pull" + (p.boss ? " boss" : "") + (devByPull[p.pull] ? " dev" : "");
    const tip = `Pull ${p.pull}${p.boss ? " — " + p.boss : ""}\\n` +
      `${mmss(p.t_start)}–${mmss(p.t_end)} (${p.duration_s}s)\\n` +
      (p.npcs||[]).map(n => `${n.n}x ${n.name}`).join("\\n");
    return `<div class="${cls}" style="left:${x(p.t_start)};width:${w}" title="${esc(tip)}"></div>`;
  }).join("");
  const deaths = (R.deaths||[]).map(d =>
    `<div class="tl-death" style="left:${x(d.t)}" title="${esc(mmss(d.t) + " " + d.player + " died")}"></div>`).join("");
  const lust = (R.lust||[]).map(l =>
    `<div class="tl-lust" style="left:${x(l.t)}" title="${esc(mmss(l.t) + " " + l.spell)}"></div>`).join("");
  let axis = "";
  const step = span > 2400 ? 600 : span > 1200 ? 300 : 120;
  for (let t = 0; t <= span; t += step)
    axis += `<span style="left:${x(t)}">${mmss(t)}</span>`;
  return `<h2>Pull timeline</h2><div class="timeline">
    <div class="tl-row">${bars}${deaths}${lust}</div>
    <div class="tl-axis">${axis}</div>
    <div class="legend">hover a bar for pack details
      <i style="background:var(--blue)"></i>trash pull
      <i style="background:var(--accent)"></i>boss
      <i style="outline:2px solid var(--bad)"></i>route deviation
      <i style="background:var(--bad)"></i>death
      <i style="background:var(--good)"></i>bloodlust</div>
  </div>`;
}

function playersTable() {
  const players = (R.players||[]).filter(p => p.guid !== "_pets" || p.damage_done);
  players.sort((a, b) => b.damage_done - a.damage_done);
  const rows = players.map(p => `<tr>
    <td>${esc(p.name || p.guid)}</td>
    <td class="dim">${esc([p.spec, p.class].filter(Boolean).join(" ") || "?")}${p.raiderio && p.raiderio.score ? ` <span title="Raider.io M+ score">· ${Math.round(p.raiderio.score)} io</span>` : ""}</td>
    <td class="num">${num(p.dps)}</td><td class="num">${num(p.hps)}</td>
    <td class="num">${num(p.damage_done)}</td>
    <td class="num">${num(p.healing_done)}</td>
    <td class="num">${num(p.absorbs_granted)}</td>
    <td class="num">${num(p.damage_taken)}</td>
    <td class="num">${p.interrupts}</td>
    <td class="num" title="estimated damage + healing prevented by this player's interrupts">${(p.kick_prevented_damage || p.kick_prevented_healing) ? "~" + num((p.kick_prevented_damage||0) + (p.kick_prevented_healing||0)) : ""}</td>
    <td class="num">${(p.dispels||0) + (p.purges||0)}</td>
    <td class="num">${p.killing_blows ?? ""}</td>
    <td class="num">${p.cpm ?? ""}</td>
    <td class="num${p.deaths ? ' dev-off' : ''}">${p.deaths}</td></tr>
    <tr><td colspan="14" style="border-bottom:1px solid var(--line)">
      <details><summary class="dim">top abilities & buffs</summary>
      <div class="dim">Damage: ${(p.top_damage_spells||[]).slice(0,8).map(s => `${esc(s.name)} ${num(s.total)}`).join(" · ")}</div>
      ${(p.top_healing_spells||[]).length ? `<div class="dim">Healing: ${(p.top_healing_spells||[]).slice(0,8).map(s => `${esc(s.name)} ${num(s.total)}`).join(" · ")}</div>` : ""}
      <div class="dim">Damage taken: ${(p.top_damage_taken||[]).slice(0,8).map(s => `${esc(s.name)} ${num(s.total)}`).join(" · ")}</div>
      ${(p.buff_uptimes||[]).length ? `<div class="dim">Buff uptime: ${(p.buff_uptimes||[]).slice(0,10).map(b => `${esc(b.name)} ${b.uptime_pct}%`).join(" · ")}</div>` : ""}
      ${(p.damage_to_bosses ? `<div class="dim">Boss damage: ${num(p.damage_to_bosses)} (${p.damage_done ? Math.round(100*p.damage_to_bosses/p.damage_done) : 0}% of total)</div>` : "")}
      ${(p.potions_used || p.healthstones_used || p.distance_traveled) ? `<div class="dim">${p.potions_used ? p.potions_used + " potions · " : ""}${p.healthstones_used ? p.healthstones_used + " healthstones · " : ""}${p.distance_traveled ? "~" + num(p.distance_traveled) + " yd traveled" : ""}</div>` : ""}
      </details></td></tr>`).join("");
  return `<h2>Players</h2><div class="wrap"><table>
    <tr><th>Player</th><th>Spec</th><th class="num">DPS</th><th class="num">HPS</th>
    <th class="num">Damage</th><th class="num">Healing</th><th class="num">Absorbs</th>
    <th class="num">Taken</th><th class="num">Kicks</th>
    <th class="num" title="estimated damage/healing prevented by kicks">Kick prev.</th>
    <th class="num" title="dispels + purges">Dispels</th>
    <th class="num" title="killing blows">KB</th>
    <th class="num" title="casts per minute">CPM</th>
    <th class="num">Deaths</th></tr>${rows}</table></div>`;
}

function avoidableDamage() {
  const a = R.avoidable_damage;
  if (!a || !a.by_player || !a.by_player.length) return "";
  const rows = a.by_player.map(p => `<tr>
    <td>${esc(p.name)}</td>
    <td class="num">${num(p.avoidable_damage_taken)}</td>
    <td class="num">${p.avoidable_hits}</td>
    <td class="dim" style="white-space:normal">${(p.by_spell||[]).slice(0, 6)
      .map(s => `${esc(s.name)} ${num(s.amount)} (${s.hits}x)`).join(" · ")}</td></tr>`).join("");
  return `<h2>Avoidable damage taken (${a.tagged_spell_count} tagged spell${a.tagged_spell_count === 1 ? "" : "s"})</h2>
    <div class="wrap"><table>
    <tr><th>Player</th><th class="num">Damage</th><th class="num">Hits</th><th>By spell</th></tr>
    ${rows}</table></div>`;
}

function comparison() {
  const c = R.comparison;
  const rows = c.pulls.map(m => {
    const dev = [];
    if (m.pulled_early.length) dev.push(`<span class="dev-early">early: ${npcs(m.pulled_early)}</span>`);
    if (m.picked_up_late.length) dev.push(`<span class="dev-late">late: ${npcs(m.picked_up_late)}</span>`);
    if (m.off_route.length) dev.push(`<span class="dev-off">off-route: ${npcs(m.off_route)}</span>`);
    if (m.untracked.length) dev.push(`<span class="dim">adds: ${npcs(m.untracked)}</span>`);
    return `<tr><td class="num">${m.actual_pull}</td>
      <td class="num">${m.primary_plan_pull ?? '<span class="dev-off">—</span>'}</td>
      <td>${Object.entries(m.matched).map(([k, v]) => `#${k}: ${npcs(v)}`).join("; ") || '<span class="dim">nothing matched</span>'}</td>
      <td style="white-space:normal">${dev.join("<br>") || '<span class="ok">on plan</span>'}</td></tr>`;
  }).join("");
  let missed = "";
  const missedEntries = Object.entries(c.missed || {});
  if (missedEntries.length)
    missed = `<h2>Planned but never engaged</h2><div class="wrap"><table>
      <tr><th>Plan pull</th><th>Mobs</th></tr>
      ${missedEntries.map(([k, v]) => `<tr><td class="num">${k}</td><td>${npcs(v)}</td></tr>`).join("")}
      </table></div>`;
  return `<h2>Route vs actual (${R.route ? esc(R.route.name) : ""})</h2>
    <div class="wrap"><table>
    <tr><th class="num">Actual</th><th class="num">Plan</th><th>Matched</th><th>Deviations</th></tr>
    ${rows}</table></div>${missed}`;
}

function routeOnly() {
  if (!R.route || !R.route.pulls) return "";
  return `<div class="wrap"><table><tr><th class="num">Plan pull</th><th class="num">Forces</th><th>Mobs</th></tr>
    ${R.route.pulls.map(p => `<tr><td class="num">${p.pull}</td>
      <td class="num">${p.forces ?? ""}${p.forces_pct_cumulative != null ? ` <span class="dim">(${p.forces_pct_cumulative}%)</span>` : ""}</td>
      <td>${npcs(p.enemies || [])}</td></tr>`).join("")}</table></div>`;
}

function mapSection() {
  const m = R.map;
  if (!m || !(m.enemies||[]).length) return "";
  const b = m.bounds;
  const w = (b.max_x - b.min_x) || 1, h = (b.max_y - b.min_y) || 1;
  // Per-plan-pull colors are generated (not drawn from the theme's small
  // fixed --good/--bad/--warn/--blue/--accent set) since a route can have
  // far more pulls than that set has entries; the lightness/saturation is
  // chosen to sit in the same range as those vars so dots read consistently
  // against the existing dark panel background.
  const pullColor = i => i == null ? "var(--dim)" : `hsl(${(i * 63) % 360},65%,62%)`;
  const playerColor = i => ["var(--blue)","var(--accent)","var(--good)","var(--warn)",
    "#c774e8","#4fd1c5"][i % 6];

  const pois = (m.pois||[]).map(p => {
    const s = Math.max(w, h) * 0.02 * (p.size_mult || 1);
    return `<rect x="${(p.x - s/2).toFixed(1)}" y="${(p.y - s/2).toFixed(1)}"
      width="${s.toFixed(1)}" height="${s.toFixed(1)}" transform="rotate(45 ${p.x} ${p.y})"
      fill="var(--warn)" stroke="var(--bg)" stroke-width="1"><title>${esc(p.type)}</title></rect>`;
  }).join("");

  const enemyDots = m.enemies.map(e => {
    const fill = pullColor(e.plan_pull);
    const r = (e.is_boss ? 1.8 : 1.0) * Math.max(w, h) * 0.01;
    // deviated pulls get a dashed red ring in addition to their fill color
    // -- a shape/stroke distinction, not just a color swap, so it stays
    // legible for anyone not distinguishing hues easily.
    const stroke = e.deviated
      ? `stroke="var(--bad)" stroke-width="${(r*0.5).toFixed(2)}" stroke-dasharray="${(r*0.6).toFixed(2)},${(r*0.4).toFixed(2)}"`
      : `stroke="#00000066" stroke-width="${(r*0.15).toFixed(2)}"`;
    const tip = `${e.name}${e.plan_pull != null ? " — plan #" + e.plan_pull : " — not in route"}`
      + (e.deviated ? " (deviation)" : "");
    return `<circle cx="${e.x}" cy="${e.y}" r="${r.toFixed(2)}" fill="${fill}" ${stroke}>` +
      `<title>${esc(tip)}</title></circle>`;
  }).join("");

  const calibrated = m.calibration && m.calibration.ok;
  let paths = "", deathMarks = "";
  if (calibrated) {
    paths = (m.players||[]).map((p, i) => {
      const pts = p.path.map(pt => `${pt[1]},${pt[2]}`).join(" ");
      return `<polyline points="${pts}" fill="none" stroke="${playerColor(i)}"
        stroke-width="${(Math.max(w, h) * 0.004).toFixed(2)}" stroke-linejoin="round"
        opacity="0.8"><title>${esc(p.name)}'s path</title></polyline>`;
    }).join("");
    const s = Math.max(w, h) * 0.014;
    deathMarks = (m.deaths||[]).map(d => `<g stroke="var(--bad)" stroke-width="${(s*0.35).toFixed(2)}">
        <line x1="${(d.x-s).toFixed(1)}" y1="${(d.y-s).toFixed(1)}" x2="${(d.x+s).toFixed(1)}" y2="${(d.y+s).toFixed(1)}" />
        <line x1="${(d.x-s).toFixed(1)}" y1="${(d.y+s).toFixed(1)}" x2="${(d.x+s).toFixed(1)}" y2="${(d.y-s).toFixed(1)}" />
        <title>${esc(d.player)} died here (${mmss(d.t)})</title></g>`).join("");
  }

  const note = calibrated ? "" : `<div class="dim">No player-path overlay: ${
    esc((m.calibration||{}).reason || "not attempted")}.</div>`;

  return `<h2>Route map</h2><div class="wrap map-wrap">
    <svg viewBox="${b.min_x} ${b.min_y} ${w} ${h}" preserveAspectRatio="xMidYMid meet">
      ${pois}${enemyDots}${paths}${deathMarks}
    </svg></div>${note}
    <div class="legend">dot = planned enemy (color = plan pull; dashed red ring = route deviation)
      <i style="background:var(--warn)"></i>POI (entrance / marker)
      ${calibrated ? '<i style="background:var(--bad)"></i>death (×) · colored lines = player paths' : ""}</div>`;
}

function pullsTable() {
  const rows = (R.pulls||[]).map(p => `<tr>
    <td class="num">${p.pull}</td>
    <td>${mmss(p.t_start)}–${mmss(p.t_end)}</td>
    <td class="num">${p.duration_s}s</td>
    <td class="num">${p.mob_count}</td>
    <td class="num">${p.forces ? "+" + num(p.forces) : ""}</td>
    <td class="num">${num(p.group_damage)}</td>
    <td class="num${p.player_deaths ? ' dev-off' : ''}">${p.player_deaths || ""}</td>
    <td>${p.boss ? `<b>${esc(p.boss)}</b>` : ""}</td>
    <td style="white-space:normal" class="dim">${npcs(p.npcs)}</td></tr>`).join("");
  return `<h2>Pulls</h2><div class="wrap"><table>
    <tr><th class="num">#</th><th>Window</th><th class="num">Length</th>
    <th class="num">Mobs</th><th class="num">Forces</th><th class="num">Group dmg</th>
    <th class="num">Deaths</th><th>Boss</th><th>Pack</th></tr>${rows}</table></div>`;
}

function enemyCasts() {
  const spells = ((R.enemy_casts||{}).spells||[]).filter(s => s.got_through + s.kicked > 0);
  if (!spells.length) return "";
  spells.sort((a, b) => b.got_through - a.got_through);
  return `<h2>Enemy casts — kicked vs got through</h2><div class="wrap"><table>
    <tr><th>Spell</th><th class="num">Got through</th><th class="num">Kicked</th>
    <th class="num">Died mid-cast</th><th>Kick rate</th></tr>
    ${spells.slice(0, 15).map(s => {
      const total = s.got_through + s.kicked;
      const pct = total ? Math.round(100 * s.kicked / total) : 0;
      const cls = pct >= 70 ? "ok" : pct >= 30 ? "dev-early" : "dev-off";
      return `<tr><td>${esc(s.name)}</td>
        <td class="num${s.got_through ? " dev-off" : ""}">${s.got_through}</td>
        <td class="num">${s.kicked}</td><td class="num">${s.expired || ""}</td>
        <td><span class="${cls}">${pct}%</span></td></tr>`;
    }).join("")}</table></div>`;
}

function encounters() {
  const list = R.encounters || [];
  if (!list.some(e => !e.kill)) return "";
  return `<h2>Boss attempts</h2><div class="wrap"><table>
    <tr><th>Time</th><th>Boss</th><th>Result</th><th class="num">Length</th></tr>
    ${list.map(e => `<tr><td>${mmss(e.t)}</td><td>${esc(e.name)}</td>
      <td>${e.kill ? '<span class="ok">kill</span>' : '<span class="dev-off">wipe</span>'}</td>
      <td class="num">${Math.round(e.duration_s)}s</td></tr>`).join("")}
    </table></div>`;
}

function deaths() {
  const list = R.deaths || [];
  if (!list.length) return "";
  const rows = list.map(d => {
    const kb = d.killing_blow || {};
    const recap = (d.recap||[]).map(r =>
      `${mmss(r.ts - (R.run.start_ts||0))} ${esc(r.spell)} from ${esc(r.source)}: ${num(r.amount)}${r.hp_after != null ? ` (hp ${num(r.hp_after)})` : ""}`).join("<br>");
    const used = d.defensives_used_before_death || [];
    let defensive;
    if (used.length) {
      const names = used.map(u => `${esc(u.name)} (${Math.round(d.ts - u.ts)}s before)`).join(", ");
      defensive = `<span class="ok">${names}</span>`;
    } else if (d.died_without_defensive === true) {
      defensive = `<span class="bad">no defensive used</span>`;
    } else {
      // died_without_defensive is null for an unrecognized spec or one
      // with no known defensives -- an em-dash rather than a confusing
      // "unknown" label on every such death
      defensive = `<span class="dim">—</span>`;
    }
    return `<tr><td>${mmss(d.t)}</td><td>${esc(d.player)}</td>
      <td class="num">${d.pull ?? ""}</td>
      <td>${kb.spell ? `${esc(kb.spell)} from ${esc(kb.source)} for ${num(kb.amount)}` : '<span class="dim">?</span>'}</td>
      <td class="num">${num(d.biggest_hit)}</td>
      <td class="num">${num(d.damage_last_5s)}</td>
      <td>${defensive}</td>
      <td><details><summary class="dim">recap</summary>${recap}</details></td></tr>`;
  }).join("");
  return `<h2>Deaths</h2><div class="wrap"><table>
    <tr><th>Time</th><th>Player</th><th class="num">Pull</th><th>Killing blow</th>
      <th class="num">Biggest hit</th><th class="num">Last 5s</th><th>Defensive</th><th>Last hits</th></tr>
    ${rows}</table></div>`;
}

function closeCalls() {
  const list = R.close_calls || [];
  if (!list.length) return "";
  const rows = list.map(c => `<tr><td>${mmss(c.t)}</td><td>${esc(c.player)}</td>
    <td class="num">${c.pull ?? ""}</td>
    <td class="num dev-off">${c.hp_pct}%</td>
    <td>${esc(c.spell)}</td><td>${esc(c.source)}</td>
    <td class="num">${num(c.amount)}</td></tr>`).join("");
  return `<h2>Close calls</h2><div class="wrap"><table>
    <tr><th>Time</th><th>Player</th><th class="num">Pull</th><th class="num">HP left</th>
      <th>Spell</th><th>Source</th><th class="num">Amount</th></tr>
    ${rows}</table></div>`;
}

function utility() {
  const rows = [];
  (R.lust||[]).forEach(l => rows.push([l.t, "Bloodlust", `${esc(l.spell)}${l.source ? " (" + esc(l.source) + ")" : ""}`, l.pull]));
  (R.brez||[]).forEach(b => rows.push([b.t, "Battle res", `${esc(b.player)} → ${esc(b.target || "?")} (${esc(b.spell)})`, b.pull]));
  (R.interrupts||[]).forEach(i => {
    const est = [];
    if (i.estimated_prevented_damage) {
      const dot = i.prevented_dot_damage ? ` (${num(i.prevented_dot_damage)} DoT)` : "";
      est.push(`~${num(i.estimated_prevented_damage)} dmg${dot}`);
    }
    if (i.estimated_prevented_healing) est.push(`~${num(i.estimated_prevented_healing)} healing`);
    if (i.prevented_debuff_applications) est.push(`a debuff (seen ${i.prevented_debuff_applications}x, no dmg)`);
    const suffix = est.length
      ? ` — <span class="ok" title="average per completed cast (direct + periodic) over ${i.observed_casts} observed casts in this run">${est.join(" + ")} prevented</span>`
      : ' — <span class="dim">no landed casts to estimate from</span>';
    rows.push([i.t, "Interrupt", `${esc(i.player)} kicked ${esc(i.interrupted_spell || "?")} on ${esc(i.target)}${suffix}`, i.pull]);
  });
  (R.dispels||[]).forEach(d => rows.push([d.t, "Dispel", `${esc(d.player)} dispelled ${esc(d.dispelled_spell || "?")} on ${esc(d.target)}`, d.pull]));
  ((R.cc||{}).events||[]).forEach(c => rows.push([c.t_start, "CC",
    `${esc(c.caster || "?")} ${esc(c.spell)} on ${esc(c.target || "?")} (${c.duration_s.toFixed(1)}s)`, c.pull]));
  if (!rows.length) return "";
  rows.sort((a, b) => a[0] - b[0]);
  return `<h2>Utility timeline (lust · brez · kicks · dispels · CC)</h2>
    <div class="wrap"><table><tr><th>Time</th><th>Kind</th><th>What</th><th class="num">Pull</th></tr>
    ${rows.map(r => `<tr><td>${mmss(r[0])}</td><td>${r[1]}</td><td>${r[2]}</td><td class="num">${r[3] ?? ""}</td></tr>`).join("")}
    </table></div>`;
}

function downtime() {
  const w = ((R.downtime||{}).windows||[]).filter(w => w.seconds >= 10);
  if (!w.length) return "";
  w.sort((a, b) => b.seconds - a.seconds);
  return `<h2>Longest downtime</h2><div class="wrap"><table>
    <tr><th>Starts</th><th class="num">Idle</th><th>Between</th></tr>
    ${w.slice(0, 12).map(x => `<tr><td>${mmss(x.t)}</td>
      <td class="num">${Math.round(x.seconds)}s</td>
      <td>pull ${x.after_pull} → ${x.before_pull}</td></tr>`).join("")}
    </table></div>`;
}

render();
</script>
</body>
</html>
"""


def render_html(report: dict[str, Any]) -> str:
    run = report.get("run", {})
    title = f"{run.get('zone') or report.get('dungeon', {}).get('name', 'M+')}" \
            f" +{run.get('keystone_level', '?')} — post-mortem"
    # html.escape the title before it lands in <title>__TITLE__</title>:
    # `zone` (and keystone_level) come straight from a combat log's
    # CHALLENGE_MODE_START, i.e. attacker-chosen text (a player names their
    # own character/the log is uploaded to the public tracker), and the
    # site's CSP allows inline scripts -- an un-escaped "</title><script>"
    # zone was a real stored-XSS vector (found 2026-09-01). The embedded
    # JSON below is separately guarded by the </-splitting on the next
    # line, and every log-derived field the client-side JS renders goes
    # through its own esc(); this <title> was the one server-side gap.
    payload = json.dumps(report).replace("</", "<\\/")
    return _TEMPLATE.replace("__TITLE__", html.escape(title)).replace(
        "__REPORT_JSON__", payload
    )
