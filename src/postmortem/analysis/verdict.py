"""The report's verdict: "where the key was lost", in plain words.

A summary over fields a finished report already carries -- the timer, the
death penalty, failed boss attempts, idle gaps between pulls, pulls the
route did not plan, and interruptible casts that landed killing blows. It
reads nothing from the log itself, so it can be built for any stored
report, old ones included (``render_html`` does exactly that when a report
predates this module).

Rules the wording follows:

* Only seconds that are actually in the report are quoted. The death
  penalty is added straight to the in-game timer, a failed boss attempt
  has a measured length, an idle gap is measured against the run's own
  typical gap, and an unplanned pull has a measured combat length. Things
  that cost time but cannot be measured from a report (run-backs after a
  death, slow kills) get no number at all.
* Plain and specific, about the run rather than about a person: "Deaths
  cost 45s", never "X died too much".
* Every input can be hostile. On the public site a report comes from an
  anonymous upload, so a field the schema calls a number may be text, a
  bool, NaN or missing; each read below degrades to "unknown" instead of
  raising or printing "nan".
"""

from __future__ import annotations

import math
from typing import Any, Optional

VERDICT_VERSION = 1

#: At most this many "what cost time" lines, ranked by seconds.
MAX_LINES = 3
#: At most this many non-time observations.
MAX_NOTES = 2
#: An idle gap counts as long once it is at least this many seconds AND
#: at least ``_GAP_FACTOR`` times the run's typical (median) gap.
_GAP_FLOOR_S = 30.0
_GAP_FACTOR = 3.0
#: "short of +N" is mentioned when the next chest was this close.
_NEAR_NEXT_CHEST_S = 120.0


# ---------------------------------------------------------------------------
# defensive readers


def _num(value: Any) -> Optional[float]:
    """A finite real number, or None. Bools and strings are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _int(value: Any) -> Optional[int]:
    v = _num(value)
    return int(v) if v is not None and v == int(v) else None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _str(value: Any) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def mmss(seconds: float) -> str:
    """0:14, 1:12, 1:02:03 -- the same shape the page uses."""
    s = int(round(abs(seconds)))
    m, sec = divmod(s, 60)
    if m >= 60:
        return f"{m // 60}:{m % 60:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def duration_words(seconds: float) -> str:
    """Short durations as "14s", longer ones as "1:12"."""
    s = int(round(abs(seconds)))
    return f"{s}s" if s < 60 else mmss(s)



def _plural(n: int, word: str, plural: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


# ---------------------------------------------------------------------------
# headline


def _headline(report: dict) -> dict:
    run = _dict(report.get("run"))
    timer = _dict(report.get("timer"))
    forces = _dict(report.get("forces"))
    completed = run.get("completed") is True
    out: dict[str, Any] = {"outcome": "abandoned", "headline": "", "chests": None,
                           "margin_s": None, "context": None}

    if not completed:
        if run.get("truncated") is True:
            out["outcome"] = "partial"
            out["headline"] = "Partial analysis"
            out["context"] = "The log hit the size limit before the key ended."
            return out
        pct = _num(forces.get("pct"))
        wall = _num(run.get("wall_duration_s"))
        if pct is not None:
            out["headline"] = f"Abandoned at {pct:g}% forces"
        elif wall is not None and wall > 0:
            out["headline"] = f"Abandoned after {mmss(wall)}"
        else:
            out["headline"] = "Abandoned"
        return out

    margin_ms = _num(timer.get("margin_ms"))
    par_ms = _num(timer.get("par_ms"))
    if margin_ms is None:
        timed = run.get("timed")
        dur_ms = _num(run.get("duration_ms"))
        if timed is True:
            out["outcome"], out["headline"] = "timed", "Timed"
        elif timed is False:
            out["outcome"], out["headline"] = "over", "Over the timer"
        else:
            out["outcome"] = "completed"
            out["headline"] = f"Completed in {mmss(dur_ms / 1000)}" if dur_ms and dur_ms > 0 else "Completed"
        if timed is True:
            out["chests"] = 1
        elif timed is False:
            out["chests"] = 0
        return out

    margin_s = margin_ms / 1000
    out["margin_s"] = round(margin_s, 1)
    if margin_s < 0:
        out["outcome"] = "over"
        out["chests"] = 0
        out["headline"] = f"Over by {duration_words(margin_s)}"
        return out

    out["outcome"] = "timed"
    threshold = _int(timer.get("threshold"))
    chests = min(3, max(1, threshold)) if threshold is not None else 1
    out["chests"] = chests
    duration_ms = par_ms - margin_ms if par_ms is not None else None
    limit_ms = {1: par_ms, 2: _num(timer.get("threshold_2_ms")),
                3: _num(timer.get("threshold_3_ms"))}.get(chests)
    spare_s = margin_s
    if duration_ms is not None and limit_ms is not None and limit_ms >= duration_ms:
        spare_s = (limit_ms - duration_ms) / 1000
    out["headline"] = f"Timed +{chests} with {duration_words(spare_s)} to spare"
    if duration_ms is not None and chests < 3:
        next_ms = _num(timer.get(f"threshold_{chests + 1}_ms"))
        if next_ms is not None and duration_ms > next_ms:
            short_s = (duration_ms - next_ms) / 1000
            if short_s <= _NEAR_NEXT_CHEST_S:
                out["context"] = f"{duration_words(short_s)} short of a +{chests + 1}."
    return out


# ---------------------------------------------------------------------------
# time lines


def _death_line(report: dict) -> Optional[dict]:
    cost = _dict(report.get("death_cost"))
    deaths = [d for d in _list(report.get("deaths")) if isinstance(d, dict)]
    n = _int(cost.get("deaths"))
    if n is None:
        n = len(deaths)
    if not n or n < 0:
        return None
    per = _num(cost.get("per_death_s"))
    total = _num(cost.get("total_s"))
    if total is None and per is not None:
        total = per * n
    if total is None or total <= 0:
        # The penalty is unknown: say how many, quote no seconds.
        return {"kind": "deaths", "seconds": None,
                "text": f"{_plural(n, 'death')} (timer penalty unknown)"}

    text = f"Deaths cost {duration_words(total)}"
    if per is not None and per > 0:
        text += f" ({n} × {duration_words(per)} penalty)"
    # The pull that took the most, when the deaths carry pulls.
    by_pull: dict[int, int] = {}
    for d in deaths:
        p = _int(d.get("pull"))
        if p is not None:
            by_pull[p] = by_pull.get(p, 0) + 1
    worst: Optional[int] = None
    if by_pull and per is not None and per > 0 and len(deaths) == n:
        worst = max(sorted(by_pull), key=lambda p: by_pull[p])
        if len(by_pull) == 1:
            text += f", all on pull {worst}"
        elif by_pull[worst] > 1:
            text += f"; {duration_words(by_pull[worst] * per)} of it on pull {worst}"
    return {"kind": "deaths", "seconds": round(total, 1), "text": text,
            "pull": worst}


def _wipe_line(report: dict) -> Optional[dict]:
    wipes = []
    for e in _list(report.get("encounters")):
        if not isinstance(e, dict) or e.get("kill") is not False:
            continue
        secs = _num(e.get("duration_s"))
        if secs is None or secs <= 0:
            continue
        wipes.append((secs, _str(e.get("name")) or "a boss"))
    if not wipes:
        return None
    total = sum(s for s, _ in wipes)
    names = []
    for _, name in wipes:
        if name not in names:
            names.append(name)
    if len(wipes) == 1:
        text = f"A wipe on {names[0]} took {duration_words(total)}"
    else:
        text = (f"{len(wipes)} wipes on {', '.join(names)} took "
                f"{duration_words(total)}")
    return {"kind": "wipe", "seconds": round(total, 1), "text": text}


def _gap_stats(report: dict) -> tuple[list[tuple[float, Any, Any]], Optional[float]]:
    gaps = []
    for w in _list(_dict(report.get("downtime")).get("windows")):
        if not isinstance(w, dict):
            continue
        secs = _num(w.get("seconds"))
        if secs is None or secs < 0:
            continue
        gaps.append((secs, _int(w.get("after_pull")), _int(w.get("before_pull"))))
    if len(gaps) < 3:
        return gaps, None
    ordered = sorted(s for s, _, _ in gaps)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return gaps, median


def _downtime_line(report: dict) -> Optional[dict]:
    gaps, typical = _gap_stats(report)
    if typical is None:
        return None
    long_gaps = [g for g in gaps if g[0] >= max(_GAP_FLOOR_S, _GAP_FACTOR * typical)]
    if not long_gaps:
        return None
    excess = sum(s - typical for s, _, _ in long_gaps)
    longest = max(long_gaps, key=lambda g: g[0])
    where = ""
    if longest[1] is not None and longest[2] is not None:
        where = f" between pulls {longest[1]} and {longest[2]}"
    if len(long_gaps) == 1:
        text = (f"A {duration_words(longest[0])} idle gap{where} "
                f"— the run's typical gap was {duration_words(typical)}")
    else:
        text = (f"{len(long_gaps)} long idle gaps: {duration_words(excess)} beyond the "
                f"typical {duration_words(typical)} gap (longest {duration_words(longest[0])}{where})")
    return {"kind": "downtime", "seconds": round(excess, 1), "text": text}


def _route_line(report: dict) -> Optional[dict]:
    comp = _dict(report.get("comparison"))
    if comp.get("error") or not _list(comp.get("pulls")):
        return None
    pulls = {}
    for p in _list(report.get("pulls")):
        if isinstance(p, dict) and _int(p.get("pull")) is not None:
            pulls[_int(p.get("pull"))] = p
    extra: list[tuple[int, float]] = []
    for m in _list(comp.get("pulls")):
        if not isinstance(m, dict) or m.get("primary_plan_pull") is not None:
            continue
        n = _int(m.get("actual_pull"))
        pull = pulls.get(n) if n is not None else None
        if pull is None or _str(pull.get("boss")):
            continue
        secs = _num(pull.get("duration_s"))
        if secs is None or secs <= 0:
            continue
        extra.append((n, secs))
    if not extra:
        return None
    total = sum(s for _, s in extra)
    nums = ", ".join(str(n) for n, _ in extra)
    if len(extra) == 1:
        text = f"Pull {nums} wasn't on the planned route — {duration_words(total)} of combat"
    else:
        text = (f"{len(extra)} pulls weren't on the planned route (pulls {nums}) "
                f"— {duration_words(total)} of combat")
    return {"kind": "route", "seconds": round(total, 1), "text": text}


# ---------------------------------------------------------------------------
# notes (no seconds)


def _spell_notes(report: dict) -> list[dict]:
    """Interruptible enemy casts that got through and landed killing blows."""
    casts = {}
    for s in _list(_dict(report.get("enemy_casts")).get("spells")):
        if not isinstance(s, dict):
            continue
        through = _int(s.get("got_through"))
        kicked = _int(s.get("kicked")) or 0
        if not through or through <= 0:
            continue
        if s.get("interruptible") is not True and kicked <= 0:
            continue
        sid = _int(s.get("spell_id"))
        name = _str(s.get("name"))
        key = sid if sid is not None else name
        if key is None:
            continue
        casts[key] = (name or "?", sid, through, kicked)
    kills: dict[Any, int] = {}
    for d in _list(report.get("deaths")):
        kb = _dict(_dict(d).get("killing_blow"))
        sid = _int(kb.get("spell_id"))
        key = sid if sid in casts else _str(kb.get("spell"))
        if key in casts:
            kills[key] = kills.get(key, 0) + 1
    notes = []
    for key, n_deaths in sorted(kills.items(), key=lambda kv: -kv[1]):
        name, sid, through, kicked = casts[key]
        total = through + max(kicked, 0)
        blows = "the killing blow" if n_deaths == 1 else f"{n_deaths} killing blows"
        text = f"{name} got through {through} of {total} casts and landed {blows}"
        notes.append({"kind": "spell", "spell": name, "spell_id": sid,
                      "got_through": through, "deaths": n_deaths, "text": text})
    return notes


def _downtime_note(report: dict, has_line: bool) -> Optional[dict]:
    if has_line:
        return None
    gaps, typical = _gap_stats(report)
    if typical is None:
        return None
    longest = max(s for s, _, _ in gaps)
    return {"kind": "downtime",
            "text": f"Downtime between pulls was steady (longest gap {duration_words(longest)})"}


# ---------------------------------------------------------------------------


def build_verdict(report: dict) -> dict:
    """The verdict for a finished report dict. Never raises on bad input."""
    report = report if isinstance(report, dict) else {}
    head = _headline(report)

    candidates = [c for c in (_death_line(report), _wipe_line(report),
                              _downtime_line(report), _route_line(report)) if c]
    # Seconds first, largest first; unquantified lines after.
    candidates.sort(key=lambda c: (c["seconds"] is None, -(c["seconds"] or 0)))
    lines = candidates[:MAX_LINES]

    notes = _spell_notes(report)
    dn = _downtime_note(report, any(c["kind"] == "downtime" for c in candidates))
    if dn:
        notes.append(dn)
    notes = notes[:MAX_NOTES]

    # Over the timer: say how much of the overage the death penalty
    # explains -- the one counterfactual the report can back with numbers,
    # because the penalty is added straight to the timer.
    if head["outcome"] == "over" and head["margin_s"] is not None and not head["context"]:
        over = -head["margin_s"]
        death = next((c for c in candidates if c["kind"] == "deaths" and c["seconds"]), None)
        if death and death["seconds"] >= over:
            head["context"] = (f"The death penalty ({duration_words(death['seconds'])}) "
                               f"is more than the {duration_words(over)} it went over.")
        elif death:
            head["context"] = (f"The death penalty explains {duration_words(death['seconds'])} "
                               f"of the {duration_words(over)}.")

    return {
        "version": VERDICT_VERSION,
        "outcome": head["outcome"],
        "headline": head["headline"],
        "context": head["context"],
        "chests": head["chests"],
        "margin_s": head["margin_s"],
        "lines": [{k: v for k, v in c.items() if k != "pull"} for c in lines],
        "notes": notes,
    }
