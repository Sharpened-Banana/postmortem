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
/* Postmortem brand system (2026-09-11). Two typefaces doing two jobs:
   Barlow Condensed, uppercase and tracked, for the run's own name; IBM
   Plex Mono for everything that is a number, a label or a timing -- which,
   on this page, is nearly all of it. The families are named with real
   fallbacks and NOT linked here: a report saved to disk has to look right
   with no network, and the public site injects the webfont link itself
   (see postmortem_site's _FONTS_LINK). */
:root {
  --bg: #0E0D0C; --panel: #141210; --panel2: #1A1714; --line: #2A2621;
  --text: #F3EFE6; --dim: #A39B8E; --accent: #C9A227; --accent-dim: #6B5718;
  --good: #5CB85C; --bad: #D9534F; --warn: #E0A13C; --blue: #5c9ad0;
  --steal: #b47fdb; --muted: #C4BCAE;
  --display: "Barlow Condensed", "Oswald", "Roboto Condensed", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--muted);
  font: 14px/1.65 var(--mono); padding: 24px;
  -webkit-font-smoothing: antialiased; }
h1 { font-family: var(--display); font-size: 34px; font-weight: 700;
  letter-spacing: .04em; text-transform: uppercase; line-height: 1;
  margin: 0 0 10px; color: var(--text); }
h2 { font-size: 12px; font-weight: 600; margin: 34px 0 12px;
  text-transform: uppercase; letter-spacing: .16em; color: var(--accent);
  border-bottom: 1px solid var(--line); padding-bottom: 8px; }
.sub { color: var(--dim); margin-bottom: 18px; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 12px;
  font-weight: 600; font-size: 11.5px; margin-right: 8px;
  letter-spacing: .04em; }
.badge.timed { background: #1d3a28; color: var(--good); }
.badge.over { background: #3a2d1d; color: var(--warn); }
.badge.abandoned { background: #232733; color: var(--dim); }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 5px 10px; border-bottom: 1px solid var(--line);
  white-space: nowrap; }
th { color: var(--dim); font-weight: 600; font-size: 11px;
  text-transform: uppercase; letter-spacing: .12em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
/* Long free-text cells (matched packs, deviations) wrap instead of forcing
   the table thousands of pixels wide -- which starved the one wrapping
   column next to them into a sliver hundreds of lines tall. */
td.txt { white-space: normal; min-width: 240px; vertical-align: top; }
td.txt .pk { display: block; }
td.txt .pk b { color: var(--dim); font-weight: 600; margin-right: 4px; }
.wrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 6px 4px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px; margin-bottom: 8px; }
.stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; }
.stat .v { font-family: var(--display); font-size: 30px; font-weight: 700;
  letter-spacing: .02em; line-height: 1.05; color: var(--text); }
.stat .l { font-size: 10.5px; color: var(--dim); text-transform: uppercase;
  letter-spacing: .12em; margin-top: 4px; }
.timeline { position: relative; background: var(--panel);
  border: 1px solid var(--line); border-radius: 10px; padding: 14px 10px 6px; }
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
/* Spellsteal-worthy casts (user-tagged, see --stealable-data): a left
   accent bar on the row rather than recoloring the kick-rate text, so it
   never collides with the ok/dev-early/dev-off kick-rate coloring
   already on that same row. */
tr.stealable { box-shadow: inset 3px 0 0 var(--steal); }
tr.stealable td:first-child::after { content: " \2605"; color: var(--steal); }
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

const R = deTag(JSON.parse(document.getElementById("report-data").textContent));
// Covers the single quote and backtick as well as the obvious four, so
// an escaped value is safe in a single-quoted attribute and in a
// template literal, not only in the double-quoted attributes this file
// happens to use today.
const esc = s => String(s ?? "").replace(/[&<>"'`]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;","`":"&#96;"}[c]));
// Every field in this report came out of an uploaded log, and on the
// public site that upload is anonymous -- so a value the schema calls a
// number can be arbitrary text. num() now rejects a non-number instead
// of stringifying it into the page (2026-09-11); pct() is the same
// guard for the several "value + %" call sites below.
const num = n => {
  const v = Number(n);
  if (n == null || n === "" || !Number.isFinite(v)) return "?";
  return Math.abs(v) >= 1e9 ? (v/1e9).toFixed(2)+"b" :
    Math.abs(v) >= 1e6 ? (v/1e6).toFixed(2)+"m" :
    Math.abs(v) >= 1e3 ? (v/1e3).toFixed(1)+"k" : Math.round(v).toString();
};
const plain = (n, fallback = "?") => {
  const v = Number(n);
  return n == null || n === "" || !Number.isFinite(v) ? fallback : String(v);
};
const pct = n => plain(n, "?") + "%";
const mmss = s => { if (s == null) return "?"; s = Math.round(s);
  const m = Math.floor(s/60), sec = s%60;
  return m >= 60 ? `${Math.floor(m/60)}:${String(m%60).padStart(2,"0")}:${String(sec).padStart(2,"0")}`
                 : `${m}:${String(sec).padStart(2,"0")}`; };
const npcs = list => (list||[]).map(e =>
  `${e.n}x ${esc(e.name || ("npc:"+e.npc_id))}`).join(", ");

function render() {
  const run = R.run, forces = R.forces || {}, dt = R.downtime || {};
  const dur = run.wall_duration_s;
  // A run cut off by the ingest event cap has the same shape as an
  // abandoned one -- no END, a duration stopping at the cap -- but it is
  // not the same thing, so say so rather than calling the key abandoned.
  let badge = run.truncated
    ? '<span class="badge abandoned">PARTIAL ANALYSIS (SIZE LIMIT)</span>'
    : '<span class="badge abandoned">INCOMPLETE / ABANDONED</span>';
  if (run.completed) badge = run.timed
    ? '<span class="badge timed">TIMED</span>'
    : '<span class="badge over">OVER TIMER</span>';

  let html = `<h1>${esc(run.zone || R.dungeon.name)} +${plain(run.keystone_level)}</h1>
  <div class="sub">${badge}
    ${run.duration_ms ? "In-game timer " + mmss(run.duration_ms/1000) + " · " : ""}
    wall clock ${mmss(dur)}
    ${run.affixes && run.affixes.length ? " · affixes " + esc(run.affixes.join(", ")) : ""}</div>`;

  html += timerInfo();

  html += `<div class="grid">
    ${forces.required ? stat(num(forces.killed), `forces killed / ${num(forces.required)} (${forces.pct}%)`) : ""}
    ${stat((R.deaths||[]).length, "player deaths")}
    ${stat(mmss(dt.combat_s), "combat time")}
    ${stat(mmss(dt.total_s), "downtime")}
    ${stat((R.pulls||[]).length, "actual pulls")}
    ${R.route ? stat(R.route.pull_count, "planned pulls") : ""}
    ${R.comparison && R.comparison.adherence_pct != null ? stat(pct(R.comparison.adherence_pct), "route adherence") : ""}
    ${R.kick_value && R.kick_value.total_estimated_prevented_damage ? stat("~" + num(R.kick_value.total_estimated_prevented_damage), "dmg prevented by kicks (est.)") : ""}
    ${R.kick_value && R.kick_value.total_estimated_prevented_healing ? stat("~" + num(R.kick_value.total_estimated_prevented_healing), "enemy healing prevented by kicks (est.)") : ""}
    ${R.enemy_casts && R.enemy_casts.kick_efficiency_pct != null ? stat(pct(R.enemy_casts.kick_efficiency_pct), "kick efficiency") : ""}
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
  html += dispelEfficiency();
  html += encounters();
  html += deaths();
  html += closeCalls();
  html += utility();
  html += downtime();
  document.getElementById("app").innerHTML = html;
}

const stat = (v, l) => `<div class="stat"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`;

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
      ${buildDetail(p)}
      </details></td></tr>`).join("");
  return `<h2>Players</h2><div class="wrap"><table>
    <tr><th>Player</th><th>Spec</th><th class="num">DPS</th><th class="num">HPS</th>
    <th class="num">Damage</th><th class="num">Healing</th><th class="num">Absorbs</th>
    <th class="num">Taken</th><th class="num">Kicks</th>
    <th class="num" title="estimated damage + enemy healing prevented by this player's interrupts -- the two summary tiles above are those same two components split apart, so this column sums to both of them together">Kick prev. (dmg+heal)</th>
    <th class="num" title="dispels + purges">Dispels</th>
    <th class="num" title="killing blows">KB</th>
    <th class="num" title="casts per minute">CPM</th>
    <th class="num">Deaths</th></tr>${rows}</table></div>`;
}

// The talent build and gear this player brought to THIS run, read from
// the log's own COMBATANT_INFO -- not their current armory state. A
// choice node the run couldn't settle (neither option's spell ever
// showed up) is printed as "A / B" rather than guessed at.
function buildDetail(p) {
  const b = p.build;
  if (!b) return "";
  let out = "";
  const t = b.talents;
  if (t && (t.picks||[]).length) {
    const named = t.picks.filter(x => x.name)
      .map(x => esc(x.name) + (x.rank > 1 ? ` <span class="dim">×${x.rank}</span>` : ""));
    const unsure = t.picks.filter(x => x.options)
      .map(x => `<span class="dev-late">${x.options.map(esc).join(" / ")}</span>`);
    out += `<div class="dim">Talents (${t.named_count}/${t.total_count} identified`
      + (t.ambiguous_count ? `, ${t.ambiguous_count} either-or` : "") + `): `
      + named.join(" · ") + (unsure.length ? " · " + unsure.join(" · ") : "") + `</div>`;
  }
  const g = b.gear;
  if (g && (g.items||[]).length) {
    const items = g.items.map(i => {
      const marks = [];
      if ((i.enchants||[]).length) marks.push("ench");
      if ((i.gems||[]).length) marks.push(`${i.gems.length}g`);
      return `${esc(i.slot)} ${i.item_level}${marks.length ? " (" + marks.join(",") + ")" : ""}`;
    });
    out += `<div class="dim">Gear${g.average_item_level ? ` (avg ${g.average_item_level})` : ""}: `
      + items.join(" · ") + `</div>`;
  }
  return out;
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
    const notes = [];
    if ((m.chained || []).length) notes.push(`<span class="ok">chained with #${m.chained.join(", #")}</span>`);
    if ((m.extra || []).length) notes.push(`<span class="dim">also engaged: ${npcs(m.extra)}</span>`);
    if (m.untracked.length) notes.push(`<span class="dim">adds: ${npcs(m.untracked)}</span>`);
    dev.push(...notes);
    return `<tr><td class="num">${m.actual_pull}</td>
      <td class="num">${m.primary_plan_pull ?? '<span class="dev-off">—</span>'}</td>
      <td class="txt">${Object.entries(m.matched).map(([k, v]) => `<span class="pk"><b>#${k}</b>${npcs(v)}</span>`).join("") || '<span class="dim">nothing matched</span>'}</td>
      <td class="txt">${dev.join("<br>") || '<span class="ok">on plan</span>'}</td></tr>`;
  }).join("");
  let missed = "";
  const missedEntries = Object.entries(c.missed || {});
  if (missedEntries.length)
    missed = `<h2>Planned but never engaged</h2><div class="wrap"><table>
      <tr><th>Plan pull</th><th>Mobs</th></tr>
      ${missedEntries.map(([k, v]) => `<tr><td class="num">${k}</td><td class="txt">${npcs(v)}</td></tr>`).join("")}
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
      <td class="txt">${npcs(p.enemies || [])}</td></tr>`).join("")}</table></div>`;
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

  // Points carry the floor (sublevel) they live on. Multi-floor dungeons
  // stack several 840x555 canvases on the same coordinates, so drawing
  // them all at once overlays unrelated rooms and only ever loads floor
  // 1's art. Draw the floor most of the planned pull sits on, and say so
  // when there are others. (Every dungeon MDT bundles today is single
  // floor, so this normally changes nothing.)
  const floorOf = o => Number(o && o.sublevel) || 1;
  const floorCounts = {};
  (m.enemies||[]).forEach(e => { const f = floorOf(e); floorCounts[f] = (floorCounts[f]||0) + 1; });
  const floors = Object.keys(floorCounts).map(Number).sort((a,b) => a-b);
  const primaryFloor = floors.length
    ? floors.reduce((best, f) => floorCounts[f] > floorCounts[best] ? f : best, floors[0])
    : 1;
  const onFloor = o => floors.length < 2 || floorOf(o) === primaryFloor;

  const pois = (m.pois||[]).filter(onFloor).map(p => {
    const s = Math.max(w, h) * 0.02 * (p.size_mult || 1);
    return `<rect x="${(p.x - s/2).toFixed(1)}" y="${(p.y - s/2).toFixed(1)}"
      width="${s.toFixed(1)}" height="${s.toFixed(1)}" transform="rotate(45 ${Number(p.x) || 0} ${Number(p.y) || 0})"
      fill="var(--warn)" stroke="var(--bg)" stroke-width="1"><title>${esc(p.type)}</title></rect>`;
  }).join("");

  const enemyDots = m.enemies.filter(onFloor).map(e => {
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
    return `<circle cx="${Number(e.x) || 0}" cy="${Number(e.y) || 0}" r="${r.toFixed(2)}" fill="${fill}" ${stroke}>` +
      `<title>${esc(tip)}</title></circle>`;
  }).join("");

  const calibrated = m.calibration && m.calibration.ok;
  let paths = "", deathMarks = "";
  if (calibrated) {
    // One polyline per contiguous segment (p.segments; older reports only
    // have the flat p.path) so a stretch with no drawable samples -- a
    // sub-map that didn't calibrate -- is a gap, not a straight line
    // across the dungeon.
    paths = (m.players||[]).map((p, i) => (p.segments || [p.path]).map(seg => {
      const pts = seg.map(pt => `${Number(pt[1]) || 0},${Number(pt[2]) || 0}`).join(" ");
      return `<polyline points="${pts}" fill="none" stroke="${playerColor(i)}"
        stroke-width="${(Math.max(w, h) * 0.004).toFixed(2)}" stroke-linejoin="round"
        opacity="0.8"><title>${esc(p.name)}'s path</title></polyline>`;
    }).join("")).join("");
    const s = Math.max(w, h) * 0.014;
    deathMarks = (m.deaths||[]).map(d => `<g stroke="var(--bad)" stroke-width="${(s*0.35).toFixed(2)}">
        <line x1="${(d.x-s).toFixed(1)}" y1="${(d.y-s).toFixed(1)}" x2="${(d.x+s).toFixed(1)}" y2="${(d.y+s).toFixed(1)}" />
        <line x1="${(d.x-s).toFixed(1)}" y1="${(d.y+s).toFixed(1)}" x2="${(d.x+s).toFixed(1)}" y2="${(d.y-s).toFixed(1)}" />
        <title>${esc(d.player)} died here (${mmss(d.t)})</title></g>`).join("");
  }

  const note = calibrated ? "" : `<div class="dim">No player-path overlay: ${
    esc((m.calibration||{}).reason || "not attempted")}.</div>`;

  // Coordinates are MDT's planning canvas: x 0..840 left->right, y 0..-555
  // TOP->BOTTOM (y grows negative going down). SVG's y grows downward, so
  // drawing the raw values put the dungeon upside-down -- unnoticeable
  // with dots alone, wrong the moment the real map sits underneath. The
  // content group flips y once; the viewBox is expressed in that flipped
  // frame. The floor's map art (m.backgrounds, embedded by the desktop
  // app from the user's own MDT install -- see mapart.py) is the whole
  // 840x555 canvas at (0,0) in the flipped frame, so it lines up with
  // every dot with no further transform; the viewBox still crops to the
  // planned pack extents as before.
  const bg = (m.backgrounds || {})[String(primaryFloor)];
  const cw = Number((m.canvas || {}).width) || 840, ch = Number((m.canvas || {}).height) || 555;
  // The background is a data: URI built by mapart.py, but it arrives
  // here inside the uploaded report like everything else -- so require
  // the shape rather than trusting it, and escape what is left.
  const safeBg = bg && typeof bg.data_uri === "string"
    && /^data:image[/](png|jpeg|webp);base64,[A-Za-z0-9+=]+$/.test(bg.data_uri)
    ? bg.data_uri : null;
  const image = safeBg
    ? `<image href="${esc(safeBg)}" x="0" y="0" width="${cw}" height="${ch}" preserveAspectRatio="none" />`
    : "";
  const dotOpacity = image ? ` opacity="0.92"` : "";

  return `<h2>Route map</h2><div class="wrap map-wrap${image ? " has-art" : ""}">
    <svg viewBox="${Number(b.min_x) || 0} ${(-b.max_y).toFixed(1)} ${Number(w) || 0} ${Number(h) || 0}" preserveAspectRatio="xMidYMid meet">
      ${image}<g transform="scale(1,-1)"${dotOpacity}>${pois}${enemyDots}${paths}${deathMarks}</g>
    </svg></div>${note}
    <div class="legend">dot = planned enemy (color = plan pull; dashed red ring = route deviation)
      <i style="background:var(--warn)"></i>POI (entrance / marker)
      ${calibrated ? '<i style="background:var(--bad)"></i>death (×) · colored lines = player paths' : ""}
      ${image ? " · map art from your MDT install" : ""}
      ${floors.length > 1 ? ` · floor ${primaryFloor} of ${floors.length} (other floors not drawn)` : ""}</div>`;
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
  const all = ((R.enemy_casts||{}).spells||[]).filter(s => s.got_through + s.kicked > 0);
  if (!all.length) return "";
  all.sort((a, b) => (b.got_through + b.kicked) - (a.got_through + a.kicked) || b.got_through - a.got_through);
  // Top 15 by volume, plus EVERY spell that was kicked at all -- so the
  // kicked column always sums to the players table's kick counts.
  const spells = all.filter((s, i) => i < 15 || s.kicked > 0);
  const kickedTotal = spells.reduce((n, s) => n + s.kicked, 0);
  const anyStealable = spells.some(s => s.stealable);
  return `<h2>Enemy casts — kicked vs got through <span class="dim" style="font-weight:400;font-size:13px">(${kickedTotal} kick${kickedTotal === 1 ? "" : "s"} total)</span></h2><div class="wrap"><table>
    <tr><th>Spell</th><th class="num">Got through</th><th class="num">Kicked</th>
    <th class="num">Died mid-cast</th><th>Kick rate</th></tr>
    ${spells.map(s => {
      const total = s.got_through + s.kicked;
      const pct = total ? Math.round(100 * s.kicked / total) : 0;
      const cls = pct >= 70 ? "ok" : pct >= 30 ? "dev-early" : "dev-off";
      return `<tr class="${s.stealable ? "stealable" : ""}"${s.stealable ? ' title="Worth Spellstealing"' : ""}>
        <td>${esc(s.name)}</td>
        <td class="num${s.got_through ? " dev-off" : ""}">${plain(s.got_through, "0")}</td>
        <td class="num">${plain(s.kicked, "0")}</td><td class="num">${s.expired ? plain(s.expired, "") : ""}</td>
        <td><span class="${cls}">${pct}%</span></td></tr>`;
    }).join("")}</table></div>${anyStealable
      ? `<div class="legend"><i style="background:var(--steal)"></i>★ worth Spellstealing</div>` : ""}`;
}

function dispelEfficiency() {
  const d = R.dispel_efficiency;
  if (!d || !(d.schools||[]).length) return "";
  const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
  const pctCls = p => p == null ? "dim" : p >= 80 ? "ok" : p >= 50 ? "dev-early" : "dev-off";
  const blocks = d.schools.map(s => {
    const who = s.dispellers.length
      ? s.dispellers.map(p => `${esc(p.name)} <span class="dim">(${esc([p.spec, p.class].filter(Boolean).join(" "))}${p.dispels ? ", " + p.dispels + " dispel" + (p.dispels === 1 ? "" : "s") : ""})</span>`).join(", ")
      : `<span class="dim">nobody in the group can dispel ${esc(s.school)} — not scored</span>`;
    const eff = s.efficiency_pct == null ? "—" : pct(s.efficiency_pct);
    const rows = s.spells.map(sp => `<tr><td>${esc(sp.name)}</td>
      <td class="num">${plain(sp.applied, "0")}</td><td class="num">${plain(sp.dispelled, "0")}</td>
      <td class="num${sp.expired && s.dispellers.length ? " dev-off" : ""}">${plain(sp.expired, "0")}</td>
      <td class="num">${sp.avg_time_to_dispel_s != null ? plain(sp.avg_time_to_dispel_s) + "s" : '<span class="dim">—</span>'}</td></tr>`).join("");
    return `<h3 style="margin:14px 0 6px;font-size:14px">${esc(cap(s.school))}
      <span class="${pctCls(s.efficiency_pct)}" style="margin-left:8px">${eff}</span>
      <span class="dim" style="font-weight:400;font-size:12.5px;margin-left:8px">${plain(s.dispelled, "0")} dispelled / ${plain(s.expired, "0")} ran out${s.avg_time_to_dispel_s != null ? ` · avg ${plain(s.avg_time_to_dispel_s)}s to dispel` : ""}</span></h3>
      <div class="dim" style="font-size:12.5px;margin-bottom:6px">Can dispel: ${who}</div>
      <div class="wrap"><table><tr><th>Debuff</th><th class="num">Applied</th><th class="num">Dispelled</th>
      <th class="num">Ran out</th><th class="num">Avg time to dispel</th></tr>${rows}</table></div>`;
  }).join("");
  const overall = d.overall_efficiency_pct == null ? "" : ` <span class="${pctCls(d.overall_efficiency_pct)}" style="font-size:14px;margin-left:8px">${plain(d.overall_efficiency_pct)}% overall</span>`;
  return `<h2>Dispel efficiency${overall}</h2>${blocks}`;
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
    const borrowed = i.estimate_source && i.estimate_source !== "observed";
    const basis = borrowed
      ? `no landed casts this run -- from ${i.estimate_source}: average of ${i.estimate_samples || "?"} casts at +${i.estimate_level}`
      : `average per completed cast (direct + periodic) over ${i.observed_casts} observed casts in this run`;
    const suffix = est.length
      ? ` — <span class="ok" title="${esc(basis)}">${est.join(" + ")} prevented${borrowed ? ` <span class="dim">(${esc(i.estimate_source)})</span>` : ""}</span>`
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
