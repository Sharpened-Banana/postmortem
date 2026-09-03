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
from dataclasses import dataclass, field
from typing import Any, Optional

from ..combatlog.events import Event
from ..mdt.dungeon_data import DungeonData
from ..mdt.route import Route
from .pulls import ActualPull
from .stats import DeathRecord

Point = tuple[float, float]

#: MDT's planning canvas -- what every clone/POI coordinate is in.
CANVAS_W, CANVAS_H = 840.0, 555.0

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
    # A single planned clone only pins down a world position if a single
    # such mob actually exists. MDT lists summoned/respawning adds once
    # (Ruby Life Pools' Scorchlings and Infused Whelps: one clone each,
    # engaged as a dozen distinct GUIDs spread across the whole dungeon in
    # a real run, 2026-09-03), and pairing *any* of those with the one
    # planned dot produced anchors hundreds of canvas units off -- enough
    # to wreck the seed fit entirely (RMS 165 on Ruby Life Pools). More
    # distinct GUIDs engaged than clones planned means the planned dot
    # doesn't stand for "the" mob, so such npc_ids are not anchors.
    guids_by_npc: dict[int, set[str]] = {}
    for pull in pulls:
        for unit in pull.units:
            if unit.npc_id is not None:
                guids_by_npc.setdefault(unit.npc_id, set()).add(unit.guid)

    seeds: dict[int, tuple[Point, Point]] = {}
    for pull in pulls:
        for unit in pull.units:
            if unit.npc_id is None or unit.first_pos is None or unit.npc_id in seeds:
                continue
            enemy = data.enemy_by_npc_id(unit.npc_id)
            if enemy is None or len(enemy.clones) != 1:
                continue
            if len(guids_by_npc.get(unit.npc_id, ())) > len(enemy.clones):
                continue
            clone = enemy.clones[0]
            seeds[unit.npc_id] = (unit.first_pos, (clone.x, clone.y))
    return seeds


def _all_encounters(pulls: list[ActualPull], data: DungeonData) -> list[tuple[int, Point]]:
    """Every real engagement with a known world position, for any npc_id
    that has at least one planned clone -- INCLUDING repeats (unlike
    ``_seed_anchors``/``_icp_refine``'s baseline, which keep only the
    first occurrence per npc_id). Feeds ``_trim_outliers``' RANSAC step a
    richer candidate pool for rescuing a failing fit: see that function's
    ``extra_encounters`` docstring for why repeats matter."""
    encounters: list[tuple[int, Point]] = []
    for pull in pulls:
        for unit in pull.units:
            if unit.npc_id is None or unit.first_pos is None:
                continue
            enemy = data.enemy_by_npc_id(unit.npc_id)
            if enemy is None or not enemy.clones:
                continue
            encounters.append((unit.npc_id, unit.first_pos))
    return encounters


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

    Each npc_id's world position is resolved once, up front, to its first
    engagement across the whole run -- same "first occurrence only" choice
    as ``_seed_anchors``. Real bug (2026-09-03): re-deriving it from a
    fresh scan of ``pulls`` on every iteration let a *later* re-engagement
    of the same npc_id silently overwrite an already-good correspondence
    (dict assignment keeps whichever pull's unit was iterated last, not
    the first) -- confirmed on a real production run where a wave-spawning
    add pulled in three different rooms had its accurate seed-anchor
    position clobbered by its third, unrelated engagement, dragging the
    fit's residual from ~80 to ~150.
    """
    world_pos_by_npc: dict[int, Point] = {}
    for pull in pulls:
        for unit in pull.units:
            if unit.npc_id is None or unit.first_pos is None or unit.npc_id in world_pos_by_npc:
                continue
            enemy = data.enemy_by_npc_id(unit.npc_id)
            if enemy is None or not enemy.clones:
                continue
            world_pos_by_npc[unit.npc_id] = unit.first_pos

    for _ in range(MAX_ICP_ITERS):
        changed = False
        updated = dict(correspondences)
        for npc_id, world_pos in world_pos_by_npc.items():
            enemy = data.enemy_by_npc_id(npc_id)
            cx, cy = transform.apply(*world_pos)
            best_idx = min(
                range(len(enemy.clones)),
                key=lambda i: (enemy.clones[i].x - cx) ** 2 + (enemy.clones[i].y - cy) ** 2,
            )
            prev = updated.get(npc_id)
            if prev is None or prev[1] != best_idx:
                changed = True
            updated[npc_id] = (world_pos, best_idx)

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
    extra_encounters: Optional[list[tuple[int, Point]]] = None,
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
    anchors are bad), scores each by how many *distinct npc_ids* (tie-
    broken by raw point count) it explains within _RANSAC_INLIER_DIST, and
    keeps the winning candidate's inlier set for a final refit.

    ``extra_encounters`` -- every real (npc_id, world_pos) engagement
    across the whole run, INCLUDING repeats of npc_ids already present in
    ``correspondences`` (unlike ``correspondences`` itself, which -- via
    ``_icp_refine`` -- keeps only each npc_id's first engagement). Real
    production case (2026-09-03): a wave-spawning add pulled in three
    different rooms only has ONE MDT-marked clone, so only one of those
    three real engagements can be geometrically correct -- but which one
    isn't knowable in advance. Folding every repeat in as an extra
    candidate point lets RANSAC discover which engagement (if any) is the
    right one, instead of being stuck with whichever single occurrence
    ``_icp_refine`` happened to see first. Only pulled in here, once the
    baseline fit already needs rescuing (residual over MAX_RESIDUAL) --
    an already-good baseline is never second-guessed by noisier repeat
    data, so this can only help, matching this function's existing
    contract. Candidates are scored by *distinct npc_id* count first
    (what MIN_ANCHORS_AFTER_TRIM actually gates on), not raw inlier
    count -- confirmed necessary empirically: scoring by raw count alone
    let a real run's winning candidate be ~70 points from just 2 heavily
    repeated npc_ids, a numerically tight but practically unverifiable
    "landmark of one" that MIN_ANCHORS_AFTER_TRIM exists to reject.

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
    seen: set[tuple[int, Point]] = set()
    for npc_id, (world_pos, clone_idx) in correspondences.items():
        enemy = data.enemy_by_npc_id(npc_id)
        if enemy is None or clone_idx >= len(enemy.clones):
            continue
        clone = enemy.clones[clone_idx]
        items.append((npc_id, world_pos, (clone.x, clone.y)))
        seen.add((npc_id, world_pos))

    for npc_id, world_pos in (extra_encounters or ()):
        if npc_id not in correspondences or (npc_id, world_pos) in seen:
            continue
        enemy = data.enemy_by_npc_id(npc_id)
        if enemy is None or not enemy.clones:
            continue
        cx, cy = transform.apply(*world_pos)
        best_idx = min(
            range(len(enemy.clones)),
            key=lambda i: (enemy.clones[i].x - cx) ** 2 + (enemy.clones[i].y - cy) ** 2,
        )
        clone = enemy.clones[best_idx]
        items.append((npc_id, world_pos, (clone.x, clone.y)))
        seen.add((npc_id, world_pos))

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
    best_key: Optional[tuple[int, int]] = None
    for combo in index_combos:
        sample_pairs = [(items[i][1], items[i][2]) for i in combo]
        candidate = fit_transform(sample_pairs)
        if candidate is None:
            continue
        cand_transform, _ = candidate
        inliers = []
        npc_ids_seen: set[int] = set()
        for i, (npc_id, world_pos, canvas_pos) in enumerate(items):
            px, py = cand_transform.apply(*world_pos)
            if (px - canvas_pos[0]) ** 2 + (py - canvas_pos[1]) ** 2 <= inlier_dist_sq:
                inliers.append(i)
                npc_ids_seen.add(npc_id)
        key = (len(npc_ids_seen), len(inliers))
        if best_key is None or key > best_key:
            best_key = key
            best_inliers = inliers

    if best_inliers is None:
        return transform, residual, correspondences
    distinct_inlier_npcs = {items[i][0] for i in best_inliers}
    if len(distinct_inlier_npcs) < MIN_ANCHORS_AFTER_TRIM:
        return transform, residual, correspondences

    final_pairs = [(items[i][1], items[i][2]) for i in best_inliers]
    fit = fit_transform(final_pairs)
    if fit is None:
        return transform, residual, correspondences
    new_transform, new_residual = fit
    # One representative correspondence per distinct npc_id in the
    # accepted inlier set -- anchor_count (see calibrate()) reports
    # distinct landmarks, not raw repeat-encounter points, so only one
    # entry per npc_id is kept here too.
    new_correspondences: dict[int, tuple[Point, int]] = {}
    for i in best_inliers:
        npc_id, world_pos, _canvas_pos = items[i]
        if npc_id not in new_correspondences:
            new_correspondences[npc_id] = (world_pos, correspondences.get(npc_id, (None, 0))[1])
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
        transform, residual, correspondences, data, _all_encounters(pulls, data)
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


# --- Per-sub-map calibration (the primary path) ---------------------------
#
# The similarity fit above assumes ONE rigid transform maps the whole
# dungeon's world space onto MDT's canvas. That is false for every current
# dungeon: Blizzard splits a dungeon into several uiMaps (Ruby Life Pools
# is 2094 + 2095; Murder Row is 2433 + 2434 + 2435), each covering its own
# world rectangle, and MDT's single-floor canvas is a hand-made COMPOSITE
# of those sub-maps' art, each pasted at its own scale and offset. So the
# world->canvas relation is piecewise: one scale + translation per uiMap,
# with the sub-map's orientation (Blizzard maps are north-up) fixed.
# Confirmed against real anchors (2026-09-03): a global similarity fit on
# 8 boss positions in Ruby Life Pools had RMS 138; the per-uiMap fits
# below had RMS 22 (2094, 5 anchors) and 8 (2095, 2 anchors), with the
# free rotation coming out at -1 deg -- i.e. north-up is exactly right.
#
# The world rectangle each uiMap covers comes straight from the combat
# log: ``MAP_CHANGE,<uiMapID>,"<name>",x0,x1,y0,y1`` is written whenever the
# logging player's map changes (including at CHALLENGE_MODE_START), and
# every advanced-block position carries the uiMapID it is on. Normalising
# a world position into "map fraction" coordinates -- x across the map from
# west to east, y down from north -- turns the unknown per-map transform
# into a plain scale + translation, which 2 anchors already over-determine
# (4 equations, 3 unknowns), so even a sub-map with only its two bosses
# engaged yields a fit that can be VALIDATED, not just solved.

# Per-anchor inlier distance (canvas units) for the pair-based robust
# selection in _fit_map, and the RMS a sub-map's final fit must clear.
# MDT's clone dots are hand-placed on stylised art, so 20-30 units of
# irreducible noise is normal (see the real residuals above); 40 keeps
# such anchors while rejecting the 100-400 unit errors a wrong pairing or
# a mob engaged far from its planned spot produces.
MAP_INLIER_DIST = 40.0
MAP_MAX_RESIDUAL = 40.0
# With exactly 2 anchors there is a single degree of validation left, so a
# lone bad anchor can hide behind a modest RMS -- demand a tighter one.
MAP_MAX_RESIDUAL_TWO_ANCHORS = 20.0
MIN_MAP_ANCHORS = 2

MapBounds = tuple[float, float, float, float]  # x0 (max), x1 (min), y0 (max), y1 (min)


def collect_map_bounds(events: list[Event]) -> dict[int, MapBounds]:
    """uiMapID -> world-coordinate bounds, from the run's MAP_CHANGE lines.
    Degenerate (zero-area) rectangles are ignored."""
    bounds: dict[int, MapBounds] = {}
    for event in events:
        if event.name != "MAP_CHANGE" or len(event.params) < 6:
            continue
        p = event.params
        try:
            ui_map_id = int(p[0].strip())
            x0, x1, y0, y1 = (float(v) for v in p[2:6])
        except ValueError:
            continue
        if ui_map_id in bounds or x0 == x1 or y0 == y1:
            continue
        bounds[ui_map_id] = (x0, x1, y0, y1)
    return bounds


def _map_normalized(bounds: MapBounds, wx: float, wy: float) -> Point:
    """World position -> north-up map coordinates in canvas-sized units:
    x grows eastward (world -y), y grows negative going south (world -x),
    matching the canvas' own y-negative-downward convention."""
    x0, x1, y0, y1 = bounds
    return (CANVAS_W * (y0 - wy) / (y0 - y1), -CANVAS_H * (x0 - wx) / (x0 - x1))


@dataclass
class MapTransform:
    """canvas = scale * normalized(world) + (tx, ty) for one uiMap."""

    ui_map_id: int
    bounds: MapBounds
    scale: float
    tx: float
    ty: float
    anchor_count: int = 0
    residual: Optional[float] = None

    def apply(self, wx: float, wy: float) -> Point:
        ux, uy = _map_normalized(self.bounds, wx, wy)
        return (self.scale * ux + self.tx, self.scale * uy + self.ty)


@dataclass
class MapCalibration:
    ok: bool
    transforms: dict[int, MapTransform] = field(default_factory=dict)
    # uiMapID -> why it got no transform (shown so a missing path segment
    # is explainable rather than mysterious).
    skipped: dict[int, str] = field(default_factory=dict)
    reason: Optional[str] = None

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "method": "per_uimap",
            "anchor_count": sum(t.anchor_count for t in self.transforms.values()),
        }
        residuals = [t.residual for t in self.transforms.values() if t.residual is not None]
        if residuals:
            out["residual"] = round(max(residuals), 2)
        if self.reason is not None:
            out["reason"] = self.reason
        if self.transforms:
            out["maps"] = {
                str(t.ui_map_id): {
                    "anchor_count": t.anchor_count,
                    "residual": round(t.residual, 2) if t.residual is not None else None,
                    "scale": round(t.scale, 4),
                    "translation": {"x": round(t.tx, 2), "y": round(t.ty, 2)},
                }
                for t in self.transforms.values()
            }
        if self.skipped:
            out["uncalibrated_maps"] = {str(k): v for k, v in self.skipped.items()}
        return out


def _fit_scale_translation(pairs: list[tuple[Point, Point]]) -> Optional[tuple[float, float, float, float]]:
    """Least-squares ``canvas = s * u + t`` over (u, canvas) pairs; returns
    (s, tx, ty, rms) or None when the u points have no spread."""
    n = len(pairs)
    if n < 2:
        return None
    ux = sum(u[0] for u, _ in pairs) / n
    uy = sum(u[1] for u, _ in pairs) / n
    cx = sum(c[0] for _, c in pairs) / n
    cy = sum(c[1] for _, c in pairs) / n
    den = sum((u[0] - ux) ** 2 + (u[1] - uy) ** 2 for u, _ in pairs)
    if den <= 1e-9:
        return None
    num = sum((u[0] - ux) * (c[0] - cx) + (u[1] - uy) * (c[1] - cy) for u, c in pairs)
    s = num / den
    tx, ty = cx - s * ux, cy - s * uy
    rms = math.sqrt(sum((s * u[0] + tx - c[0]) ** 2 + (s * u[1] + ty - c[1]) ** 2
                        for u, c in pairs) / n)
    return s, tx, ty, rms


def _fit_map(anchors: list[tuple[Point, Point]]) -> Optional[tuple[float, float, float, float, int]]:
    """Robust scale+translation for one sub-map from (normalized_world,
    canvas) anchors: every 2-anchor subset proposes a fit, the proposal
    with the most inliers (ties: lowest RMS) is refit on its inliers, and
    the result is accepted only with a positive scale (a negative one is a
    point reflection, never a real map placement) and an RMS under the
    gate. Returns (s, tx, ty, rms, inlier_count) or None.

    Pair-based rather than least-squares-then-trim for the same reason as
    _trim_outliers: with 3-5 anchors and one or two 100+ unit outliers, a
    fit over all of them is dragged so far that the good anchors look
    like the outliers (real case: Murder Row's main map, 5 anchors, where
    a boss's companion pet and one patrolling mob were 130-360 units off
    and the all-anchor fit had RMS 201; the best pair's 3 inliers fit to
    RMS 13)."""
    if len(anchors) < MIN_MAP_ANCHORS:
        return None
    best: Optional[tuple[int, float, list[tuple[Point, Point]]]] = None
    for a, b in itertools.combinations(range(len(anchors)), 2):
        proposal = _fit_scale_translation([anchors[a], anchors[b]])
        if proposal is None or proposal[0] <= 0:
            continue
        s, tx, ty, _ = proposal
        inliers = [
            (u, c) for u, c in anchors
            if math.dist((s * u[0] + tx, s * u[1] + ty), c) <= MAP_INLIER_DIST
        ]
        if len(inliers) < MIN_MAP_ANCHORS:
            continue
        refit = _fit_scale_translation(inliers)
        if refit is None or refit[0] <= 0:
            continue
        key = (len(inliers), -refit[3])
        if best is None or key > (best[0], -best[1]):
            best = (len(inliers), refit[3], inliers)
    if best is None:
        return None
    n_inliers, _, inliers = best
    s, tx, ty, rms = _fit_scale_translation(inliers)  # type: ignore[misc]
    gate = MAP_MAX_RESIDUAL_TWO_ANCHORS if n_inliers == 2 else MAP_MAX_RESIDUAL
    if rms > gate:
        return None
    return s, tx, ty, rms, n_inliers


def _guid_counts(pulls: list[ActualPull]) -> dict[int, int]:
    """npc_id -> how many distinct units of it were engaged this run."""
    guids: dict[int, set[str]] = {}
    for pull in pulls:
        for unit in pull.units:
            if unit.npc_id is not None:
                guids.setdefault(unit.npc_id, set()).add(unit.guid)
    return {npc_id: len(g) for npc_id, g in guids.items()}


def _seed_anchors_by_map(
    pulls: list[ActualPull], data: DungeonData
) -> dict[Optional[int], list[tuple[Point, Point]]]:
    """uiMapID -> [(world_pos, canvas_pos)] for the same unambiguous
    single-clone anchors as ``_seed_anchors`` (one per npc_id, first
    engagement, never a summoned/respawning mob), grouped by the sub-map
    the mob's position was reported on. ``None`` collects anchors whose
    position came without a uiMapID."""
    guid_counts = _guid_counts(pulls)
    seen: set[int] = set()
    out: dict[Optional[int], list[tuple[Point, Point]]] = {}
    for pull in pulls:
        for unit in pull.units:
            if unit.npc_id is None or unit.first_pos is None or unit.npc_id in seen:
                continue
            enemy = data.enemy_by_npc_id(unit.npc_id)
            if enemy is None or len(enemy.clones) != 1:
                continue
            if guid_counts.get(unit.npc_id, 0) > len(enemy.clones):
                continue
            seen.add(unit.npc_id)
            clone = enemy.clones[0]
            out.setdefault(unit.first_map_id, []).append((unit.first_pos, (clone.x, clone.y)))
    return out


# How long after CHALLENGE_MODE_START a player's first position still
# counts as "standing at the entrance". The group is rooted in place for
# the ~10s countdown and pulls straight from the door, so the earliest
# sample inside this window is within a few yards of the entrance POI.
ENTRANCE_WINDOW_S = 20.0


def entrance_anchor(
    data: DungeonData, position_samples: dict[str, list[list[float]]]
) -> Optional[tuple[Point, Point, int]]:
    """``(world_pos, canvas_pos, ui_map_id)`` pairing MDT's dungeonEntrance
    POI with the earliest player position of the run, or None when either
    side is missing/ambiguous. Every run has this anchor regardless of
    which mobs were engaged, which is what lets a sub-map with a single
    boss on it (Altar of Fangs' main map, real case) still calibrate."""
    entrances = [p for pois in data.pois.values() for p in pois if p.type == "dungeonEntrance"]
    if len(entrances) != 1:
        return None
    earliest: Optional[list[float]] = None
    for samples in position_samples.values():
        for s in samples:
            if len(s) > 3 and s[3] and s[0] <= ENTRANCE_WINDOW_S:
                if earliest is None or s[0] < earliest[0]:
                    earliest = s
    if earliest is None:
        return None
    poi = entrances[0]
    return ((earliest[1], earliest[2]), (poi.x, poi.y), int(earliest[3]))


def calibrate_maps(
    pulls: list[ActualPull],
    data: DungeonData,
    map_bounds: dict[int, MapBounds],
    extra_anchors: Optional[list[tuple[Point, Point, int]]] = None,
) -> MapCalibration:
    """Fit one MapTransform per uiMap that has MAP_CHANGE bounds and at
    least MIN_MAP_ANCHORS usable anchors. ``ok`` when at least one sub-map
    calibrates; sub-maps that don't are listed in ``skipped`` and their
    positions simply aren't drawn (a partial overlay of correct paths beats
    a complete overlay of wrong ones). ``extra_anchors`` are additional
    ``(world_pos, canvas_pos, ui_map_id)`` correspondences from outside the
    engaged-mob pool (see ``entrance_anchor``)."""
    if not any(u.first_pos is not None for pull in pulls for u in pull.units):
        return MapCalibration(ok=False, reason="no position data (advanced combat logging required)")

    anchors_by_map = _seed_anchors_by_map(pulls, data)
    for world, canvas, ui_map_id in extra_anchors or []:
        anchors_by_map.setdefault(ui_map_id, []).append((world, canvas))
    result = MapCalibration(ok=False)
    for ui_map_id, anchors in anchors_by_map.items():
        if ui_map_id is None:
            continue
        bounds = map_bounds.get(ui_map_id)
        if bounds is None:
            result.skipped[ui_map_id] = "no MAP_CHANGE bounds logged for this map"
            continue
        if len(anchors) < MIN_MAP_ANCHORS:
            result.skipped[ui_map_id] = f"insufficient anchors ({len(anchors)})"
            continue
        normalized = [(_map_normalized(bounds, *w), c) for w, c in anchors]
        fit = _fit_map(normalized)
        if fit is None:
            result.skipped[ui_map_id] = f"no consistent fit from {len(anchors)} anchors"
            continue
        s, tx, ty, rms, n = fit
        result.transforms[ui_map_id] = MapTransform(
            ui_map_id=ui_map_id, bounds=bounds, scale=s, tx=tx, ty=ty,
            anchor_count=n, residual=rms,
        )
    if result.transforms:
        result.ok = True
    else:
        result.reason = "insufficient anchors"
    return result


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
            # Routes name clones by MDT's own key (EnemyClone.idx), which
            # diverges from list position past a deleted clone's hole --
            # see mdt/extract._lua_int_items. Position is only the fallback
            # for data extracted before that key was recorded.
            clone_key = clone.idx if clone.idx is not None else i
            plan_pull = plan_pull_of.get((enemy.enemy_idx, clone_key))
            enemies.append({
                "npc_id": enemy.npc_id,
                "name": enemy.name,
                "x": clone.x,
                "y": clone.y,
                "is_boss": enemy.is_boss,
                "plan_pull": plan_pull,
                "deviated": plan_pull in deviated_pulls if plan_pull is not None else False,
                # Which floor's 840x555 canvas this point lives on -- the
                # renderer draws one panel per floor (each with its own
                # background art when available, see mapart.py).
                "sublevel": clone.sublevel if clone.sublevel is not None else 1,
            })

    pois: list[dict[str, Any]] = []
    for sublevel, sublevel_pois in data.pois.items():
        for poi in sublevel_pois:
            xs.append(poi.x)
            ys.append(poi.y)
            pois.append({
                "type": poi.type, "x": poi.x, "y": poi.y, "size_mult": poi.size_mult,
                "sublevel": int(sublevel) if sublevel is not None else 1,
            })

    if xs and ys:
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    else:
        # No dungeon geometry at all (shouldn't normally happen -- data is
        # only present when there's at least dungeon metadata) -- fall back
        # to MDT's own hardcoded canvas as a reasonable default extent.
        min_x, max_x, min_y, max_y = 0.0, CANVAS_W, -CANVAS_H, 0.0
    pad = max(20.0, 0.05 * max(max_x - min_x, max_y - min_y, 1.0))

    return {
        "canvas": {"width": int(CANVAS_W), "height": int(CANVAS_H)},
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
    map_bounds: Optional[dict[int, MapBounds]] = None,
) -> dict[str, Any]:
    """Assemble the full ``map`` report block: Part 1's planned-map geometry
    (always present) plus, when calibration succeeds, the fit parameters,
    each player's transformed (canvas-space) path, and canvas-space death
    markers. When calibration doesn't succeed, geometry is still returned
    plan-only, with ``calibration.reason`` explaining why there's no
    player-path overlay.

    With ``map_bounds`` (the run's MAP_CHANGE rectangles, see
    ``collect_map_bounds``) calibration is per sub-map -- the only model
    that actually matches MDT's composite canvases -- and each position
    sample is transformed by the fit for the uiMap it was recorded on
    (``samples[i][3]``); samples on a sub-map without a fit are left out.
    Without it, the legacy single-similarity fit is used."""
    geometry = plan_geometry(data, route, comparison)

    # transform_for(sample) -> the transform to draw that sample with, or
    # None to leave it out. Which one depends on the calibration mode.
    if map_bounds:
        entrance = entrance_anchor(data, position_samples)
        map_cal = calibrate_maps(
            pulls, data, map_bounds, extra_anchors=[entrance] if entrance else None,
        )
        geometry["calibration"] = map_cal.summary()

        def transform_for(sample: list[float]):
            ui_map_id = int(sample[3]) if len(sample) > 3 and sample[3] else None
            return map_cal.transforms.get(ui_map_id)
    else:
        calibration = calibrate(pulls, data)
        geometry["calibration"] = calibration.summary()

        def transform_for(sample: list[float]):
            return calibration.transform

    players_out: list[dict[str, Any]] = []
    deaths_out: list[dict[str, Any]] = []
    if geometry["calibration"].get("ok"):
        for guid, samples in position_samples.items():
            name = player_names.get(guid)
            if not name or not samples:
                continue
            # ``path`` is every drawable sample; ``segments`` is the same
            # points split wherever a sample was left out (a sub-map with
            # no fit) so the renderer doesn't bridge the hole with a
            # straight line across the map -- which is exactly what a
            # single polyline did on a real Murder Row report whose third
            # sub-map had too few anchors.
            path: list[list[float]] = []
            segments: list[list[list[float]]] = []
            current: list[list[float]] = []
            for s in samples:
                transform = transform_for(s)
                if transform is None:
                    if current:
                        segments.append(current)
                        current = []
                    continue
                cx, cy = transform.apply(s[1], s[2])
                point = [s[0], round(cx, 1), round(cy, 1)]
                path.append(point)
                current.append(point)
            if current:
                segments.append(current)
            if path:
                players_out.append({"name": name, "guid": guid, "path": path,
                                    "segments": segments})

        for death in deaths:
            samples = [s for s in (position_samples.get(death.player_guid) or [])
                       if transform_for(s) is not None]
            t_rel = round(death.ts - run_start_ts, 1)
            nearest = _nearest_sample(samples, t_rel)
            if nearest is None:
                continue
            cx, cy = transform_for(nearest).apply(nearest[1], nearest[2])
            deaths_out.append({
                "player": player_names.get(death.player_guid) or death.player_name,
                "t": t_rel, "x": round(cx, 1), "y": round(cy, 1),
            })

    geometry["players"] = players_out
    geometry["deaths"] = deaths_out
    return geometry
