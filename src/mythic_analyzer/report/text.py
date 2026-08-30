"""Plain-text post-mortem report for the terminal."""

from __future__ import annotations

from typing import Any


def _fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_num(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    if abs(n) >= 1e9:
        return f"{n / 1e9:.2f}b"
    if abs(n) >= 1e6:
        return f"{n / 1e6:.2f}m"
    if abs(n) >= 1e3:
        return f"{n / 1e3:.1f}k"
    return f"{n:.0f}"


def _npcs(entries: list[dict[str, Any]]) -> str:
    parts = []
    for e in entries:
        name = e.get("name") or f"npc:{e.get('npc_id')}"
        parts.append(f"{e['n']}x {name}")
    return ", ".join(parts)


def render_text(report: dict[str, Any]) -> str:
    out: list[str] = []
    run = report["run"]
    add = out.append

    title = f"{run.get('zone') or report['dungeon']['name']} +{run.get('keystone_level', '?')}"
    add("=" * 72)
    add(f"MYTHIC+ POST-MORTEM — {title}")
    add("=" * 72)
    result = "IN PROGRESS / ABANDONED"
    if run.get("completed"):
        result = "TIMED" if run.get("timed") else "COMPLETED (over timer)"
    timer = run.get("duration_ms")
    add(f"Result: {result}"
        + (f"  |  In-game timer: {_fmt_time(timer / 1000)}" if timer else "")
        + f"  |  Wall clock: {_fmt_time(run.get('wall_duration_s'))}")
    if run.get("affixes"):
        add(f"Affixes: {run['affixes']}")

    forces = report.get("forces") or {}
    if forces.get("required"):
        add(f"Forces: {forces['killed']:.0f} / {forces['required']:.0f}"
            f" ({forces.get('pct')}%)")

    downtime = report.get("downtime") or {}
    add(f"Combat time: {_fmt_time(downtime.get('combat_s'))}"
        f"  |  Downtime between pulls: {_fmt_time(downtime.get('total_s'))}")

    # --- players ---
    add("")
    add("-- PLAYERS " + "-" * 61)
    add(f"{'Player':<24}{'Spec':<22}{'DPS':>8}{'HPS':>8}{'Dmg':>8}"
        f"{'Taken':>8}{'Int':>5}{'Dis':>5}{'Dth':>5}")
    for p in sorted(report["players"], key=lambda p: -p["damage_done"]):
        spec = " ".join(x for x in (p.get("spec"), p.get("class")) if x) or "?"
        add(f"{(p['name'] or p['guid'])[:23]:<24}{spec[:21]:<22}"
            f"{_fmt_num(p.get('dps')):>8}{_fmt_num(p.get('hps')):>8}"
            f"{_fmt_num(p['damage_done']):>8}{_fmt_num(p['damage_taken']):>8}"
            f"{p['interrupts']:>5}{p['dispels']:>5}{p['deaths']:>5}")

    # --- route comparison ---
    comparison = report.get("comparison")
    if comparison and "error" not in comparison:
        add("")
        add("-- ROUTE vs ACTUAL " + "-" * 53)
        if comparison.get("adherence_pct") is not None:
            add(f"Route adherence: {comparison['adherence_pct']}%"
                f"  (planned forces {comparison['plan_forces']:.0f},"
                f" killed {comparison['actual_forces']:.0f})")
        for m in comparison["pulls"]:
            plan = m["primary_plan_pull"]
            label = f"Pull {m['actual_pull']:>3}"
            plan_label = f"plan #{plan}" if plan is not None else "not in route"
            flags = []
            if m["pulled_early"]:
                flags.append("EARLY: " + _npcs(m["pulled_early"]))
            if m["picked_up_late"]:
                flags.append("LATE: " + _npcs(m["picked_up_late"]))
            if m["off_route"]:
                flags.append("OFF-ROUTE: " + _npcs(m["off_route"]))
            marker = "  " if not flags else "! "
            add(f"{marker}{label} -> {plan_label}"
                + ("  |  " + "  |  ".join(flags) if flags else ""))
        if comparison.get("missed"):
            add("  Never engaged (planned but not pulled):")
            for plan_idx, entries in comparison["missed"].items():
                add(f"    plan #{plan_idx}: {_npcs(entries)}")
    elif comparison and "error" in comparison:
        add("")
        add(f"Route comparison unavailable: {comparison['error']}")

    # --- pulls ---
    add("")
    add("-- PULLS " + "-" * 63)
    for p in report["pulls"]:
        boss = f"  [BOSS: {p['boss']}]" if p.get("boss") else ""
        deaths = f"  deaths:{p['player_deaths']}" if p.get("player_deaths") else ""
        forces_s = f"  +{p['forces']:.0f} forces" if p.get("forces") else ""
        add(f"Pull {p['pull']:>3}  {_fmt_time(p['t_start'])}-{_fmt_time(p['t_end'])}"
            f" ({p['duration_s']:.0f}s)  {p['mob_count']} mobs"
            f"{forces_s}{deaths}{boss}")

    # --- deaths ---
    deaths = report.get("deaths") or []
    if deaths:
        add("")
        add("-- DEATHS " + "-" * 62)
        for d in deaths:
            kb = d.get("killing_blow") or {}
            add(f"{_fmt_time(d.get('t'))}  {d['player']}"
                f"  (pull {d.get('pull', '?')})"
                + (f"  killed by {kb.get('spell')} from {kb.get('source')}"
                   f" for {_fmt_num(kb.get('amount'))}" if kb else ""))

    # --- kick value ---
    kicks = report.get("kick_value") or {}
    if kicks.get("by_player"):
        add("")
        add("-- KICKS (estimated damage/healing prevented) " + "-" * 26)
        for entry in kicks["by_player"]:
            parts = [f"{entry['kicks']} kicks"]
            if entry["estimated_prevented_damage"]:
                parts.append(f"~{_fmt_num(entry['estimated_prevented_damage'])} dmg prevented")
            if entry["estimated_prevented_healing"]:
                parts.append(f"~{_fmt_num(entry['estimated_prevented_healing'])} healing prevented")
            add(f"  {entry['name']:<24}{'  |  '.join(parts)}")
        for i in report.get("interrupts") or []:
            est = i.get("estimated_prevented_damage")
            est_h = i.get("estimated_prevented_healing")
            what = []
            if est:
                what.append(f"~{_fmt_num(est)} dmg")
            if est_h:
                what.append(f"~{_fmt_num(est_h)} healing")
            basis = (f" (avg of {i['observed_casts']} landed casts)"
                     if i.get("observed_casts") else " (never landed — no estimate)")
            add(f"    {_fmt_time(i.get('t'))}  {i['player']} kicked "
                f"{i.get('interrupted_spell') or '?'} on {i['target']}"
                + (f": {' + '.join(what)} prevented{basis}" if what else basis))

    # --- big moments ---
    lust = report.get("lust") or []
    brez = report.get("brez") or []
    if lust or brez:
        add("")
        add("-- COOLDOWNS " + "-" * 59)
        for l in lust:
            add(f"{_fmt_time(l.get('t'))}  {l['spell']}"
                + (f" ({l['source']})" if l.get("source") else "")
                + (f"  pull {l['pull']}" if l.get("pull") else ""))
        for b in brez:
            add(f"{_fmt_time(b.get('t'))}  {b['player']} battle-rezzed"
                f" {b.get('target') or '?'} ({b['spell']})")

    # --- downtime ---
    windows = (report.get("downtime") or {}).get("windows") or []
    slow = [w for w in windows if w["seconds"] >= 10]
    if slow:
        add("")
        add("-- LONGEST DOWNTIME " + "-" * 52)
        for w in sorted(slow, key=lambda w: -w["seconds"])[:10]:
            add(f"{_fmt_time(w.get('t'))}  {w['seconds']:.0f}s idle"
                f" between pull {w['after_pull']} and {w['before_pull']}")

    add("=" * 72)
    return "\n".join(out)
