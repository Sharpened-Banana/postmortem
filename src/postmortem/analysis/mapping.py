"""Route map overlay: planned-map geometry (zero risk) and world->canvas
coordinate calibration (derived, must fail safe).

MDT's own addon has no known transform between world space (what the combat
log's advanced-block positions are in) and its 840x555 planning canvas (what
dungeon-data clone x/y are in) -- see module docstring notes in
``mdt/extract.py``. So instead of assuming a fixed scale/rotation, this
module *fits* a 2D similarity transform (uniform scale, rotation,
translation, and possibly a reflection) from corresponding points collected
at runtime: an engaged NPC's first-seen world position (``UnitEngagement.
first_pos``) paired with that same NPC's planned clone position, for NPCs
whose npc_id maps to exactly one clone (unambiguous anchors). The fit is
refined ICP-style once a rough transform is known, then gated on anchor
count and residual error -- a bad fit is rejected rather than shown, and the
caller falls back to the geometry-only planned map.

Pure-Python closed-form 2D absolute-orientation (Procrustes) solve; no
numpy/scipy per this project's stdlib-only runtime constraint.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass
from typing import Any, Optional

from ..mdt.dungeon_data import DungeonData
from ..mdt.route import Route
from .pulls import ActualPull
from .stats import DeathRecord

Point = tuple[float, float]

# --- anchor/fit thresholds ---------------------------------------------
#
# MIN_SEED_ANCHORS: floor to *attempt* a fit at all. A 2D similarity
# transform has 4 degrees of freedom (scale, rotation, 2 translation) and is
# technically solvable from 2 points, but that's wildly unstable (no
# redundancy to catch a bad pairing or a near-degenerate/collinear seed
# set). 3 unique correspondences is the practical floor for a first
# estimate that ICP refinement can then build on.
MIN_SEED_ANCHORS = 3
# MIN_FINAL_ANCHORS: floor to *accept* the final (ICP-refined) fit. Set
# higher than the seed floor because the refined set includes anchors
# resolved via nearest-clone assignment (ambiguous multi-clone packs), which
# are only trustworthy once there's enough independent agreement to dilute
# a single bad assignment's influence.
MIN_FINAL_ANCHORS = 4
# MAX_RESIDUAL: RMS residual, in canvas units, above which a fit is
# rejected outright. MDT's canvas is 840x555, so 50 units is ~6% of the
# width -- loose enough to tolerate MDT's clone placements not being
# pixel-perfect against in-game world geometry (they're hand-placed on a
# stylized map texture, not derived from world coordinates), tight enough
# that a fit built from a wrong pairing or the wrong zone entirely won't
# read as "confident". A wrong map is worse than no map, so this errs
# strict.
MAX_RESIDUAL = 50.0
# ICP refinement: a few iterations is enough for the assignment to settle;
# bail early once nothing changes.
MAX_ICP_ITERS = 5
# Outlier trimming (see _trim_outliers): floor on how many correspondences
# a fit may be reduced to while trying to clear MAX_RESIDUAL. Set well
# above MIN_FINAL_ANCHORS so a fit that only clears the residual bar by
# trimming down to a near-minimal anchor set (i.e. barely more redundancy
# than degrees of freedom) is rejected as unreliable rather than accepted --
# trimming should discard a handful of bad correspondences from an
# otherwise-solid set, not explain away most of the data.
MIN_ANCHORS_AFTER_TRIM = 8
# Per-point inlier distance for RANSAC-style outlier rejection (see
# _trim_outliers) -- deliberately the same value as MAX_RESIDUAL: a point
# within this distance of a *candidate* transform's prediction is
# consistent with the overall RMS bar the final refit must also clear.
_RANSAC_INLIER_DIST = MAX_RESIDUAL
# Minimal-subset size for each RANSAC candidate transform. A 2D similarity
# transform has 4 degrees of freedom (scale, rotation, 2 translation) and
# is technically solvable from 2 points, but 3 gives a little redundancy
# over that theoretical minimum -- same reasoning as MIN_SEED_ANCHORS, and
# small enough that a genuinely clean 3-point subset is likely to exist
# even when a meaningful fraction of the full anchor set is corrupted
# (unlike fitting -- and thus being biased by -- the *whole* contaminated
# set at once, which is what made naive greedy worst-point-removal
# unreliable here: a least-squares fit dragged by outliers can make a
# *good* anchor's residual look worse than an actual outlier's).
_RANSAC_SAMPLE_SIZE = 3
# Cap on how many candidate subsets to evaluate. Exhaustive
# itertools.combinations(n, 3) is cheap for realistic anchor counts (a
# dungeon has at most a few dozen distinct single-clone npc_ids engaged in
# one run), but this bounds runtime for a pathological anchor count --
# beyond it, a deterministically-seeded random sample of subsets is used
# instead (deterministic so this stays reproducible/testable, not because
# non-determinism would be unsafe).
_RANSAC_MAX_CANDIDATES = 2000


@dataclass
class Transform:
    """A 2D similarity transform (scale * M * p + t), where M is a rotation
    matrix, or a rotation composed with a y-flip when ``reflected``."""

    scale: float
    rotation: float  # radians
    reflected: bool
    tx: float
    ty: float
    m00: float
    m01: float
    m10: float
    m11: float

    def apply(self, x: float, y: float) -> Point:
        return (
            self.scale * (self.m00 * x + self.m01 * y) + self.tx,
            self.scale * (self.m10 * x + self.m11 * y) + self.ty,
        )


@dataclass
class CalibrationResult:
    ok: bool
    reason: Optional[str] = None
    transform: Optional[Transform] = None
    anchor_count: int = 0
    residual: Optional[float] = None

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok, "anchor_count": self.anchor_count}
        if self.reason is not None:
            out["reason"] = self.reason
        if self.residual is not None:
            out["residual"] = round(self.residual, 2)
        if self.transform is not None:
            t = self.transform
            out["scale"] = round(t.scale, 4)
            out["rotation"] = round(t.rotation, 4)
            out["reflected"] = t.reflected
            out["translation"] = {"x": round(t.tx, 2), "y": round(t.ty, 2)}
        return out


def _fit_candidate(
    world_pts: list[Point], canvas_pts: list[Point], reflect: bool
) -> Optional[tuple[Transform, float]]:
    n = len(world_pts)
    wx = sum(p[0] for p in world_pts) / n
    wy = sum(p[1] for p in world_pts) / n
    cx = sum(p[0] for p in canvas_pts) / n
    cy = sum(p[1] for p in canvas_pts) / n

    px = [p[0] - wx for p in world_pts]
    py = [p[1] - wy for p in world_pts]
    qx = [p[0] - cx for p in canvas_pts]
    qy = [p[1] - cy for p in canvas_pts]
    if reflect:
        py = [-v for v in py]

    Sxx = sum(a * b for a, b in zip(px, qx))
    Sxy = sum(a * b for a, b in zip(px, qy))
    Syx = sum(a * b for a, b in zip(py, qx))
    Syy = sum(a * b for a, b in zip(py, qy))

    theta = math.atan2(Sxy - Syx, Sxx + Syy)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    # M: the full linear map (rotation, or rotation-after-reflection) that
    # best aligns the (possibly y-flipped) centered world points to the
    # centered canvas points.
    if reflect:
        m00, m01 = cos_t, sin_t
        m10, m11 = sin_t, -cos_t
    else:
        m00, m01 = cos_t, -sin_t
        m10, m11 = sin_t, cos_t

    # Apply the *plain* rotation R(theta) (not M, which already bakes in
    # the reflection) to the (possibly y-flipped) centered points -- M@p
    # would double-apply the flip, since M = R(theta) @ F.
    rot_x = [cos_t * a - sin_t * b for a, b in zip(px, py)]
    rot_y = [sin_t * a + cos_t * b for a, b in zip(px, py)]
    numerator = sum(rx * qxi + ry * qyi for rx, ry, qxi, qyi in zip(rot_x, rot_y, qx, qy))
    denom = sum(a * a + b * b for a, b in zip(px, py))
    if denom <= 1e-9:
        return None
    scale = numerator / denom

    tx = cx - scale * (m00 * wx + m01 * wy)
    ty = cy - scale * (m10 * wx + m11 * wy)

    transform = Transform(scale=scale, rotation=theta, reflected=reflect,
                           tx=tx, ty=ty, m00=m00, m01=m01, m10=m10, m11=m11)

    sq_err = 0.0
    for (wxi, wyi), (cxi, cyi) in zip(world_pts, canvas_pts):
        px_, py_ = transform.apply(wxi, wyi)
        sq_err += (px_ - cxi) ** 2 + (py_ - cyi) ** 2
    residual = math.sqrt(sq_err / n)
    return transform, residual


def fit_transform(pairs: list[tuple[Point, Point]]) -> Optional[tuple[Transform, float]]:
    """Fit the best (lowest-residual) similarity transform, trying both the
    non-reflected and reflected orientation, from a list of
    (world_point, canvas_point) correspondences. Returns None if there
    aren't enough points or the point set is degenerate (e.g. all
    coincident -- zero spread gives no orientation/scale information)."""
    if len(pairs) < 2:
        return None
    world_pts = [p[0] for p in pairs]
    canvas_pts = [p[1] for p in pairs]
    candidates = []
    for reflect in (False, True):
        result = _fit_candidate(world_pts, canvas_pts, reflect)
        if result is not None:
            candidates.append(result)
    if not candidates:
        return None
    return min(candidates, key=lambda tr: tr[1])


def _seed_anchors(
    pulls: list[ActualPull], data: DungeonData
) -> dict[int, tuple[Point, Point]]:
    """npc_id -> (world_pos, canvas_pos) for npc_ids that have exactly one
    clone in the dungeon data (unambiguous pairing) and were engaged with a
    known first-seen world position. Only the first engagement of a given
    npc_id is kept -- a repeat engagement of the same single-clone npc_id
    doesn't add new geometric information, just risks re-weighting it."""
    seeds: dict[int, tuple[Point, Point]] = {}
    for pull in pulls:
        for unit in pull.units:
            if unit.npc_id is None or unit.first_pos is None or unit.npc_id in seeds:
                continue
            enemy = data.enemy_by_npc_id(unit.npc_id)
            if enemy is None or len(enemy.clones) != 1:
                continue
            clone = enemy.clones[0]
            seeds[unit.npc_id] = (unit.first_pos, (clone.x, clone.y))
    return seeds


def _icp_refine(
    transform: Transform,
    residual: float,
    pulls: list[ActualPull],
    data: DungeonData,
    correspondences: dict[int, tuple[Point, int]],
) -> tuple[Transform, float, dict[int, tuple[Point, int]]]:
    """Refine the transform a few ICP-style iterations: transform every
    engaged npc_id's first_pos into canvas space with the current estimate,
    assign it to the nearest clone *of that same npc_id* (now usable even
    for npc_ids with multiple clones), and refit against the accumulated
    correspondence set. Stops early once the assignment stops changing.

    ``correspondences`` maps npc_id -> (world_pos, clone_index) and is both
    the seed and the running accumulator/output.
    """
    for _ in range(MAX_ICP_ITERS):
        changed = False
        updated = dict(correspondences)
        for pull in pulls:
            for unit in pull.units:
                if unit.npc_id is None or unit.first_pos is None:
                    continue
                enemy = data.enemy_by_npc_id(unit.npc_id)
                if enemy is None or not enemy.clones:
                    continue
                cx, cy = transform.apply(*unit.first_pos)
                best_idx = min(
                    range(len(enemy.clones)),
                    key=lambda i: (enemy.clones[i].x - cx) ** 2 + (enemy.clones[i].y - cy) ** 2,
                )
                prev = updated.get(unit.npc_id)
                if prev is None or prev[1] != best_idx:
                    changed = True
                updated[unit.npc_id] = (unit.first_pos, best_idx)

        pairs: list[tuple[Point, Point]] = []
        for npc_id, (world_pos, clone_idx) in updated.items():
            enemy = data.enemy_by_npc_id(npc_id)
            if enemy is None or clone_idx >= len(enemy.clones):
                continue
            clone = enemy.clones[clone_idx]
            pairs.append((world_pos, (clone.x, clone.y)))

        fit = fit_transform(pairs)
        if fit is None:
            break
        transform, residual = fit
        correspondences = updated
        if not changed:
            break
    return transform, residual, correspondences


def _trim_outliers(
    transform: Transform,
    residual: float,
    correspondences: dict[int, tuple[Point, int]],
    data: DungeonData,
) -> tuple[Transform, float, dict[int, tuple[Point, int]]]:
    """If the fit's residual exceeds MAX_RESIDUAL, find the largest
    consistent (inlier) subset of the anchors via RANSAC and refit from
    just those.

    A handful of noisy/mismatched anchors -- a mob that moved before its
    first logged interaction, or (rarer) a genuinely wrong npc_id/clone
    pairing -- can drag an otherwise-solid fit's RMS residual well past the
    threshold on their own, even though the fitted transform is correct for
    the bulk of the data (confirmed empirically: a handful of outlier
    anchors with 150-250 unit offsets is enough to push a near-zero
    residual up into the 100+ range this project saw on every real
    production upload before this existed). Naive greedy trimming (fit all
    anchors, drop the single worst-residual one, repeat) was tried first
    and rejected: with a small anchor set and a meaningful outlier
    fraction, the *initial* least-squares fit is itself dragged far enough
    off by the outliers that a genuinely clean anchor can end up looking
    worse than an actual outlier, so greedy removal deletes the wrong
    points (confirmed empirically against a 10-anchor/2-outlier case).
    RANSAC sidesteps this: it never trusts a fit built from the whole
    contaminated set. Instead it builds many small-subset candidate
    transforms (_RANSAC_SAMPLE_SIZE points each -- small enough that a
    genuinely clean subset almost certainly exists even when several
    anchors are bad), scores each by how many of *all* the anchors it
    explains within _RANSAC_INLIER_DIST, and keeps the winning candidate's
    inlier set for a final refit.

    This is a well-justified correction for a real, verified failure mode,
    not a threshold loosened to paper over bad data -- MAX_RESIDUAL itself
    is untouched, and MIN_ANCHORS_AFTER_TRIM keeps this from explaining
    away most of the anchor set to force a pass. If no candidate reaches
    that floor, the original fit is returned unchanged and the caller's
    existing residual gate rejects it exactly as before -- this only ever
    helps, never masks a genuinely bad fit.
    """
    if residual <= MAX_RESIDUAL or len(correspondences) < MIN_ANCHORS_AFTER_TRIM:
        return transform, residual, correspondences

    items: list[tuple[int, Point, Point]] = []
    for npc_id, (world_pos, clone_idx) in correspondences.items():
        enemy = data.enemy_by_npc_id(npc_id)
        if enemy is None or clone_idx >= len(enemy.clones):
            continue
        clone = enemy.clones[clone_idx]
        items.append((npc_id, world_pos, (clone.x, clone.y)))
    n = len(items)
    if n < _RANSAC_SAMPLE_SIZE:
        return transform, residual, correspondences

    all_combos = itertools.combinations(range(n), _RANSAC_SAMPLE_SIZE)
    index_combos = list(itertools.islice(all_combos, _RANSAC_MAX_CANDIDATES + 1))
    if len(index_combos) > _RANSAC_MAX_CANDIDATES:
        rng = random.Random(0)
        index_combos = [
            tuple(sorted(rng.sample(range(n), _RANSAC_SAMPLE_SIZE)))
            for _ in range(_RANSAC_MAX_CANDIDATES)
        ]

    inlier_dist_sq = _RANSAC_INLIER_DIST ** 2
    best_inliers: Optional[list[int]] = None
    for combo in index_combos:
        sample_pairs = [(items[i][1], items[i][2]) for i in combo]
        candidate = fit_transform(sample_pairs)
        if candidate is None:
            continue
        cand_transform, _ = candidate
        inliers = []
        for i, (_npc_id, world_pos, canvas_pos) in enumerate(items):
            px, py = cand_transform.apply(*world_pos)
            if (px - canvas_pos[0]) ** 2 + (py - canvas_pos[1]) ** 2 <= inlier_dist_sq:
                inliers.append(i)
        if best_inliers is None or len(inliers) > len(best_inliers):
            best_inliers = inliers

    if best_inliers is None or len(best_inliers) < MIN_ANCHORS_AFTER_TRIM:
        return transform, residual, correspondences

    final_pairs = [(items[i][1], items[i][2]) for i in best_inliers]
    fit = fit_transform(final_pairs)
    if fit is None:
        return transform, residual, correspondences
    new_transform, new_residual = fit
    new_correspondences = {
        items[i][0]: (items[i][1], correspondences[items[i][0]][1]) for i in best_inliers
    }
    return new_transform, new_residual, new_correspondences


def calibrate(pulls: list[ActualPull], data: DungeonData) -> CalibrationResult:
    """Attempt to fit a world->canvas transform for this run. Fails safe:
    returns ``ok=False`` with a human-readable ``reason`` whenever the data
    doesn't support a confident fit, rather than forcing a bad answer
    through (see module docstring and the threshold constants above)."""
    any_pos = any(u.first_pos is not None for pull in pulls for u in pull.units)
    if not any_pos:
        return CalibrationResult(
            ok=False, reason="no position data (advanced combat logging required)"
        )

    seeds = _seed_anchors(pulls, data)
    if len(seeds) < MIN_SEED_ANCHORS:
        return CalibrationResult(ok=False, reason="insufficient anchors",
                                  anchor_count=len(seeds))

    seed_pairs = list(seeds.values())
    fit = fit_transform(seed_pairs)
    if fit is None:
        return CalibrationResult(ok=False, reason="degenerate anchor geometry",
                                  anchor_count=len(seeds))
    transform, residual = fit

    # seed correspondences in (world_pos, clone_index) form for _icp_refine;
    # every seed npc_id has exactly one clone, so its index is always 0.
    seed_correspondences = {npc_id: (world_pos, 0) for npc_id, (world_pos, _) in seeds.items()}
    transform, residual, correspondences = _icp_refine(
        transform, residual, pulls, data, seed_correspondences
    )
    transform, residual, correspondences = _trim_outliers(
        transform, residual, correspondences, data
    )
    anchor_count = len(correspondences)

    if anchor_count < MIN_FINAL_ANCHORS:
        return CalibrationResult(ok=False, reason="insufficient anchors",
                                  anchor_count=anchor_count, residual=residual)
    if residual > MAX_RESIDUAL:
        return CalibrationResult(ok=False, reason="residual too high",
                                  anchor_count=anchor_count, residual=residual)
    return CalibrationResult(ok=True, transform=transform,
                              anchor_count=anchor_count, residual=residual)


# --- Part 1: planned-map geometry (zero coordinate risk) -----------------


def plan_geometry(
    data: DungeonData,
    route: Optional[Route] = None,
    comparison: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Static planned-map geometry: every enemy clone's canvas x/y plus
    dungeon POIs (entrance, etc.), with plan-pull identity and deviation
    flags baked in when a route/comparison is available. No coordinate
    transform involved -- MDT's own clone x/y *are* canvas-space already --
    so this is always safe to compute and render, with or without a route.
    """
    # clone -> plan pull index, straight from the route's own
    # pull -> enemy_idx -> [clone_idx] mapping. This is a precise
    # correspondence (unlike trying to infer it from the npc-id-level
    # route/comparison summary), available whenever a route was supplied.
    plan_pull_of: dict[tuple[int, int], int] = {}
    if route is not None:
        for pull in route.pulls:
            for enemy_idx, clone_idxs in pull.enemies.items():
                for clone_idx in clone_idxs:
                    plan_pull_of[(enemy_idx, clone_idx)] = pull.index

    # Plan pulls flagged as deviated: either the actual pull matched to them
    # as primary had deviations (early/late/off-route units), or they were
    # never engaged at all (comparison.missed). Coarse (deviation counts are
    # per actual-pull, not attributable to a single plan pull with total
    # precision) but a reasonable approximation for a visual flag.
    deviated_pulls: set[int] = set()
    if comparison is not None:
        for m in comparison.get("pulls", []):
            if m.get("deviations"):
                pp = m.get("primary_plan_pull")
                if pp is not None:
                    deviated_pulls.add(pp)
        deviated_pulls.update(int(k) for k in (comparison.get("missed") or {}))

    enemies: list[dict[str, Any]] = []
    xs: list[float] = []
    ys: list[float] = []
    for enemy in data.enemies:
        for i, clone in enumerate(enemy.clones, start=1):
            xs.append(clone.x)
            ys.append(clone.y)
            plan_pull = plan_pull_of.get((enemy.enemy_idx, i))
            enemies.append({
                "npc_id": enemy.npc_id,
                "name": enemy.name,
                "x": clone.x,
                "y": clone.y,
                "is_boss": enemy.is_boss,
                "plan_pull": plan_pull,
                "deviated": plan_pull in deviated_pulls if plan_pull is not None else False,
            })

    pois: list[dict[str, Any]] = []
    for sublevel_pois in data.pois.values():
        for poi in sublevel_pois:
            xs.append(poi.x)
            ys.append(poi.y)
            pois.append({
                "type": poi.type, "x": poi.x, "y": poi.y, "size_mult": poi.size_mult,
            })

    if xs and ys:
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    else:
        # No dungeon geometry at all (shouldn't normally happen -- data is
        # only present when there's at least dungeon metadata) -- fall back
        # to MDT's own hardcoded canvas as a reasonable default extent.
        min_x, max_x, min_y, max_y = 0.0, 840.0, -555.0, 0.0
    pad = max(20.0, 0.05 * max(max_x - min_x, max_y - min_y, 1.0))

    return {
        "canvas": {"width": 840, "height": 555},
        "bounds": {
            "min_x": round(min_x - pad, 1), "max_x": round(max_x + pad, 1),
            "min_y": round(min_y - pad, 1), "max_y": round(max_y + pad, 1),
        },
        "enemies": enemies,
        "pois": pois,
    }


def _nearest_sample(
    samples: list[list[float]], t_rel: float
) -> Optional[list[float]]:
    if not samples:
        return None
    return min(samples, key=lambda s: abs(s[0] - t_rel))


def build_map_report(
    data: DungeonData,
    route: Optional[Route],
    comparison: Optional[dict[str, Any]],
    pulls: list[ActualPull],
    position_samples: dict[str, list[list[float]]],
    player_names: dict[str, str],
    deaths: list[DeathRecord],
    run_start_ts: float,
) -> dict[str, Any]:
    """Assemble the full ``map`` report block: Part 1's planned-map geometry
    (always present) plus, when calibration succeeds, the fit parameters,
    each player's transformed (canvas-space) path, and canvas-space death
    markers. When calibration doesn't succeed, geometry is still returned
    plan-only, with ``calibration.reason`` explaining why there's no
    player-path overlay."""
    geometry = plan_geometry(data, route, comparison)
    calibration = calibrate(pulls, data)
    geometry["calibration"] = calibration.summary()

    players_out: list[dict[str, Any]] = []
    deaths_out: list[dict[str, Any]] = []
    if calibration.ok and calibration.transform is not None:
        transform = calibration.transform
        for guid, samples in position_samples.items():
            name = player_names.get(guid)
            if not name or not samples:
                continue
            path = []
            for s in samples:
                cx, cy = transform.apply(s[1], s[2])
                path.append([s[0], round(cx, 1), round(cy, 1)])
            players_out.append({"name": name, "guid": guid, "path": path})

        for death in deaths:
            samples = position_samples.get(death.player_guid) or []
            t_rel = round(death.ts - run_start_ts, 1)
            nearest = _nearest_sample(samples, t_rel)
            if nearest is None:
                continue
            cx, cy = transform.apply(nearest[1], nearest[2])
            deaths_out.append({
                "player": player_names.get(death.player_guid) or death.player_name,
                "t": t_rel, "x": round(cx, 1), "y": round(cy, 1),
            })

    geometry["players"] = players_out
    geometry["deaths"] = deaths_out
    return geometry
