"""Self-contained HTML post-mortem report.

The full report JSON is embedded in the page; a small amount of vanilla JS
renders the tables and the pull timeline. No external resources.
"""

from __future__ import annotations

import html
import json
import math
from typing import Any

from ..analysis.verdict import build_verdict
from .affixes import affix_js


def _finite_only(value: Any) -> Any:
    """``value`` with every non-finite float (inf, -inf, nan) replaced by
    None, recursively through dicts, lists and tuples."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite_only(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_only(v) for v in value]
    return value


def script_json(value: Any) -> str:
    """``value`` as JSON that is safe to embed in a ``<script>`` data block
    and that the browser's ``JSON.parse`` accepts.

    Python's ``json.dumps`` writes ``Infinity``/``NaN`` for non-finite
    floats, which is not JSON: ``JSON.parse`` throws on it and the page
    renders nothing at all. A ratio over a zero-length pull or a rate from
    an anonymous upload is enough to produce one, so they become null (the
    renderers already show null as "?"/"—"), and ``allow_nan=False`` makes
    any path that slips past the sanitiser fail here, loudly, rather than
    in every visitor's browser. "</" is split so a value cannot close the
    surrounding script element.
    """
    return json.dumps(_finite_only(value), allow_nan=False).replace("</", "<\\/")

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
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
   already on that same row. The star's CSS escape is doubled because
   this template is a plain Python string: a single backslash made it the
   octal escape \\260 and the page showed a degree sign and a 5. */
tr.stealable { box-shadow: inset 3px 0 0 var(--steal); }
tr.stealable td:first-child::after { content: " \\2605"; color: var(--steal); }
details { margin: 4px 0; }
summary { cursor: pointer; }
.legend { font-size: 12px; color: var(--dim); margin-top: 6px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin: 0 4px 0 12px; vertical-align: -1px; }
a.ability { color: inherit; text-decoration: none;
  border-bottom: 1px dotted var(--dim); }
a.ability:hover { color: var(--accent); border-bottom-color: var(--accent); }
.map-wrap { padding: 0; }
.map-wrap svg { display: block; width: 100%; height: auto; max-height: 560px;
  background: var(--panel2); }
/* Every section is a <details> whose summary is its h2, so a reader can
   fold away the tables they are not reading. The h2 carries a real
   <button> (aria-expanded / aria-controls, keyboard-focusable) as the
   chevron; clicking anywhere on the heading toggles too. Collapsed
   sections are remembered in localStorage by section id. On a phone the
   sticky index below the stats jumps between sections, so 13 tables are
   navigable rather than a scroll. The native marker is hidden: the
   button is the only affordance. */
details.sec { margin: 0; }
details.sec > summary { list-style: none; cursor: pointer; }
details.sec > summary::-webkit-details-marker { display: none; }
details.sec > summary h2 { display: flex; align-items: center;
  justify-content: space-between; gap: 10px; }
details.sec > summary .sec-title { min-width: 0; }
details.sec:not([open]) > summary h2 { color: var(--dim); }
.sec-toggle, .sec-all { font-family: var(--mono); background: var(--panel);
  color: var(--dim); border: 1px solid var(--line); border-radius: 999px;
  cursor: pointer; line-height: 1; }
.sec-toggle:hover, .sec-all:hover { color: var(--accent); border-color: var(--accent-dim); }
.sec-toggle:focus-visible, .sec-all:focus-visible { outline: 2px solid var(--accent);
  outline-offset: 2px; }
.sec-toggle { flex: none; width: 28px; height: 28px; padding: 0; font-size: 12px;
  display: inline-flex; align-items: center; justify-content: center; }
.sec-toggle::before { content: "\\25BE"; }
details.sec:not([open]) > summary .sec-toggle::before { content: "\\25B8"; }
/* Collapse all / expand all, above the sections (and above the phone's
   sticky index). Same pill vocabulary as the index chips. */
.sec-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  justify-content: flex-end; margin: 16px 0 -12px; }
.sec-all { font-size: 11px; text-transform: uppercase; letter-spacing: .12em;
  padding: 6px 12px; }
.sec-index { display: none; }
/* The pull timeline as one row per pull, same time axis: shown on a
   phone in place of the single hover-only strip (no hover on a phone). */
.tl-stack { display: none; }
/* Header meta: affix chips (same vocabulary as the Browse Runs feed in
   report/index.py) and the clock. */
.meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px;
  color: var(--dim); margin: 0 0 18px; font-size: 13px; }
.affixes { display: inline-flex; flex-wrap: wrap; gap: 6px; }
.affix { display: inline-block; font-size: 11px; padding: 2px 8px;
  border: 1px solid var(--line); border-radius: 4px; color: var(--dim);
  background: var(--bg); letter-spacing: .02em; cursor: default; }
.affix.base { color: var(--text); }
.affix.season { color: var(--accent); border-color: var(--accent); }
.affix.harsh { color: var(--warn); border-color: var(--warn); }
/* The verdict band: where the key was lost, directly under the title.
   Its colour is the outcome's -- timed green, over amber, abandoned red. */
.verdict { --vc: var(--muted); background: var(--panel); border: 1px solid var(--line);
  border-left: 4px solid var(--vc); border-radius: 10px; padding: 16px 22px 14px;
  margin: 0 0 14px; }
.verdict.timed { --vc: var(--good); }
.verdict.over { --vc: var(--warn); }
.verdict.abandoned, .verdict.partial { --vc: var(--bad); }
.v-top { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px;
  font-size: 12px; color: var(--dim); }
.v-top .badge { margin-right: 0; }
.v-stars { color: var(--accent); font-size: 17px; letter-spacing: 3px; line-height: 1; }
.v-stars .off { color: var(--accent-dim); }
.v-head { font-family: var(--display); font-weight: 700; text-transform: uppercase;
  letter-spacing: .03em; font-size: 46px; line-height: 1; color: var(--vc);
  margin: 12px 0 6px; overflow-wrap: anywhere; }
.v-head .u { text-transform: lowercase; }
.v-ctx { color: var(--text); margin: 0 0 4px; }
.v-cap { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .16em;
  color: var(--accent); margin: 14px 0 2px; }
.v-lines { list-style: none; margin: 0; padding: 0; }
.v-lines li { display: grid; grid-template-columns: 62px 1fr; gap: 14px; padding: 6px 0;
  border-top: 1px solid var(--line); color: var(--muted); }
.v-lines .v-s { text-align: right; font-weight: 600; font-variant-numeric: tabular-nums;
  color: var(--warn); white-space: nowrap; }
.v-lines li.deaths .v-s { color: var(--bad); }
.v-lines li.note .v-s, .v-lines .v-s.none { color: var(--dim); font-weight: 400; }
/* Four primary numbers, then everything else as a compact strip. */
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px; }
.stat .v.good { color: var(--good); } .stat .v.warn { color: var(--warn); }
.stat .v.bad { color: var(--bad); }
.stat .s { font-size: 12px; color: var(--muted); margin-top: 2px; }
.kpi2 { display: flex; flex-wrap: wrap; gap: 4px 20px; margin: 12px 0 0; padding: 9px 14px;
  border: 1px solid var(--line); border-radius: 10px; font-size: 12px; color: var(--dim); }
.kpi2 span { white-space: nowrap; }
.kpi2 b { color: var(--text); font-weight: 600; margin-right: 6px;
  font-variant-numeric: tabular-nums; }
/* Player names: class colour (lightened where the stock colour misses
   WCAG AA on the dark panel), realm dimmed, region dropped. */
.pn { font-weight: 600; }
.realm { color: var(--dim); font-weight: 400; font-size: .92em; }
.p-more, .ph-only { display: none; }
/* Pulls: the pack list keeps to ~2 lines, the rest behind "+N more";
   a pull somebody died in carries a red edge. */
tr.died { box-shadow: inset 3px 0 0 var(--bad); }
td.txt details.more { display: inline; margin: 0; }
td.txt details.more summary { display: inline; color: var(--accent); }
td.txt details.more[open] summary { display: none; }
/* Timeline: tapping (or clicking) a bar prints its pull here -- title
   tooltips never show on a touch screen. */
.tl-pull[data-pull], .tl-srow[data-pull] { cursor: pointer; }
.tl-pull.sel { outline: 2px solid var(--text); outline-offset: 1px; }
.tl-detail { font-size: 12.5px; color: var(--text); min-height: 1.65em; margin-top: 6px;
  overflow-wrap: anywhere; }
.tl-detail:empty::before { content: "Tap or hover a bar for pack details."; color: var(--dim); }
/* Legend chips (route map, timeline). */
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.chip { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px;
  color: var(--muted); border: 1px solid var(--line); border-radius: 999px;
  padding: 3px 10px; background: var(--panel); }
.chip i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; flex: none; }
.chip i.sq { border-radius: 2px; }
.chip i.dia { border-radius: 1px; transform: rotate(45deg); width: 8px; height: 8px; }
.chip i.ring { background: transparent; border: 2px dashed var(--bad); }
.chip i.line { width: 14px; height: 3px; border-radius: 2px; }
@media (max-width: 720px) {
  body { padding: 16px; font-size: 15px; }
  h1 { font-size: 28px; }
  .wrap { padding: 4px 2px; }
  th, td { padding: 5px 8px; }
  .verdict { padding: 14px 16px 12px; }
  .v-head { font-size: 36px; }
  .v-lines li { grid-template-columns: 52px 1fr; gap: 10px; font-size: 14px; }
  .kpis { grid-template-columns: 1fr 1fr; gap: 10px; }
  .kpis > .stat:last-child:nth-child(odd) { grid-column: 1 / -1; }
  .stat { padding: 12px 14px; }
  .stat .v { font-size: 26px; }
  /* section index: one sticky, sideways-scrolling strip of chips, right
     under the verdict -- a wrapped block of 13 chips ate a third of the
     screen while stuck to the top */
  .sec-index { display: flex; flex-wrap: nowrap; overflow-x: auto; gap: 6px; position: sticky;
    top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--bg); padding: 10px 0 8px;
    margin: 0 0 12px; border-bottom: 1px solid var(--line); scrollbar-width: none;
    -webkit-overflow-scrolling: touch; }
  .sec-index::-webkit-scrollbar { display: none; }
  .sec-index a { flex: none; }
  .sec-index a { font-size: 11px; letter-spacing: .04em; color: var(--muted);
    border: 1px solid var(--line); border-radius: 999px; padding: 5px 10px;
    text-decoration: none; white-space: nowrap; }
  .sec-index a:active { color: var(--accent); border-color: var(--accent); }
  /* the chevron button grows to a thumb-sized target without growing
     the heading: negative vertical margin eats the extra height */
  .sec-toggle { width: 40px; height: 40px; margin: -8px 0; font-size: 14px; }
  .sec-controls { justify-content: flex-start; margin: 14px 0 0; }
  .sec-all { padding: 8px 14px; }
  /* timeline: one row per pull */
  .tl-row { display: none; }
  .tl-stack { display: block; }
  .tl-srow { display: flex; align-items: center; gap: 8px; height: 20px; }
  .tl-slabel { flex: none; width: 34px; font-size: 11px; color: var(--dim);
    text-align: right; font-variant-numeric: tabular-nums; }
  .tl-slabel.boss { color: var(--accent); }
  .tl-strack { flex: 1; position: relative; height: 20px; min-width: 0; }
  .tl-strack .tl-pull { top: 3px; height: 14px; }
  .tl-strack .tl-death, .tl-strack .tl-lust { height: 20px; }
  .tl-axis { margin-left: 42px; }
  .legend { line-height: 1.9; }
  /* wide tables as cards: header row hidden, cells labelled by data-l */
  table.cards, table.cards tbody, table.cards tr, table.cards td { display: block; }
  table.cards tr:first-child { display: none; }
  table.cards tr { display: grid; grid-template-columns: 1fr 1fr; gap: 3px 10px;
    padding: 10px 8px; border-bottom: 1px solid var(--line); }
  table.cards tr:last-child { border-bottom: none; }
  table.cards td, table.cards td.num { padding: 0; border: 0; white-space: normal;
    text-align: left; min-width: 0; overflow-wrap: anywhere; }
  table.cards td:first-child, table.cards td[colspan], table.cards td.wide { grid-column: 1 / -1; }
  table.cards td:first-child:not([data-l]) { color: var(--text); font-weight: 600;
    font-size: 14.5px; }
  table.cards td[data-l]::before { content: attr(data-l); display: block;
    color: var(--dim); font-size: 10px; text-transform: uppercase; letter-spacing: .12em; }
  table.cards.list tr { grid-template-columns: 1fr; }
  table.cards.players td:nth-child(2) { grid-column: 1 / -1; margin: -2px 0 4px; }
  /* player cards: four role-aware numbers; the rest sit in the expander */
  table.cards.players td.xtra { display: none; }
  .p-more { display: block; color: var(--muted); margin: 6px 0 4px; }
  .ph-only { display: inline; }
  .tl-srow { height: 24px; }
  table.cards tr.stealable { box-shadow: inset 3px 0 0 var(--steal); }
  .map-wrap svg { max-height: 70vh; }
}
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
// stripping) keeps the characters readable. & is escaped too, so every
// string is exactly the HTML text of the original value and esc() below
// can tell deTag's entities from a literal "&lt;" in a name. Quotes are
// left for esc() at the attribute sites, because pre-escaping them here
// would double-escape the many legitimate apostrophes in WoW names.
function deTag(value) {
  if (typeof value === "string")
    return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
//
// An & that already starts one of deTag's entities is left alone: every
// report string has been through deTag, and escaping its &lt; again made
// a name with a bracket display as "A&lt;b&gt;". The output is just as
// inert -- no raw < > " ' ` survives, and & appears only as an entity.
// (Nothing on this page reads a report value back out of an attribute;
// the feed in report/index.py does, and has attr() for that.)
const esc = s => String(s ?? "").replace(/&(?!(?:amp|lt|gt);)|[<>"'`]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;","`":"&#96;"}[c]));

// Ability names link to the ability on Wowhead: a click opens its page,
// and where Wowhead's tooltip script loads (the public site, the desktop
// app online) hovering shows the in-game tooltip. Offline -- a saved
// report, no network -- they stay ordinary links. The id must be a
// positive integer, so no report value can shape the URL.
function ability(name, id) {
  const label = esc(name);
  const sid = Number(id);
  if (!Number.isInteger(sid) || sid <= 0) return label;
  return `<a class="ability" href="https://www.wowhead.com/spell=${sid}" data-wowhead="spell=${sid}"`
    + ` target="_blank" rel="noopener noreferrer">${label}</a>`;
}

// Wowhead's tooltip script, loaded once after the first render instead of
// as a <script src> in the template, so the page still has exactly one
// inline script for the site's CSP hash. Its defaults leave link text,
// colour and icons alone (no whTooltips config needed); it watches the
// document for hovers, so re-rendered rows keep working.
function loadAbilityTooltips() {
  if (typeof window === "undefined" || !document.createElement || !document.head) return;
  if (document.getElementById("wowhead-tooltips")) return;
  const s = document.createElement("script");
  s.id = "wowhead-tooltips";
  s.src = "https://wow.zamimg.com/js/tooltips.js";
  s.async = true;
  document.head.appendChild(s);
}
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
// The magnitude is formatted and the sign put in front: Math.floor on a
// negative made -65s "-2:-5". A non-number is "?", not "NaN:NaN".
const mmss = s => { if (s == null || s === "") return "?"; s = Math.round(Number(s));
  if (!Number.isFinite(s)) return "?";
  const sign = s < 0 ? "-" : ""; s = Math.abs(s);
  const m = Math.floor(s/60), sec = s%60;
  return sign + (m >= 60 ? `${Math.floor(m/60)}:${String(m%60).padStart(2,"0")}:${String(sec).padStart(2,"0")}`
                         : `${m}:${String(sec).padStart(2,"0")}`); };
const npcs = list => (list||[]).map(e =>
  `${plain(e.n)}x ${esc(e.name || ("npc:"+e.npc_id))}`).join(", ");
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

// Affix id -> name/short/kind tables, shared with the Browse Runs feed
// (report/affixes.py writes them in here when the module loads).
__AFFIX_JS__
function affixChips(ids) {
  if (!Array.isArray(ids) || !ids.length) return "";
  return `<span class="affixes">` + ids.map(id => {
    const k = String(id);
    const kind = own(AFFIX_KIND, k) ? AFFIX_KIND[k] : "";
    const name = own(AFFIXES, k) ? AFFIXES[k] : "Affix #" + k;
    return `<span class="affix${kind ? " " + kind : ""}" title="${esc(name)}">${esc(name)}</span>`;
  }).join("") + `</span>`;
}

// Class colours for player names on the dark panel. The stock WoW colours
// for Death Knight, Demon Hunter and Shaman miss WCAG AA (4.5:1) against
// --panel, so those three are lightened towards white until they pass;
// the rest are the stock colours. tests/test_report_header.py checks every
// entry against --bg, --panel and --panel2.
const CLASS_TEXT = {
  deathknight: "#D55D71", demonhunter: "#B75ED5", druid: "#FF7C0A",
  evoker: "#33937F", hunter: "#AAD372", mage: "#3FC7EB", monk: "#00FF98",
  paladin: "#F48CBA", priest: "#FFFFFF", rogue: "#FFF468", shaman: "#2484E2",
  warlock: "#8788EE", warrior: "#C69B3A",
};
let classByName = {};  // player name -> class, filled per render()
function classColor(cls) {
  const k = String(cls ?? "").toLowerCase().replace(/[^a-z]/g, "");
  return k && own(CLASS_TEXT, k) ? CLASS_TEXT[k] : "";
}
// "Name-Realm-US" as the name in its class colour and the realm dimmed;
// the region tail is dropped (every player in a key shares it). The full
// name stays in the title.
function who(name, cls) {
  const s = String(name ?? "?");
  const col = classColor(cls != null ? cls : (own(classByName, s) ? classByName[s] : ""));
  const style = col ? ` style="color:${col}"` : "";
  const m = /^([^-]+)-(.+?)(?:-(?:US|EU|KR|TW|CN))?$/.exec(s);
  if (!m) return `<span class="pn"${style} title="${esc(s)}">${esc(s)}</span>`;
  return `<span class="pn"${style} title="${esc(s)}">${esc(m[1])}</span><span class="realm">-${esc(m[2])}</span>`;
}

// Matches the phone breakpoint in the CSS. Absent (node harness, old
// webviews) means "not a phone".
const narrowScreen = () => typeof window !== "undefined" && !!window.matchMedia
  && window.matchMedia("(max-width: 720px)").matches;
// Long tables a phone reader rarely wants first: closed by default on a
// phone only, unless the reader opened them before. Desktop stays open.
const PHONE_COLLAPSED = ["sec-route-vs-actual", "sec-pulls", "sec-enemy-casts-kicked-vs-got-through"];

function render() {
  const run = R.run || {}, forces = R.forces || {}, dt = R.downtime || {};
  const dur = run.wall_duration_s;
  classByName = {};
  (R.players||[]).forEach(p => { if (p && typeof p.name === "string") classByName[p.name] = p.class; });
  // A run cut off by the ingest event cap has the same shape as an
  // abandoned one -- no END, a duration stopping at the cap -- but it is
  // not the same thing, so say so rather than calling the key abandoned.
  let badge = run.truncated
    ? '<span class="badge abandoned">PARTIAL ANALYSIS (SIZE LIMIT)</span>'
    : '<span class="badge abandoned">INCOMPLETE / ABANDONED</span>';
  // timed is null when the dungeon's timer is not known: say completed,
  // never guess (the log's own "success" flag is 1 on every finished key).
  if (run.completed) badge = run.timed == null
    ? '<span class="badge abandoned">COMPLETED</span>'
    : run.timed
    ? '<span class="badge timed">TIMED</span>'
    : '<span class="badge over">OVER TIMER</span>';

  const verdict = verdictBand(badge);
  let html = `<h1>${esc(run.zone || (R.dungeon || {}).name)} +${plain(run.keystone_level)}</h1>
  <div class="meta">${verdict ? "" : badge}${affixChips(run.affixes)}
    <span>${run.duration_ms ? "in-game " + mmss(run.duration_ms/1000) + " · " : ""}wall clock ${mmss(dur)}</span></div>`;
  html += verdict;

  sectionIds = {};
  collapsed = loadCollapsed();
  phoneLayout = narrowScreen();
  const sections = [
    timeline(), playersTable(), avoidableDamage(),
    (R.comparison && !R.comparison.error) ? comparison()
      : (R.route ? `<h2>Route</h2><div class="dim">${esc((R.comparison||{}).error || "")}</div>` + routeOnly() : ""),
    mapSection(), pullsTable(), enemyCasts(), dispelEfficiency(), encounters(),
    deaths(), closeCalls(), utility(), downtime(),
  ].filter(Boolean).map(section);
  // DOM order puts the (phone-only) index straight under the verdict,
  // ahead of the numbers; on a desktop it is hidden and the tiles follow
  // the verdict directly.
  html += `<nav class="sec-index">${sections.map(x => `<a href="#${x.id}">${x.title}</a>`).join("")}</nav>`;
  html += kpis(forces, dt);
  html += `<div class="sec-controls">`
    + `<button type="button" class="sec-all" data-all="collapse">Collapse all</button>`
    + `<button type="button" class="sec-all" data-all="expand">Expand all</button></div>`;
  html += sections.map(x => x.html).join("");
  const app = document.getElementById("app");
  app.innerHTML = html;
  loadAbilityTooltips();
  wireSections(app);
}

// Section folding. Delegated on #app, no inline handlers (the public site
// names this script's hash in its CSP), and attached once even if render()
// runs again. Every path that changes a section goes through the native
// <details> open state, so the "toggle" event is the one place that keeps
// the button's aria-expanded and localStorage in step.
let sectionsWired = false;
function wireSections(app) {
  if (sectionsWired || !app.addEventListener) return;
  sectionsWired = true;
  app.addEventListener("click", ev => {
    const at = ev.target.closest ? ev.target : null;
    if (!at) return;
    const toggle = at.closest(".sec-toggle");
    if (toggle) {
      // The button sits inside the <summary>; handle it here and stop the
      // summary's own activation so the click toggles exactly once.
      ev.preventDefault();
      const d = toggle.closest("details.sec");
      if (d) d.open = !d.open;
      return;
    }
    const bar = at.closest(".timeline [data-pull]");
    if (bar) { showPull(app, bar.getAttribute("data-pull")); return; }
    const all = at.closest(".sec-all");
    if (all) {
      const open = all.getAttribute("data-all") === "expand";
      app.querySelectorAll("details.sec").forEach(d => { d.open = open; });
      return;
    }
    // Tapping an index chip opens a section the reader had collapsed, so
    // the jump never lands on a closed summary.
    const chip = at.closest(".sec-index a[href^='#']");
    if (!chip) return;
    const target = document.getElementById(chip.getAttribute("href").slice(1));
    if (target && target.tagName === "DETAILS") target.open = true;
  });
  // "toggle" does not bubble; capture it on the container instead.
  app.addEventListener("toggle", ev => {
    const d = ev.target;
    if (!d || !d.classList || !d.classList.contains("sec")) return;
    const btn = d.querySelector(".sec-toggle");
    if (btn) {
      btn.setAttribute("aria-expanded", d.open ? "true" : "false");
      const t = d.querySelector(".sec-title");
      btn.setAttribute("aria-label", (d.open ? "Collapse " : "Expand ") + (t ? t.textContent.trim() : "section"));
    }
    const map = loadCollapsed();
    // A section that starts closed on a phone remembers being opened
    // there (0), so it stays open next time; anything else just forgets.
    if (d.open) {
      if (PHONE_COLLAPSED.includes(d.id) && narrowScreen()) map[d.id] = 0; else delete map[d.id];
    } else map[d.id] = 1;
    saveCollapsed(map);
  }, true);
}

// Tap (or click) a pull on the timeline: its details go in the line under
// the chart, because a title tooltip never shows on a touch screen. The
// attribute is only used to find the pull; what is printed comes from R.
function showPull(app, key) {
  const out = app.querySelector(".tl-detail");
  const p = (R.pulls||[]).find(x => String(x.pull) === String(key));
  if (!out || !p) return;
  app.querySelectorAll(".tl-pull.sel").forEach(el => el.classList.remove("sel"));
  app.querySelectorAll(`.tl-pull[data-pull="${esc(plain(p.pull, ""))}"]`).forEach(el => el.classList.add("sel"));
  const died = (R.deaths||[]).filter(d => d.pull === p.pull).length;
  out.innerHTML = `<b>Pull ${plain(p.pull)}</b>${p.boss ? " — " + esc(p.boss) : ""}`
    + ` · ${mmss(p.t_start)}–${mmss(p.t_end)} (${plain(p.duration_s)}s)`
    + (died ? ` · <span class="dev-off">${died} death${died === 1 ? "" : "s"}</span>` : "")
    + ((p.npcs||[]).length ? ` · <span class="dim">${npcs(p.npcs)}</span>` : "");
}

// Which sections the reader folded away last time, keyed by section id
// (which is the section's slugged title, so it survives across reports).
// Storage can be missing (the node test harness, private windows, a
// webview with storage off) or throw on access; either way the report
// simply renders fully expanded.
const COLLAPSED_KEY = "postmortem.report.collapsed";
let collapsed = {};
function loadCollapsed() {
  try {
    const v = JSON.parse(localStorage.getItem(COLLAPSED_KEY) || "{}");
    return v && typeof v === "object" && !Array.isArray(v) ? v : {};
  } catch (e) { return {}; }
}
function saveCollapsed(map) {
  try { localStorage.setItem(COLLAPSED_KEY, JSON.stringify(map)); } catch (e) {}
}

// One report section: an h2 followed by its content, wrapped so it can be
// collapsed and the index can jump to it. Section titles are the h2's own
// text, which came through deTag() like everything else; the id is the
// title slugged ("sec-pull-timeline"), numbered only on a repeat, so the
// same section keeps the same id from one report to the next.
let sectionIds = {};  // reset per render()
let phoneLayout = false;  // narrowScreen() at render time
function section(html) {
  const m = /^<h2>([\\s\\S]*?)<\\/h2>/.exec(html);
  if (!m) return { id: "", title: "", html };
  // A trailing <span> is a per-report annotation (the dispel score, the
  // kick total), not part of the name: left in, "85% overall" landed in
  // the id -- so the id and its remembered collapsed state changed from
  // one report to the next -- and in the phone index chip.
  const title = m[1].replace(/\\s*<span\\b[^>]*>[\\s\\S]*?<\\/span>\\s*$/, "")
    .replace(/<[^>]*>/g, "").replace(/\\s*\\(.*$/, "").trim();
  const base = "sec-" + (title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "section");
  const n = (sectionIds[base] || 0) + 1;
  sectionIds[base] = n;
  const id = n === 1 ? base : `${base}-${n}`;
  const pref = own(collapsed, id) ? collapsed[id] : undefined;
  const open = pref === undefined ? !(phoneLayout && PHONE_COLLAPSED.includes(id)) : !pref;
  const button = `<button type="button" class="sec-toggle" aria-expanded="${open ? "true" : "false"}"`
    + ` aria-controls="${id}-body" aria-label="${open ? "Collapse" : "Expand"} ${esc(title)}"></button>`;
  return { id, title, html: `<details class="sec"${open ? " open" : ""} id="${id}">`
    + `<summary><h2><span class="sec-title">${m[1]}</span>${button}</h2></summary>`
    + `<div class="sec-body" id="${id}-body">` + html.slice(m[0].length) + `</div></details>` };
}

// cls and sub are optional; cls is always one of this file's literals.
const stat = (v, l, cls = "", sub = "") => `<div class="stat"><div class="v${cls ? " " + cls : ""}">${esc(v)}</div>`
  + `<div class="l">${esc(l)}</div>${sub ? `<div class="s">${esc(sub)}</div>` : ""}</div>`;
const finite = v => v != null && v !== "" && typeof v !== "boolean" && Number.isFinite(Number(v));

// Four primary numbers (timer margin, deaths, forces, route adherence --
// each skipped when the report has no value), then a compact strip with
// everything else.
function kpis(forces, dt) {
  const t = R.timer || {}, c = R.comparison || {}, kv = R.kick_value || {}, ec = R.enemy_casts || {};
  const dc = R.death_cost || {};
  const tiles = [];
  if (finite(t.margin_ms)) {
    const m = Number(t.margin_ms);
    tiles.push(stat((m >= 0 ? "+" : "-") + mmss(Math.abs(m)/1000), m >= 0 ? "under the timer" : "over the timer",
      m >= 0 ? "good" : "warn", finite(t.par_ms) ? `par ${mmss(t.par_ms/1000)}` : ""));
  }
  const nd = (R.deaths||[]).length;
  tiles.push(stat(nd, nd === 1 ? "player death" : "player deaths", nd ? "bad" : "",
    finite(dc.total_s) && Number(dc.total_s) > 0 ? `-${mmss(dc.total_s)} on the timer` : ""));
  if (forces.required)
    tiles.push(stat(pct(forces.pct), "enemy forces", finite(forces.pct) && Number(forces.pct) < 100 ? "warn" : "",
      `${num(forces.killed)} / ${num(forces.required)}`));
  if (finite(c.adherence_pct))
    tiles.push(stat(pct(c.adherence_pct), "route adherence", "",
      R.route ? `${plain(R.route.pull_count)} planned · ${(R.pulls||[]).length} pulled` : ""));
  const strip = [];
  if (finite(dt.combat_s)) strip.push([mmss(dt.combat_s), "combat"]);
  if (finite(dt.total_s)) strip.push([mmss(dt.total_s), "downtime"]);
  strip.push([String((R.pulls||[]).length), "pulls"]);
  if (R.route && !finite(c.adherence_pct)) strip.push([plain(R.route.pull_count), "planned pulls"]);
  if (finite(ec.kick_efficiency_pct)) strip.push([pct(ec.kick_efficiency_pct), "kick efficiency"]);
  if (finite(kv.total_estimated_prevented_damage) && Number(kv.total_estimated_prevented_damage))
    strip.push(["~" + num(kv.total_estimated_prevented_damage), "dmg prevented by kicks (est.)"]);
  if (finite(kv.total_estimated_prevented_healing) && Number(kv.total_estimated_prevented_healing))
    strip.push(["~" + num(kv.total_estimated_prevented_healing), "enemy healing prevented by kicks (est.)"]);
  return `<div class="kpis">${tiles.join("")}</div>`
    + `<div class="kpi2">${strip.map(([v, l]) => `<span><b>${esc(v)}</b>${esc(l)}</span>`).join("")}</div>`;
}

// Par and the +2/+3 thresholds, for the verdict band's top line.
function parLabel() {
  const t = R.timer;
  if (!t || !finite(t.par_ms)) return "";
  return `par ${mmss(t.par_ms/1000)} · +2 at ${mmss(t.threshold_2_ms/1000)} · +3 at ${mmss(t.threshold_3_ms/1000)}`;
}

// The verdict band (analysis/verdict.py): headline in the outcome's
// colour, chest stars, and up to three "what cost time" lines plus notes.
// The verdict is stored in the report like everything else, so on the
// public site it is uploaded text: the outcome and kind pick classes only
// from fixed lists, seconds must be numbers, and all text is escaped.
// The display face is uppercase; a seconds unit stays lowercase so
// "14s" does not read as "14S". Runs on already-escaped text and only
// inserts fixed markup.
const unitCase = h => h.replace(/(\\d)s\\b/g, '$1<span class="u">s</span>');
const OUTCOMES = ["timed", "over", "abandoned", "partial", "completed"];
const LINE_KINDS = ["deaths", "wipe", "downtime", "route"];
function verdictBand(badge) {
  const v = R.verdict;
  if (!v || typeof v !== "object" || Array.isArray(v)) return "";
  const outcome = OUTCOMES.includes(v.outcome) ? v.outcome : "completed";
  // Clamped to a whole 0..3 like the feed's stars (chestCount in
  // report/index.py): "★".repeat(-1) throws and would blank the page.
  let stars = "";
  if (finite(v.chests)) {
    const n = Math.min(3, Math.max(0, Math.trunc(Number(v.chests))));
    stars = `<span class="v-stars" title="${n} chest${n === 1 ? "" : "s"}" aria-label="${n} of 3 chests">`
      + "★".repeat(n) + `<span class="off">${"★".repeat(3 - n)}</span></span>`;
  }
  const par = parLabel();
  const lines = (Array.isArray(v.lines) ? v.lines : []).slice(0, 3).filter(l => l && typeof l === "object");
  const notes = (Array.isArray(v.notes) ? v.notes : []).slice(0, 2).filter(l => l && typeof l === "object");
  const row = (l, isNote) => {
    const kind = LINE_KINDS.includes(l.kind) ? l.kind : "";
    const secs = !isNote && typeof l.seconds === "number" && Number.isFinite(l.seconds) && l.seconds > 0
      ? `<span class="v-s">-${l.seconds < 59.5 ? Math.round(l.seconds) + "s" : mmss(l.seconds)}</span>` : `<span class="v-s none">${isNote ? "note" : "—"}</span>`;
    return `<li class="${isNote ? "note" : kind}">${secs}<span>${esc(l.text)}</span></li>`;
  };
  const cap = outcome === "timed" ? "Where the time went"
    : outcome === "over" ? "Where the key was lost" : "What happened";
  return `<section class="verdict ${outcome}" aria-label="Verdict">
    <div class="v-top">${badge}${stars}${par ? `<span>${esc(par)}</span>` : ""}</div>
    <div class="v-head">${unitCase(esc(v.headline))}</div>
    ${v.context ? `<div class="v-ctx">${esc(v.context)}</div>` : ""}
    ${lines.length || notes.length ? `<div class="v-cap">${cap}</div><ul class="v-lines">`
      + lines.map(l => row(l, false)).join("") + notes.map(l => row(l, true)).join("") + `</ul>` : ""}
  </section>`;
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
    return `<div class="${cls}" data-pull="${esc(plain(p.pull, ""))}" style="left:${x(p.t_start)};width:${w}" title="${esc(tip)}"></div>`;
  }).join("");
  const deaths = (R.deaths||[]).map(d =>
    `<div class="tl-death" style="left:${x(d.t)}" title="${esc(mmss(d.t) + " " + d.player + " died")}"></div>`).join("");
  const lust = (R.lust||[]).map(l =>
    `<div class="tl-lust" style="left:${x(l.t)}" title="${esc(mmss(l.t) + " " + l.spell)}"></div>`).join("");
  let axis = "";
  const step = span > 2400 ? 600 : span > 1200 ? 300 : 120;
  for (let t = 0; t <= span; t += step)
    axis += `<span style="left:${x(t)}">${mmss(t)}</span>`;
  // Phone layout: the same bars, one row per pull, so each pull is
  // readable without hover. Deaths and lusts sit on the pull they fell in.
  const within = (t, p) => t >= p.t_start && t <= p.t_end;
  const stack = pulls.map(p => {
    const w = Math.max(0.15, 100 * (p.t_end - p.t_start) / span).toFixed(2) + "%";
    const cls = "tl-pull" + (p.boss ? " boss" : "") + (devByPull[p.pull] ? " dev" : "");
    const marks = (R.deaths||[]).filter(d => within(d.t, p)).map(d =>
        `<div class="tl-death" style="left:${x(d.t)}"></div>`).join("")
      + (R.lust||[]).filter(l => within(l.t, p)).map(l =>
        `<div class="tl-lust" style="left:${x(l.t)}"></div>`).join("");
    return `<div class="tl-srow" data-pull="${esc(plain(p.pull, ""))}"><span class="tl-slabel${p.boss ? " boss" : ""}" title="${esc(p.boss || "")}">${plain(p.pull)}</span>`
      + `<div class="tl-strack"><div class="${cls}" data-pull="${esc(plain(p.pull, ""))}" style="left:${x(p.t_start)};width:${w}"></div>${marks}</div></div>`;
  }).join("");
  return `<h2>Pull timeline</h2><div class="timeline">
    <div class="tl-row">${bars}${deaths}${lust}</div>
    <div class="tl-stack">${stack}</div>
    <div class="tl-axis">${axis}</div>
    <div class="tl-detail" aria-live="polite"></div>
    <div class="chips">
      <span class="chip"><i class="sq" style="background:var(--blue)"></i>trash pull</span>
      <span class="chip"><i class="sq" style="background:var(--accent)"></i>boss</span>
      <span class="chip"><i class="sq" style="outline:2px solid var(--bad);outline-offset:-2px"></i>route deviation</span>
      <span class="chip"><i class="line" style="background:var(--bad)"></i>death</span>
      <span class="chip"><i class="line" style="background:var(--good)"></i>bloodlust</span></div>
  </div>`;
}

function playersTable() {
  const players = (R.players||[]).filter(p => p.guid !== "_pets" || p.damage_done);
  players.sort((a, b) => b.damage_done - a.damage_done);
  // On a phone each card shows four numbers picked by role -- throughput
  // (HPS for a healer, DPS otherwise), damage taken, kicks, deaths -- and
  // the rest move into the expander (td.xtra is hidden there, .p-more
  // shown).
  const rows = players.map(p => {
    const healer = String(p.role || "").toLowerCase().startsWith("heal");
    const kickPrev = (p.kick_prevented_damage || p.kick_prevented_healing)
      ? "~" + num((Number(p.kick_prevented_damage)||0) + (Number(p.kick_prevented_healing)||0)) : "";
    const dispels = plain((Number(p.dispels)||0) + (Number(p.purges)||0), "0");
    const more = [[healer ? "DPS" : "HPS", num(healer ? p.dps : p.hps)], ["Damage", num(p.damage_done)],
      ["Healing", num(p.healing_done)], ["Absorbs", num(p.absorbs_granted)], ["Kick prev.", kickPrev],
      ["Dispels", dispels], ["KB", plain(p.killing_blows, "")], ["CPM", plain(p.cpm, "")]]
      .filter(([, v]) => v !== "").map(([l, v]) => `${l} <b>${esc(v)}</b>`).join(" · ");
    return `<tr>
    <td>${who(p.name || p.guid, p.class)}</td>
    <td class="dim">${esc([p.spec, p.class].filter(Boolean).join(" ") || "?")}${p.raiderio && p.raiderio.score ? ` <span title="Raider.io M+ score">· ${num(p.raiderio.score)} io</span>` : ""}</td>
    <td class="num${healer ? " xtra" : ""}" data-l="DPS">${num(p.dps)}</td><td class="num${healer ? "" : " xtra"}" data-l="HPS">${num(p.hps)}</td>
    <td class="num xtra" data-l="Damage">${num(p.damage_done)}</td>
    <td class="num xtra" data-l="Healing">${num(p.healing_done)}</td>
    <td class="num xtra" data-l="Absorbs">${num(p.absorbs_granted)}</td>
    <td class="num" data-l="Taken">${num(p.damage_taken)}</td>
    <td class="num" data-l="Kicks">${plain(p.interrupts, "0")}</td>
    <td class="num xtra" data-l="Kick prev." title="estimated damage + healing prevented by this player's interrupts">${kickPrev}</td>
    <td class="num xtra" data-l="Dispels">${dispels}</td>
    <td class="num xtra" data-l="KB">${plain(p.killing_blows, "")}</td>
    <td class="num xtra" data-l="CPM">${plain(p.cpm, "")}</td>
    <td class="num${p.deaths ? ' dev-off' : ''}" data-l="Deaths">${plain(p.deaths, "0")}</td></tr>
    <tr><td colspan="14" style="border-bottom:1px solid var(--line)">
      <details><summary class="dim"><span class="ph-only">more stats, </span>top abilities & buffs</summary>
      <div class="p-more">${more}</div>
      <div class="dim">Damage: ${(p.top_damage_spells||[]).slice(0,8).map(s => `${ability(s.name, s.spell_id)} ${num(s.total)}`).join(" · ")}</div>
      ${(p.top_healing_spells||[]).length ? `<div class="dim">Healing: ${(p.top_healing_spells||[]).slice(0,8).map(s => `${ability(s.name, s.spell_id)} ${num(s.total)}`).join(" · ")}</div>` : ""}
      <div class="dim">Damage taken: ${(p.top_damage_taken||[]).slice(0,8).map(s => `${ability(s.name, s.spell_id)} ${num(s.total)}`).join(" · ")}</div>
      ${(p.buff_uptimes||[]).length ? `<div class="dim">Buff uptime: ${(p.buff_uptimes||[]).slice(0,10).map(b => `${ability(b.name, b.spell_id)} ${b.uptime_pct}%`).join(" · ")}</div>` : ""}
      ${(p.damage_to_bosses ? `<div class="dim">Boss damage: ${num(p.damage_to_bosses)} (${p.damage_done ? Math.round(100*p.damage_to_bosses/p.damage_done) : 0}% of total)</div>` : "")}
      ${(p.potions_used || p.healthstones_used || p.distance_traveled) ? `<div class="dim">${p.potions_used ? p.potions_used + " potions · " : ""}${p.healthstones_used ? p.healthstones_used + " healthstones · " : ""}${p.distance_traveled ? "~" + num(p.distance_traveled) + " yd traveled" : ""}</div>` : ""}
      ${buildDetail(p)}
      </details></td></tr>`;
  }).join("");
  return `<h2>Players</h2><div class="wrap"><table class="cards players">
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
      .map(x => ability(x.name, x.spell_id) + (x.rank > 1 ? ` <span class="dim">×${x.rank}</span>` : ""));
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
    <td>${who(p.name)}</td>
    <td class="num" data-l="Damage">${num(p.avoidable_damage_taken)}</td>
    <td class="num" data-l="Hits">${plain(p.avoidable_hits, "0")}</td>
    <td class="dim wide" data-l="By spell" style="white-space:normal">${(p.by_spell||[]).slice(0, 6)
      .map(s => `${ability(s.name, s.spell_id)} ${num(s.amount)} (${plain(s.hits)}x)`).join(" · ")}</td></tr>`).join("");
  return `<h2>Avoidable damage taken (${a.tagged_spell_count} tagged spell${a.tagged_spell_count === 1 ? "" : "s"})</h2>
    <div class="wrap"><table class="cards">
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
    && /^data:image[/](png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(bg.data_uri)
    ? bg.data_uri : null;
  const image = safeBg
    ? `<image href="${esc(safeBg)}" x="0" y="0" width="${cw}" height="${ch}" preserveAspectRatio="none" />`
    : "";
  const dotOpacity = image ? ` opacity="0.92"` : "";

  return `<h2>Route map</h2><div class="wrap map-wrap${image ? " has-art" : ""}">
    <svg viewBox="${Number(b.min_x) || 0} ${(-b.max_y).toFixed(1)} ${Number(w) || 0} ${Number(h) || 0}" preserveAspectRatio="xMidYMid meet">
      ${image}<g transform="scale(1,-1)"${dotOpacity}>${pois}${enemyDots}${paths}${deathMarks}</g>
    </svg></div>${note}
    <div class="chips">
      <span class="chip"><i style="background:hsl(63,65%,62%)"></i>planned enemy · colour = plan pull</span>
      <span class="chip"><i class="ring"></i>route deviation</span>
      <span class="chip"><i class="dia" style="background:var(--warn)"></i>POI (entrance / marker)</span>
      ${calibrated ? '<span class="chip"><i class="line" style="background:var(--bad)"></i>death (×)</span>'
        + '<span class="chip"><i class="line" style="background:var(--blue)"></i>player paths</span>' : ""}
      ${image ? '<span class="chip">map art from your MDT install</span>' : ""}
      ${floors.length > 1 ? `<span class="chip">floor ${primaryFloor} of ${floors.length} · other floors not drawn</span>` : ""}</div>`;
}

function pullsTable() {
  // The pack list keeps to about two lines; the rest opens in place.
  const pack = list => {
    list = Array.isArray(list) ? list : [];
    if (list.length <= 4) return npcs(list);
    return `${npcs(list.slice(0, 3))}, <details class="more"><summary>+${list.length - 3} more</summary>${npcs(list.slice(3))}</details>`;
  };
  const rows = (R.pulls||[]).map(p => `<tr${p.player_deaths ? ' class="died"' : ""}>
    <td class="num">${plain(p.pull)}</td>
    <td>${mmss(p.t_start)}–${mmss(p.t_end)}</td>
    <td class="num">${p.duration_s}s</td>
    <td class="num">${p.mob_count}</td>
    <td class="num">${p.forces ? "+" + num(p.forces) : ""}</td>
    <td class="num">${num(p.group_damage)}</td>
    <td class="num${p.player_deaths ? ' dev-off' : ''}">${p.player_deaths || ""}</td>
    <td>${p.boss ? `<b>${esc(p.boss)}</b>` : ""}</td>
    <td class="txt dim">${pack(p.npcs)}</td></tr>`).join("");
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
  return `<h2>Enemy casts — kicked vs got through <span class="dim" style="font-weight:400;font-size:13px">(${kickedTotal} kick${kickedTotal === 1 ? "" : "s"} total)</span></h2><div class="wrap"><table class="cards">
    <tr><th>Spell</th><th class="num">Got through</th><th class="num">Kicked</th>
    <th class="num">Died mid-cast</th><th>Kick rate</th></tr>
    ${spells.map(s => {
      const total = s.got_through + s.kicked;
      const pct = total ? Math.round(100 * s.kicked / total) : 0;
      const cls = pct >= 70 ? "ok" : pct >= 30 ? "dev-early" : "dev-off";
      return `<tr class="${s.stealable ? "stealable" : ""}"${s.stealable ? ' title="Worth Spellstealing"' : ""}>
        <td>${ability(s.name, s.spell_id)}</td>
        <td class="num${s.got_through ? " dev-off" : ""}" data-l="Got through">${plain(s.got_through, "0")}</td>
        <td class="num" data-l="Kicked">${plain(s.kicked, "0")}</td><td class="num" data-l="Died mid-cast">${s.expired ? plain(s.expired, "") : ""}</td>
        <td data-l="Kick rate"><span class="${cls}">${pct}%</span></td></tr>`;
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
    const rows = s.spells.map(sp => `<tr><td>${ability(sp.name, sp.spell_id)}</td>
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
      `${mmss(r.ts - (R.run.start_ts||0))} ${ability(r.spell, r.spell_id)} from ${esc(r.source)}: ${num(r.amount)}${r.hp_after != null ? ` (hp ${num(r.hp_after)})` : ""}`).join("<br>");
    const used = d.defensives_used_before_death || [];
    let defensive;
    if (used.length) {
      const names = used.map(u => `${ability(u.name, u.spell_id)} (${Math.round(d.ts - u.ts)}s before)`).join(", ");
      defensive = `<span class="ok">${names}</span>`;
    } else if (d.died_without_defensive === true) {
      defensive = `<span class="bad">no defensive used</span>`;
    } else {
      // died_without_defensive is null for an unrecognized spec or one
      // with no known defensives -- an em-dash rather than a confusing
      // "unknown" label on every such death
      defensive = `<span class="dim">—</span>`;
    }
    return `<tr><td data-l="Time">${mmss(d.t)}</td><td data-l="Player">${who(d.player)}</td>
      <td class="num" data-l="Pull">${plain(d.pull, "")}</td>
      <td data-l="Killing blow">${kb.spell ? `${ability(kb.spell, kb.spell_id)} from ${esc(kb.source)} for ${num(kb.amount)}` : '<span class="dim">?</span>'}</td>
      <td class="num" data-l="Biggest hit">${num(d.biggest_hit)}</td>
      <td class="num" data-l="Last 5s">${num(d.damage_last_5s)}</td>
      <td data-l="Defensive">${defensive}</td>
      <td data-l="Post-mortem">${postMortem(d)}</td>
      <td data-l="Last hits"><details><summary class="dim">recap</summary>${recap}</details></td></tr>`;
  }).join("");
  return `<h2>Deaths</h2><div class="wrap"><table class="cards list">
    <tr><th>Time</th><th>Player</th><th class="num">Pull</th><th>Killing blow</th>
      <th class="num">Biggest hit</th><th class="num">Last 5s</th><th>Defensive</th>
      <th>Post-mortem</th><th>Last hits</th></tr>
    ${rows}</table></div>`;
}

// The tank death post-mortem cell (see analysis/tank_death.py): what was
// sitting ready, how long since an active-mitigation press, and what the
// group still had. Collapsed behind a <details> so the deaths table stays
// readable; the summary carries the headline so it is useful closed.
function postMortem(d) {
  const t = d.tank_analysis;
  if (!t || !t.scored) return '<span class="dim">—</span>';

  const unused = t.available_unused || [];
  const externals = t.externals_available || [];
  const never = t.never_used || [];
  const gap = t.mitigation_gap_s;

  const parts = [];
  const active = t.active_at_death || [];
  if (active.length) {
    parts.push(`<div><span class="ok">Was holding:</span> `
      + active.map(a => ability(a.name, a.spell_id)).join(", ") + `</div>`);
  }
  if (unused.length) {
    parts.push(`<div><span class="bad">Ready and unused:</span> ` + unused.map(u =>
      // never_cast entries come from the addon's spellbook capture: we know
      // they had it and never pressed it all run, so there is no
      // "off cooldown for N seconds" to quote -- it was up the whole time.
      u.never_cast
        ? `${ability(u.name, u.spell_id)} <span class="dim">(never pressed this run)</span>`
        : `${ability(u.name, u.spell_id)} <span class="dim">(off cooldown ${Math.round(u.ready_for_s)}s)</span>`
    ).join(", ") + `</div>`);
  }
  if (gap != null) {
    parts.push(`<div>Last active mitigation <b>${gap.toFixed(1)}s</b> before death</div>`);
  }
  if (externals.length) {
    parts.push(`<div><span class="bad">Group had up:</span> ` + externals.map(e =>
      `${ability(e.name, e.spell_id)} <span class="dim">(${esc(e.caster)})</span>`).join(", ") + `</div>`);
  }
  if (never.length) {
    // Never pressed all run. From the log alone that cannot be told apart
    // from "not talented", so it is an observation, not a mistake. The
    // addon's spellbook capture settles it per spell (n.known), and where
    // it has, the caveat is dropped rather than hedged out of habit.
    const proven = never.filter(n => n.known);
    const unsure = never.filter(n => !n.known);
    if (proven.length) {
      parts.push(`<div class="dim">Had, never used this run: `
        + proven.map(n => ability(n.name, n.spell_id)).join(", ") + `</div>`);
    }
    if (unsure.length) {
      parts.push(`<div class="dim">Never used this run (may not be talented): `
        + unsure.map(n => ability(n.name, n.spell_id)).join(", ") + `</div>`);
    }
  }
  if (!parts.length) return '<span class="ok">nothing left unused</span>';

  const headline = unused.length
    ? `<span class="bad">${unused.length} ready</span>`
    : (externals.length ? `<span class="bad">external up</span>` : "detail");
  return `<details><summary>${headline}</summary>${parts.join("")}</details>`;
}

function closeCalls() {
  const list = R.close_calls || [];
  if (!list.length) return "";
  const rows = list.map(c => `<tr><td data-l="Time">${mmss(c.t)}</td><td data-l="Player">${who(c.player)}</td>
    <td class="num" data-l="Pull">${c.pull ?? ""}</td>
    <td class="num dev-off" data-l="HP left">${c.hp_pct}%</td>
    <td data-l="Spell">${ability(c.spell, c.spell_id)}</td><td data-l="Source">${esc(c.source)}</td>
    <td class="num" data-l="Amount">${num(c.amount)}</td></tr>`).join("");
  return `<h2>Close calls</h2><div class="wrap"><table class="cards list">
    <tr><th>Time</th><th>Player</th><th class="num">Pull</th><th class="num">HP left</th>
      <th>Spell</th><th>Source</th><th class="num">Amount</th></tr>
    ${rows}</table></div>`;
}

function utility() {
  const rows = [];
  (R.lust||[]).forEach(l => rows.push([l.t, "Bloodlust", `${ability(l.spell, l.spell_id)}${l.source ? " (" + esc(l.source) + ")" : ""}`, l.pull]));
  (R.brez||[]).forEach(b => rows.push([b.t, "Battle res", `${esc(b.player)} → ${esc(b.target || "?")} (${ability(b.spell, b.spell_id)})`, b.pull]));
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
    rows.push([i.t, "Interrupt", `${esc(i.player)} kicked ${ability(i.interrupted_spell || "?", i.interrupted_spell_id)} on ${esc(i.target)}${suffix}`, i.pull]);
  });
  (R.dispels||[]).forEach(d => rows.push([d.t, "Dispel", `${esc(d.player)} dispelled ${ability(d.dispelled_spell || "?", d.dispelled_spell_id)} on ${esc(d.target)}`, d.pull]));
  ((R.cc||{}).events||[]).forEach(c => rows.push([c.t_start, "CC",
    `${esc(c.caster || "?")} ${ability(c.spell, c.spell_id)} on ${esc(c.target || "?")} (${c.duration_s.toFixed(1)}s)`, c.pull]));
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


# The affix tables go in once, here, so the inline script is still one
# fixed string: report/csp.py hashes _TEMPLATE as it stands after import.
_TEMPLATE = _TEMPLATE.replace("__AFFIX_JS__", affix_js())


def render_html(report: dict[str, Any]) -> str:
    # Reports stored before the verdict existed get one built now, from
    # the fields they already carry (see analysis/verdict.py).
    if not isinstance(report.get("verdict"), dict):
        report = {**report, "verdict": build_verdict(report)}
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
    payload = script_json(report)
    return _TEMPLATE.replace("__TITLE__", html.escape(title)).replace(
        "__REPORT_JSON__", payload
    )
