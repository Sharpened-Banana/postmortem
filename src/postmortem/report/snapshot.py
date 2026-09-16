"""Text and HTML renderers for a snapshot report (analysis/snapshot.py).

The HTML page is rendered entirely here, in Python, into static markup
with inline SVG charts: no script at all, so the page carries nothing a
Content-Security-Policy has to hash (report/csp.py still lists the
template so a script added later is covered automatically). Every value
that came out of a combat log goes through html.escape on its way in --
the same posture as report/html.py, minus the client-side renderer.

The brand CSS is lifted from report/html.py's own <style> block at import
time rather than copied, so the two pages can't drift apart.
"""

from __future__ import annotations

import html as _html
from typing import Any, Optional

from .html import _TEMPLATE as _RUN_TEMPLATE
from .text import _fmt_num, _fmt_time, _pull_label

_STYLE_START = _RUN_TEMPLATE.index("<style>")
_STYLE_END = _RUN_TEMPLATE.index("</style>") + len("</style>")
_BRAND_STYLE = _RUN_TEMPLATE[_STYLE_START:_STYLE_END]

_EXTRA_STYLE = """
<style>
/* snapshot-only additions on top of the run report's brand block */
.chart { background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 10px 8px 4px; margin-bottom: 12px; }
.chart svg { display: block; width: 100%; height: auto; }
.chart .cap { font-size: 11px; color: var(--dim); text-transform: uppercase;
  letter-spacing: .12em; margin: 0 6px 4px; }
.legend b { font-weight: 600; color: var(--text); }
.focus-note { color: var(--dim); font-style: italic; }
.marker { color: var(--accent); font-weight: 600; }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px; }
</style>
"""

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
__STYLE__
</head>
<body>
__BODY__
</body>
</html>
"""

#: Series colours, one per player, in the order the series dict lists them.
_PALETTE = ("#C9A227", "#5c9ad0", "#5CB85C", "#b47fdb", "#D9534F", "#E0A13C", "#C4BCAE")


def _esc(value: Any) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def _role_label(role: Optional[str]) -> str:
    return (role or "general").capitalize()


# --- text ---------------------------------------------------------------------

def render_snapshot_text(report: dict[str, Any]) -> str:
    out: list[str] = []
    add = out.append
    snap = report["snapshot"]
    run = report["run"]
    title = f"{run.get('zone') or 'M+'} +{run.get('keystone_level', '?')}"
    add("=" * 72)
    add(f"SNAPSHOT ({snap['role']}) at {_fmt_time(snap['t_marker'])} — {title}")
    add("=" * 72)
    add(f"Window: {_fmt_time(snap['t_start'])} – {_fmt_time(snap['t_end'])}"
        f"  ({snap['before_s']:.0f}s before / {snap['after_s']:.0f}s after,"
        f" {snap['window_s']:.0f}s total)")
    add(f"Focus: {snap.get('focus_player') or '(none)'}  |  source: {snap.get('source')}")

    # --- the moment itself ---
    around = report.get("around_marker") or {}
    add("")
    add(f"-- AROUND THE MARKER (±{around.get('radius_s', 10):.0f}s) " + "-" * 40)
    for d in around.get("deaths") or []:
        kb = d.get("killing_blow") or {}
        add(f"  {_fmt_time(d.get('t'))}  DEATH  {d['player']}"
            + (f"  killed by {kb.get('spell')} from {kb.get('source')}"
               f" for {_fmt_num(kb.get('amount'))}" if kb else ""))
    for c in around.get("close_calls") or []:
        add(f"  {_fmt_time(c.get('t'))}  CLOSE  {c['player']} dropped to {c['hp_pct']}%"
            f"  -- {c['spell']} from {c['source']} for {_fmt_num(c['amount'])}")
    hits = around.get("largest_hits") or []
    if hits:
        add("  Largest hits:")
        for h in hits:
            hp = f"  (→ {h['hp_pct']}% hp)" if h.get("hp_pct") is not None else ""
            add(f"    {_fmt_time(h.get('t'))}  {h['player']:<22}{_fmt_num(h['amount']):>8}"
                f"  {h['spell']} from {h['source']}{hp}")
    if not (around.get("deaths") or around.get("close_calls") or hits):
        add("  nothing landed on the group in this window")

    # --- focus ---
    focus = report.get("focus") or {}
    add("")
    add(f"-- {_role_label(focus.get('role')).upper()} FOCUS " + "-" * 50)
    if focus.get("note"):
        add(f"  {focus['note']}")
    elif focus.get("role") == "healer":
        _healer_text(add, focus)
    elif focus.get("role") == "tank":
        _tank_text(add, focus)

    # --- players ---
    add("")
    add("-- PLAYERS (window) " + "-" * 52)
    add(f"{'Player':<24}{'Spec':<22}{'DPS':>8}{'HPS':>8}{'Taken':>8}{'DTPS':>8}"
        f"{'Casts':>6}{'Int':>5}{'Dth':>5}")
    for p in sorted(report["players"], key=lambda p: -p["damage_done"]):
        spec = " ".join(x for x in (p.get("spec"), p.get("class")) if x) or "?"
        add(f"{(p['name'] or p['guid'])[:23]:<24}{spec[:21]:<22}"
            f"{_fmt_num(p.get('dps')):>8}{_fmt_num(p.get('hps')):>8}"
            f"{_fmt_num(p['damage_taken']):>8}{_fmt_num(p.get('dtps')):>8}"
            f"{p.get('casts_total', 0):>6}{p['interrupts']:>5}{p['deaths']:>5}")

    # --- deaths / close calls (whole window) ---
    deaths = report.get("deaths") or []
    if deaths:
        add("")
        add("-- DEATHS " + "-" * 62)
        for d in deaths:
            kb = d.get("killing_blow") or {}
            add(f"{_fmt_time(d.get('t'))}  {d['player']}  (pull {_pull_label(d.get('pull'))})"
                + (f"  killed by {kb.get('spell')} from {kb.get('source')}"
                   f" for {_fmt_num(kb.get('amount'))}" if kb else ""))
            used = d.get("defensives_used_before_death") or []
            if used:
                add("    used: " + ", ".join(u["name"] for u in used))
            elif d.get("died_without_defensive") is True:
                add("    no defensive used")
    close_calls = report.get("close_calls") or []
    if close_calls:
        add("")
        add("-- CLOSE CALLS " + "-" * 57)
        for c in close_calls:
            add(f"{_fmt_time(c.get('t'))}  {c['player']} dropped to {c['hp_pct']}% hp"
                f"  -- {c['spell']} from {c['source']} for {_fmt_num(c['amount'])}")

    # --- enemy casts ---
    spells = (report.get("enemy_casts") or {}).get("spells") or []
    if spells:
        add("")
        add("-- ENEMY CASTS " + "-" * 57)
        for s in spells[:12]:
            total = s["got_through"] + s["kicked"]
            add(f"  {s['name']:<32}{s['got_through']:>3} landed / {total:>3} casts"
                + (f"  ({s['kicked']} kicked)" if s["kicked"] else ""))

    # --- pulls ---
    pulls = report.get("pulls") or []
    if pulls:
        add("")
        add("-- PULLS IN WINDOW " + "-" * 53)
        for p in pulls:
            boss = f"  [BOSS: {p['boss']}]" if p.get("boss") else ""
            add(f"Pull {p['pull']:>3}  {_fmt_time(p['t_start'])}-{_fmt_time(p['t_end'])}"
                f" ({p['duration_s']:.0f}s)  {p['mob_count']} mobs{boss}")
    add("=" * 72)
    return "\n".join(out)


def _healer_text(add, f: dict[str, Any]) -> None:
    oh = f"{f['overheal_pct']}%" if f.get("overheal_pct") is not None else "?"
    add(f"  Healing: {_fmt_num(f['healing_done'])} effective, {oh} overheal,"
        f" {_fmt_num(f['absorbs_granted'])} absorbs")
    gcd = f"{f['gcd_use_pct']}%" if f.get("gcd_use_pct") is not None else "?"
    add(f"  Casts: {f['casts']} ({gcd} of {f['possible_gcds']:.0f} possible GCDs)")
    m = f.get("mana") or {}
    if m.get("start") is not None:
        add(f"  Mana: {m['start']:.0f}% at start, {m['min']:.0f}% min, {m['end']:.0f}% at end")
    else:
        add("  Mana: no data (advanced combat logging off, or not a mana user)")
    if f.get("healing_by_target"):
        add("  By target:")
        for t in f["healing_by_target"][:6]:
            add(f"    {t['name']:<24}{_fmt_num(t['total']):>8}"
                f"  (+{_fmt_num(t['overhealing'])} over)")
    if f.get("healing_by_spell"):
        add("  By spell:")
        for s in f["healing_by_spell"][:8]:
            add(f"    {s['name']:<28}{_fmt_num(s['total']):>8}")
    cds = f.get("cooldowns_used") or []
    add("  Cooldowns used: " + (", ".join(
        f"{c['spell']} @{_fmt_time(c['t'])}" for c in cds) if cds else "none"))
    ext = f.get("externals_given") or []
    if ext:
        add("  Externals: " + ", ".join(
            f"{c['spell']} → {c.get('target') or '?'} @{_fmt_time(c['t'])}" for c in ext))
    if f.get("dispels"):
        add(f"  Dispels: {len(f['dispels'])}")
    if f.get("damage_taken_by_player"):
        add("  Who took the damage:")
        for e in f["damage_taken_by_player"]:
            top = ", ".join(s["name"] for s in e["top_spells"][:3])
            add(f"    {e['name']:<24}{_fmt_num(e['damage_taken']):>8}  {top}")


def _tank_text(add, f: dict[str, Any]) -> None:
    d = f.get("dtps") or {}
    add(f"  Damage taken: {_fmt_num(f['damage_taken'])}"
        f"  (peak {_fmt_num(d.get('peak'))}/s, mean {_fmt_num(d.get('mean'))}/s)")
    pm = f.get("physical_vs_magic") or {}
    pct = f"{pm['physical_pct']}%" if pm.get("physical_pct") is not None else "?"
    add(f"  Physical {_fmt_num(pm.get('physical'))} ({pct}) / magic {_fmt_num(pm.get('magic'))}")
    add(f"  Self-healing: {_fmt_num(f.get('self_healing'))}")
    if f.get("active_mitigation"):
        add("  Active mitigation:")
        for m in f["active_mitigation"]:
            up = f"{m['uptime_pct']}%" if m.get("uptime_pct") is not None else "?"
            add(f"    {m['name']:<28}{m['casts']:>3} casts  {up:>6} uptime ({m['uptime_s']:.0f}s)")
    else:
        add(f"  Active mitigation: {f.get('mitigation_note') or 'no mitigation data'}")
    cds = f.get("cooldowns_used") or []
    add("  Cooldowns used: " + (", ".join(
        f"{c['spell']} @{_fmt_time(c['t'])}" for c in cds) if cds else "none"))
    if f.get("damage_by_spell"):
        add("  By spell:")
        for s in f["damage_by_spell"][:8]:
            add(f"    {s['name']:<28}{_fmt_num(s['total']):>8}")
    if f.get("damage_by_source"):
        add("  By source:")
        for s in f["damage_by_source"][:6]:
            add(f"    {s['name']:<28}{_fmt_num(s['total']):>8}")
    if f.get("biggest_hits"):
        add("  Biggest hits:")
        for h in f["biggest_hits"][:5]:
            add(f"    {_fmt_time(h.get('t'))}  {_fmt_num(h['amount']):>8}  {h['spell']} from {h['source']}")


# --- html ---------------------------------------------------------------------

def _svg_line_chart(
    series: list[tuple[str, list[Optional[float]], str]],
    *,
    t0: float,
    marker_t: float,
    y_max: Optional[float] = None,
    y_label: str = "",
    width: int = 900,
    height: int = 220,
) -> str:
    """One inline SVG: a line per (name, values, colour), x in seconds
    from ``t0`` (run-relative), a vertical marker line at ``marker_t``.
    None values are skipped (the line breaks there)."""
    left, right, top, bottom = 48, 12, 10, 26
    n = max((len(v) for _, v, _ in series), default=0)
    if n < 2:
        return '<div class="focus-note">no data in this window</div>'
    if y_max is None:
        y_max = max((v for _, vals, _ in series for v in vals if v is not None), default=0)
    y_max = y_max or 1.0
    plot_w, plot_h = width - left - right, height - top - bottom

    def x_of(i: int) -> float:
        return left + plot_w * i / (n - 1)

    def y_of(v: float) -> float:
        return top + plot_h * (1.0 - min(v, y_max) / y_max)

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(y_label)}">']
    # gridlines + y ticks
    for k in range(5):
        v = y_max * k / 4
        y = y_of(v)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
                     f'stroke="#2A2621" stroke-width="1"/>')
        label = f"{v:.0f}%" if "%" in y_label else _fmt_num(v)
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" '
                     f'font-size="10" fill="#A39B8E">{_esc(label)}</text>')
    # x ticks every ~30 s
    step = 30 if n > 90 else 15 if n > 40 else 5
    for i in range(0, n, step):
        parts.append(f'<text x="{x_of(i):.1f}" y="{height - 8}" text-anchor="middle" '
                     f'font-size="10" fill="#A39B8E">{_esc(_fmt_time(t0 + i))}</text>')
    # marker
    mi = marker_t - t0
    if 0 <= mi <= n - 1:
        mx = x_of(int(mi)) + (plot_w / (n - 1)) * (mi - int(mi))
        parts.append(f'<line x1="{mx:.1f}" y1="{top}" x2="{mx:.1f}" y2="{top + plot_h}" '
                     f'stroke="#C9A227" stroke-width="2" stroke-dasharray="4 3"/>')
    for name, values, colour in series:
        pts = " ".join(
            f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in enumerate(values) if v is not None
        )
        if pts:
            parts.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                         f'stroke-width="1.6" stroke-linejoin="round"><title>{_esc(name)}</title></polyline>')
    parts.append("</svg>")
    return "".join(parts)


def _legend(names: list[str]) -> str:
    return '<div class="legend">' + "".join(
        f'<i style="background:{_PALETTE[i % len(_PALETTE)]}"></i><b>{_esc(n)}</b>'
        for i, n in enumerate(names)
    ) + "</div>"


def _chart(caption: str, svg: str, names: list[str]) -> str:
    return f'<div class="chart"><div class="cap">{_esc(caption)}</div>{svg}{_legend(names)}</div>'


def _table(headers: list[tuple[str, bool]], rows: list[list[Any]]) -> str:
    """``headers``: (label, numeric?) pairs; numeric columns right-align."""
    if not rows:
        return '<div class="focus-note">none</div>'
    head = "".join(
        f'<th class="num">{_esc(h)}</th>' if num else f"<th>{_esc(h)}</th>"
        for h, num in headers
    )
    body = []
    for row in rows:
        cells = []
        for (_h, num), cell in zip(headers, row):
            cells.append(f'<td class="num">{_esc(cell)}</td>' if num else f"<td>{_esc(cell)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="wrap"><table><tr>{head}</tr>{"".join(body)}</table></div>'


def _stat(value: Any, label: str) -> str:
    return f'<div class="stat"><div class="v">{_esc(value)}</div><div class="l">{_esc(label)}</div></div>'


def _charts_html(report: dict[str, Any]) -> str:
    series = report.get("series") or {}
    players = series.get("players") or {}
    t0 = series.get("t0", 0.0)
    marker_t = report["snapshot"]["t_marker"]
    names = list(players)
    colour = {n: _PALETTE[i % len(_PALETTE)] for i, n in enumerate(names)}
    if not names:
        return '<h2>Timeline</h2><div class="focus-note">no per-second data in this window</div>'
    out = ["<h2>Timeline</h2>"]
    hp = [(n, players[n]["hp_pct"], colour[n]) for n in names]
    out.append(_chart("Health %", _svg_line_chart(hp, t0=t0, marker_t=marker_t,
                                                  y_max=100, y_label="hp %"), names))
    dt = [(n, players[n]["damage_taken"], colour[n]) for n in names]
    out.append(_chart("Damage taken per second",
                      _svg_line_chart(dt, t0=t0, marker_t=marker_t, y_label="damage"), names))
    hd = [(n, players[n]["healing_done"], colour[n]) for n in names
          if any(players[n]["healing_done"])]
    if hd:
        out.append(_chart("Healing done per second",
                          _svg_line_chart(hd, t0=t0, marker_t=marker_t, y_label="healing"),
                          [n for n, _, _ in hd]))
    mana = [(n, players[n]["mana_pct"], colour[n]) for n in names if players[n].get("mana_pct")]
    focus_name = report["snapshot"].get("focus_player")
    if report["snapshot"].get("role") == "healer" and focus_name in players and players[focus_name].get("mana_pct"):
        mana = [(focus_name, players[focus_name]["mana_pct"], colour[focus_name])]
    if mana:
        out.append(_chart("Mana %", _svg_line_chart(mana, t0=t0, marker_t=marker_t,
                                                    y_max=100, y_label="mana %"),
                          [n for n, _, _ in mana]))
    return "".join(out)


def _around_html(report: dict[str, Any]) -> str:
    around = report.get("around_marker") or {}
    out = [f"<h2>Around the marker (±{around.get('radius_s', 10):.0f}s)</h2>"]
    rows: list[list[Any]] = []
    for d in around.get("deaths") or []:
        kb = d.get("killing_blow") or {}
        rows.append([_fmt_time(d.get("t")), "DEATH", d["player"],
                     f"{kb.get('spell', '?')} from {kb.get('source', '?')}" if kb else "",
                     _fmt_num(kb.get("amount")) if kb else ""])
    for c in around.get("close_calls") or []:
        rows.append([_fmt_time(c.get("t")), f"CLOSE ({c['hp_pct']}%)", c["player"],
                     f"{c['spell']} from {c['source']}", _fmt_num(c["amount"])])
    for h in around.get("largest_hits") or []:
        hp = f" → {h['hp_pct']}%" if h.get("hp_pct") is not None else ""
        rows.append([_fmt_time(h.get("t")), f"hit{hp}", h["player"],
                     f"{h['spell']} from {h['source']}", _fmt_num(h["amount"])])
    out.append(_table([("When", False), ("What", False), ("Player", False),
                       ("Ability", False), ("Amount", True)], rows))
    return "".join(out)


def _focus_html(report: dict[str, Any]) -> str:
    f = report.get("focus") or {}
    role = f.get("role") or "general"
    out = [f"<h2>{_esc(_role_label(role))} focus"
           + (f" — {_esc(report['snapshot'].get('focus_player'))}"
              if report["snapshot"].get("focus_player") else "") + "</h2>"]
    if f.get("note"):
        out.append(f'<div class="focus-note">{_esc(f["note"])}</div>')
        return "".join(out)
    if role == "healer":
        m = f.get("mana") or {}
        out.append('<div class="grid">'
                   + _stat(_fmt_num(f["healing_done"]), "effective healing")
                   + _stat(f"{f['overheal_pct']}%" if f.get("overheal_pct") is not None else "?", "overheal")
                   + _stat(f"{f['gcd_use_pct']}%" if f.get("gcd_use_pct") is not None else "?",
                           f"gcd use ({f['casts']} casts)")
                   + _stat(f"{m['min']:.0f}%" if m.get("min") is not None else "?", "mana low point")
                   + "</div>")
        out.append('<div class="cols">')
        out.append("<div><h2>Healing by target</h2>" + _table(
            [("Target", False), ("Effective", True), ("Overheal", True)],
            [[t["name"], _fmt_num(t["total"]), _fmt_num(t["overhealing"])]
             for t in f.get("healing_by_target") or []]) + "</div>")
        out.append("<div><h2>Healing by spell</h2>" + _table(
            [("Spell", False), ("Effective", True)],
            [[s["name"], _fmt_num(s["total"])] for s in (f.get("healing_by_spell") or [])[:12]]) + "</div>")
        out.append("</div>")
        out.append("<h2>Cooldowns, externals, dispels</h2>" + _table(
            [("When", False), ("Kind", False), ("Spell", False), ("Target", False)],
            [[_fmt_time(c["t"]), "cooldown", c["spell"], c.get("target") or ""]
             for c in f.get("cooldowns_used") or []]
            + [[_fmt_time(c["t"]), "external", c["spell"], c.get("target") or ""]
               for c in f.get("externals_given") or []]
            + [[_fmt_time(d.get("t")), d.get("kind", "dispel"),
                d.get("dispelled_spell") or "?", d.get("target") or ""]
               for d in f.get("dispels") or []]))
        out.append("<h2>Who took the damage</h2>" + _table(
            [("Player", False), ("Taken", True), ("From", False)],
            [[e["name"], _fmt_num(e["damage_taken"]),
              ", ".join(s["name"] for s in e["top_spells"][:3])]
             for e in f.get("damage_taken_by_player") or []]))
        out.append("<h2>Damage by source</h2>" + _table(
            [("Source", False), ("Damage to group", True)],
            [[s["name"], _fmt_num(s["total"])] for s in f.get("damage_by_source") or []]))
    elif role == "tank":
        d = f.get("dtps") or {}
        pm = f.get("physical_vs_magic") or {}
        out.append('<div class="grid">'
                   + _stat(_fmt_num(f["damage_taken"]), "damage taken")
                   + _stat(_fmt_num(d.get("peak")), "peak / second")
                   + _stat(_fmt_num(d.get("mean")), "mean / second")
                   + _stat(f"{pm['physical_pct']}%" if pm.get("physical_pct") is not None else "?", "physical")
                   + _stat(_fmt_num(f.get("self_healing")), "self-healing")
                   + "</div>")
        out.append("<h2>Active mitigation</h2>")
        if f.get("active_mitigation"):
            out.append(_table(
                [("Ability", False), ("Casts", True), ("Uptime", True), ("Uptime %", True)],
                [[m["name"], m["casts"], f"{m['uptime_s']:.0f}s",
                  f"{m['uptime_pct']}%" if m.get("uptime_pct") is not None else "?"]
                 for m in f["active_mitigation"]]))
        else:
            out.append(f'<div class="focus-note">{_esc(f.get("mitigation_note") or "no mitigation data")}</div>')
        out.append("<h2>Cooldowns used</h2>" + _table(
            [("When", False), ("Spell", False)],
            [[_fmt_time(c["t"]), c["spell"]] for c in f.get("cooldowns_used") or []]))
        out.append('<div class="cols">')
        out.append("<div><h2>Damage by spell</h2>" + _table(
            [("Spell", False), ("Damage", True)],
            [[s["name"], _fmt_num(s["total"])] for s in (f.get("damage_by_spell") or [])[:12]]) + "</div>")
        out.append("<div><h2>Damage by source</h2>" + _table(
            [("Source", False), ("Damage", True)],
            [[s["name"], _fmt_num(s["total"])] for s in f.get("damage_by_source") or []]) + "</div>")
        out.append("</div>")
        out.append("<h2>Biggest hits</h2>" + _table(
            [("When", False), ("Amount", True), ("Ability", False), ("HP after", True)],
            [[_fmt_time(h.get("t")), _fmt_num(h["amount"]), f"{h['spell']} from {h['source']}",
              f"{h['hp_pct']}%" if h.get("hp_pct") is not None else ""]
             for h in f.get("biggest_hits") or []]))
    return "".join(out)


def render_snapshot_html(report: dict[str, Any]) -> str:
    snap = report["snapshot"]
    run = report.get("run", {})
    title = (f"Snapshot ({snap['role']}) at {_fmt_time(snap['t_marker'])} — "
             f"{run.get('zone') or 'M+'} +{run.get('keystone_level', '?')}")
    body: list[str] = []
    body.append(f"<h1>{_esc(title)}</h1>")
    body.append(
        f'<div class="sub">Window {_esc(_fmt_time(snap["t_start"]))} – '
        f'{_esc(_fmt_time(snap["t_end"]))} ({snap["before_s"]:.0f}s before / '
        f'{snap["after_s"]:.0f}s after the <span class="marker">marker at '
        f'{_esc(_fmt_time(snap["t_marker"]))}</span>)'
        + (f' · focus <b>{_esc(snap["focus_player"])}</b>' if snap.get("focus_player") else "")
        + f' · {_esc(snap.get("source"))}</div>'
    )
    body.append(_charts_html(report))
    body.append(_around_html(report))
    body.append(_focus_html(report))

    deaths = report.get("deaths") or []
    body.append("<h2>Deaths in window</h2>" + _table(
        [("When", False), ("Player", False), ("Killing blow", False), ("Amount", True), ("Defensive", False)],
        [[_fmt_time(d.get("t")), d["player"],
          f"{(d.get('killing_blow') or {}).get('spell', '?')} from {(d.get('killing_blow') or {}).get('source', '?')}"
          if d.get("killing_blow") else "",
          _fmt_num((d.get("killing_blow") or {}).get("amount")) if d.get("killing_blow") else "",
          ", ".join(u["name"] for u in d.get("defensives_used_before_death") or [])
          or ("none used" if d.get("died_without_defensive") else "")]
         for d in deaths]))
    body.append("<h2>Close calls in window</h2>" + _table(
        [("When", False), ("Player", False), ("HP", True), ("Ability", False), ("Amount", True)],
        [[_fmt_time(c.get("t")), c["player"], f"{c['hp_pct']}%",
          f"{c['spell']} from {c['source']}", _fmt_num(c["amount"])]
         for c in report.get("close_calls") or []]))
    spells = (report.get("enemy_casts") or {}).get("spells") or []
    body.append("<h2>Enemy casts</h2>" + _table(
        [("Spell", False), ("Landed", True), ("Kicked", True)],
        [[s["name"], s["got_through"], s["kicked"]] for s in spells[:15]]))
    body.append("<h2>Players (window)</h2>" + _table(
        [("Player", False), ("Spec", False), ("DPS", True), ("HPS", True), ("Taken", True),
         ("DTPS", True), ("Casts", True), ("Kicks", True), ("Deaths", True)],
        [[p["name"] or p["guid"],
          " ".join(x for x in (p.get("spec"), p.get("class")) if x) or "?",
          _fmt_num(p.get("dps")), _fmt_num(p.get("hps")), _fmt_num(p["damage_taken"]),
          _fmt_num(p.get("dtps")), p.get("casts_total", 0), p["interrupts"], p["deaths"]]
         for p in sorted(report["players"], key=lambda p: -p["damage_done"])]))
    return (_TEMPLATE.replace("__TITLE__", _esc(title))
            .replace("__STYLE__", _BRAND_STYLE + _EXTRA_STYLE)
            .replace("__BODY__", "".join(body)))
