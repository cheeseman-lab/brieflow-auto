#!/usr/bin/env python3
"""brieflow_tiles.py — test/validation/anchor tile selection.

Pick representative tiles for diagnostics (select_test_tile[_from_metadata]),
well-spread validation tiles for sweeps (select_validation_tiles_from_metadata),
and well-spread SBS alignment anchors (select_initial_sbs_tiles, via
farthest-point sampling). Pure stdlib + lazy nd2/pandas; no dependency on other
plugin modules. Re-exported from brieflow_wizard.

Hard rule (do not change): select_test_tile does NOT fall back to tile 0 on read
failure — tile 0 is an edge tile and silent fallback picks bad data. It returns
tiles: [] and the caller routes to operator selection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = [
    "select_test_tile",
    "select_test_tile_from_metadata",
    "select_validation_tiles_from_metadata",
    "select_tiles_one_per_well",
    "select_initial_sbs_tiles",
    "list_wells_from_metadata",
    "_farthest_point_indices",
]


# Well ids round-trip through list_wells_from_metadata as f"{row_dir}{col_dir}"
# (e.g. "A1", or Opera Phenix "r03c05"). Splitting them back with well[0],
# well[1:] only works for the alpha-row convention — it turns "r03c05" into
# "r" + "03c05" and the metadata path 404s. Mirrors brieflow's own
# split_well_to_cols (lib/shared/file_utils.py).
_WELL_PHENIX = re.compile(r"^([Rr]\d+)([Cc]\d+)$")   # r03c05 -> r03 / c05
_WELL_ALPHA = re.compile(r"^([A-Za-z]+)(\d+)$")       # A1 / B12 -> A / 1


def _split_well(well: str) -> tuple[str, str]:
    """Split a well id into its (row_dir, col_dir) path components."""
    m = _WELL_PHENIX.match(well) or _WELL_ALPHA.match(well)
    if m:
        return m.group(1), m.group(2)
    return well[0], well[1:]


def list_wells_from_metadata(
    analysis_dir: str | Path, phase: str = "phenotype"
) -> list[str]:
    """Enumerate the distinct wells present in a phase's brieflow-preprocess
    metadata. Walks the same layout as :func:`select_test_tile_from_metadata`
    (`brieflow_output/preprocess/metadata/<phase>/<plate>/<well_row>/<well_col>/
    combined_metadata.parquet`) and returns sorted distinct well names (e.g.
    ``["A1", "A2", "B1"]``).

    Pure/read-only and defensive: returns ``[]`` on any missing directory so
    callers can route to operator selection rather than crash. Used by the
    aggregate phase to resolve TEST_WELL_1/TEST_WELL_2 from two distinct wells
    (the merge/aggregate/cluster phases share the plate/well grid with
    phenotype — same assumption as resolve_phase's meta_phase).
    """
    base = Path(analysis_dir) / "brieflow_output" / "preprocess" / "metadata" / phase
    if not base.is_dir():
        return []
    wells: set[str] = set()
    for plate_dir in sorted(base.iterdir()):
        if not plate_dir.is_dir():
            continue
        for row_dir in sorted(plate_dir.iterdir()):
            if not row_dir.is_dir():
                continue
            for col_dir in sorted(row_dir.iterdir()):
                if not col_dir.is_dir():
                    continue
                if (col_dir / "combined_metadata.parquet").exists():
                    wells.add(f"{row_dir.name}{col_dir.name}")
    return sorted(wells)


def select_test_tile(
    nd2_path: str | Path,
    n_tiles: int = 7,
    interior_radius_frac: float = 0.6,
) -> dict:
    """Pick interior, well-distributed test tile indices from an ND2's stage
    positions. Returns:

        {
          "tiles": [int, ...],          # sorted tile indices to use; empty on failure
          "well_center_um": (x, y),
          "well_radius_um": float,
          "rationale": "picked N tiles in r < 0.6*r_max, distributed by angle",
        }

    Returns `{"tiles": []}` (NOT `[0]`) on every failure path — tile 0 is an
    edge tile and a silent-failure fallback to it picks bad tiles. The caller
    must check for empty `tiles` and route to operator selection via
    `AskUserQuestion`.
    """
    try:
        import nd2  # lazy import — keeps brieflow_wizard importable without nd2
        import math
    except ImportError:
        return {
            "tiles": [],
            "rationale": "nd2 package not available — operator must pick by hand",
            "well_center_um": None,
            "well_radius_um": None,
        }
    try:
        # nd2 0.10+: imread returns ndarray (no .metadata). The right path is
        # ND2File context-manager, then walk experiment loops for an XYPosLoop.
        with nd2.ND2File(str(nd2_path)) as f:
            points = None
            for loop in f.experiment:
                params = getattr(loop, "parameters", None)
                p = getattr(params, "points", None) if params is not None else None
                if p:
                    points = p
                    break
        if not points:
            return {
                "tiles": [],
                "rationale": "ND2 has no XYPosLoop / stage points — operator must pick by hand",
                "well_center_um": None,
                "well_radius_um": None,
            }
        positions = [(p.stagePositionUm.x, p.stagePositionUm.y) for p in points]
    except Exception as e:
        return {
            "tiles": [],
            "rationale": f"could not read ND2 stage positions ({type(e).__name__}: {e}) — operator must pick by hand",
            "well_center_um": None,
            "well_radius_um": None,
        }
    if not positions:
        return {
            "tiles": [0],
            "rationale": "no stage positions in ND2 metadata; defaulting to tile 0",
            "well_center_um": None,
            "well_radius_um": None,
        }

    cx = sum(x for x, _ in positions) / len(positions)
    cy = sum(y for _, y in positions) / len(positions)
    import math

    radii = [math.hypot(x - cx, y - cy) for x, y in positions]
    r_max = max(radii)
    interior = [
        (i, math.atan2(y - cy, x - cx))
        for i, ((x, y), r) in enumerate(zip(positions, radii))
        if r < interior_radius_frac * r_max
    ]
    if not interior:
        interior = list(enumerate(math.atan2(y - cy, x - cx) for x, y in positions))
    # Sample uniformly by angle
    interior.sort(key=lambda p: p[1])
    step = max(1, len(interior) // n_tiles)
    tiles = sorted(
        {interior[i * step][0] for i in range(min(n_tiles, len(interior) // step + 1))}
    )
    return {
        "tiles": tiles,
        "well_center_um": (cx, cy),
        "well_radius_um": r_max,
        "rationale": f"picked {len(tiles)} tiles in r < {interior_radius_frac}*r_max ({interior_radius_frac * r_max:.0f} um), distributed by angle",
    }


def select_test_tile_from_metadata(
    analysis_dir: str | Path,
    phase: str,
    n_tiles: int = 3,
    interior_radius_frac: float = 0.6,
    plate: int | str | None = None,
    well: str | None = None,
    seed: int | None = None,
) -> dict:
    """Pick interior, well-distributed test tile indices using brieflow-preprocess
    metadata. Designed for post-preprocess auto-probe: operators don't have to
    point at a specific ND2; this scans the structured metadata that the
    preprocess phase already wrote.

    Reads `brieflow_output/preprocess/metadata/<phase>/<plate>/<well_row>/<well_col>/combined_metadata.parquet`
    (one parquet per well, all tile/cycle rows) and dedupes to unique tiles
    with their stage positions.

    Args:
        analysis_dir: screen analysis directory (contains brieflow_output/).
        phase: 'sbs' or 'phenotype' — must match the preprocess subtree.
        n_tiles: how many test tiles to pick.
        interior_radius_frac: only consider tiles within this fraction of the
            well's outer radius (default 0.6 — generous interior).
        plate: optionally pin a specific plate (int or str). If None, uses the
            first available plate alphabetically.
        well: optionally pin a specific well (e.g. 'A2'). If None, picks one
            uniformly at random from the available wells.
        seed: RNG seed for the random well pick (None → nondeterministic).

    Returns:
        {
          "plate": <plate>,
          "well": <well>,                 # e.g. 'A2'
          "tiles": [int, ...],            # interior tile indices
          "well_center_um": (x, y),
          "well_radius_um": float,
          "rationale": "picked N tiles in r < 0.6*r_max in well A2 (random of {A1,A2,A3,B1,B2,B3})",
        }

    Returns `{"tiles": []}` (NOT `[0]`) on every failure path so callers can
    route to operator selection.
    """
    import math
    import random as _random

    try:
        import pandas as pd
    except ImportError:
        return {
            "tiles": [],
            "rationale": "pandas not available — cannot read preprocess metadata",
            "plate": None, "well": None,
            "well_center_um": None, "well_radius_um": None,
        }

    base = Path(analysis_dir) / "brieflow_output" / "preprocess" / "metadata" / phase
    if not base.is_dir():
        return {
            "tiles": [],
            "rationale": f"preprocess metadata not found at {base} — run preprocess phase first",
            "plate": None, "well": None,
            "well_center_um": None, "well_radius_um": None,
        }

    # Discover available (plate, well) pairs from the layout
    # base/<plate>/<well_row>/<well_col>/combined_metadata.parquet
    available: list[tuple[str, str]] = []
    for plate_dir in sorted(base.iterdir()):
        if not plate_dir.is_dir():
            continue
        for row_dir in sorted(plate_dir.iterdir()):
            if not row_dir.is_dir():
                continue
            for col_dir in sorted(row_dir.iterdir()):
                if not col_dir.is_dir():
                    continue
                pq = col_dir / "combined_metadata.parquet"
                if pq.exists():
                    available.append((plate_dir.name, f"{row_dir.name}{col_dir.name}"))

    if not available:
        return {
            "tiles": [],
            "rationale": f"no combined_metadata.parquet found under {base}",
            "plate": None, "well": None,
            "well_center_um": None, "well_radius_um": None,
        }

    # Filter by plate / well if pinned
    if plate is not None:
        plate_s = str(plate)
        available = [(p, w) for p, w in available if p == plate_s]
    if well is not None:
        available = [(p, w) for p, w in available if w == well]

    if not available:
        return {
            "tiles": [],
            "rationale": f"no metadata matches plate={plate} well={well}",
            "plate": plate, "well": well,
            "well_center_um": None, "well_radius_um": None,
        }

    # Pick a well: random by default (matches operator expectation), unless pinned
    rng = _random.Random(seed)
    pool_str = "{" + ",".join(w for _, w in available) + "}"
    sel_plate, sel_well = rng.choice(available) if well is None else available[0]

    # Load the parquet for the chosen (plate, well)
    well_row, well_col = _split_well(sel_well)
    parquet_path = base / sel_plate / well_row / well_col / "combined_metadata.parquet"
    df = pd.read_parquet(parquet_path, columns=["tile", "x_pos", "y_pos"])
    # Dedupe to one row per tile (positions are stable across cycles)
    df = df.drop_duplicates(subset=["tile"]).sort_values("tile").reset_index(drop=True)

    positions = list(zip(df["x_pos"].tolist(), df["y_pos"].tolist()))
    tile_ids = df["tile"].tolist()

    cx = sum(x for x, _ in positions) / len(positions)
    cy = sum(y for _, y in positions) / len(positions)
    radii = [math.hypot(x - cx, y - cy) for x, y in positions]
    r_max = max(radii) if radii else 0.0

    interior = [
        (tile_ids[i], math.atan2(positions[i][1] - cy, positions[i][0] - cx))
        for i, r in enumerate(radii)
        if r < interior_radius_frac * r_max
    ]
    if not interior and len(tile_ids) < 9:
        # Too few tiles for an interior ring (a small test well): take those nearest the centre.
        nearest = sorted(range(len(tile_ids)), key=lambda i: radii[i])[:n_tiles]
        interior = [
            (tile_ids[i], math.atan2(positions[i][1] - cy, positions[i][0] - cx)) for i in nearest
        ]
    if not interior:
        return {
            "tiles": [],
            "rationale": f"no interior tiles (r < {interior_radius_frac}*r_max) in well {sel_well}",
            "plate": sel_plate, "well": sel_well,
            "well_center_um": (cx, cy), "well_radius_um": r_max,
        }

    # Sample uniformly by angle for cross-well distribution
    interior.sort(key=lambda p: p[1])
    step = max(1, len(interior) // n_tiles)
    picked = sorted(
        {interior[i * step][0] for i in range(min(n_tiles, (len(interior) - 1) // step + 1))}
    )

    well_origin = "pinned" if well is not None else f"random of {pool_str}"
    return {
        "plate": int(sel_plate) if sel_plate.isdigit() else sel_plate,
        "well": sel_well,
        "tiles": picked,
        "well_center_um": (cx, cy),
        "well_radius_um": r_max,
        "rationale": (
            f"picked {len(picked)} tiles in r < {interior_radius_frac}*r_max "
            f"({interior_radius_frac * r_max:.0f} um) in well {sel_well} ({well_origin}), "
            f"distributed by angle"
        ),
    }


def select_validation_tiles_from_metadata(
    analysis_dir: str | Path,
    phase: str,
    n_tiles: int = 5,
    min_plates: int = 1,
    min_wells: int = 2,
    interior_radius_frac: float = 0.5,
    seed: int | None = None,
) -> dict:
    """Pick `n_tiles` interior tiles spanning ≥`min_plates` plates and
    ≥`min_wells` distinct wells, using brieflow-preprocess metadata.

    Designed for cross-tile validation (Step 7) — the operator wants
    coverage of plate-to-plate and well-to-well variance, not just spatial
    variance within one well. Distinct from `select_test_tile_from_metadata`
    which intentionally samples within one well for the operator's
    `TEST_TILE` (Step 1).

    Returns:
        {
          "tiles": [(plate:int|str, well:str, tile:int), ...],
          "n_plates_spanned": int,
          "n_wells_spanned": int,
          "rationale": "picked N tiles across P plates / W wells, ...",
        }

    Returns `{"tiles": []}` (NOT a fabricated tile-0 fallback) if metadata
    is missing or coverage can't reach min_plates × min_wells — caller
    surfaces a finding + routes to operator selection.
    """
    import math
    import random as _random
    from collections import defaultdict

    try:
        import pandas as pd
    except ImportError:
        return {
            "tiles": [],
            "rationale": "pandas not available — cannot read preprocess metadata",
            "n_plates_spanned": 0,
            "n_wells_spanned": 0,
        }

    base = Path(analysis_dir) / "brieflow_output" / "preprocess" / "metadata" / phase
    if not base.is_dir():
        return {
            "tiles": [],
            "rationale": f"preprocess metadata not found at {base} — run preprocess phase first",
            "n_plates_spanned": 0,
            "n_wells_spanned": 0,
        }

    # Discover available (plate, well) pairs.
    by_plate: dict[str, list[str]] = defaultdict(list)
    for plate_dir in sorted(base.iterdir()):
        if not plate_dir.is_dir():
            continue
        for row_dir in sorted(plate_dir.iterdir()):
            if not row_dir.is_dir():
                continue
            for col_dir in sorted(row_dir.iterdir()):
                if (col_dir / "combined_metadata.parquet").exists():
                    by_plate[plate_dir.name].append(f"{row_dir.name}{col_dir.name}")

    if not by_plate:
        return {
            "tiles": [],
            "rationale": f"no combined_metadata.parquet found under {base}",
            "n_plates_spanned": 0,
            "n_wells_spanned": 0,
        }

    rng = _random.Random(seed)

    # Plate selection — at least min_plates if available; cap at len(by_plate).
    all_plates = sorted(by_plate.keys())
    n_plates_pick = min(len(all_plates), max(1, min_plates))
    plates_picked = rng.sample(all_plates, n_plates_pick) if n_plates_pick < len(all_plates) else all_plates

    # Within each chosen plate, pick at least min_wells (or all if fewer available).
    # Distribute n_tiles across the selected (plate, well) bins.
    bins: list[tuple[str, str]] = []  # (plate, well) pairs we will draw from
    for plate in plates_picked:
        wells = sorted(by_plate[plate])
        n_wells_pick = min(len(wells), max(1, min_wells))
        wells_picked = rng.sample(wells, n_wells_pick) if n_wells_pick < len(wells) else wells
        for w in wells_picked:
            bins.append((plate, w))

    if not bins:
        return {
            "tiles": [],
            "rationale": "no (plate, well) bins after selection — coverage requirements unsatisfiable",
            "n_plates_spanned": 0,
            "n_wells_spanned": 0,
        }

    # Allocate n_tiles across bins — round-robin with at least 1 per bin where possible.
    n_tiles = max(1, n_tiles)
    per_bin = [0] * len(bins)
    for i in range(n_tiles):
        per_bin[i % len(bins)] += 1

    # For each bin, read the well's parquet and pick interior tiles distributed by angle.
    out_tiles: list[tuple] = []
    for (plate, well), n in zip(bins, per_bin):
        if n == 0:
            continue
        well_row, well_col = _split_well(well)
        parquet_path = base / plate / well_row / well_col / "combined_metadata.parquet"
        try:
            df = pd.read_parquet(parquet_path, columns=["tile", "x_pos", "y_pos"])
        except Exception:
            continue
        df = df.drop_duplicates(subset=["tile"]).sort_values("tile").reset_index(drop=True)
        positions = list(zip(df["x_pos"].tolist(), df["y_pos"].tolist()))
        tile_ids = df["tile"].tolist()
        if not positions:
            continue
        cx = sum(x for x, _ in positions) / len(positions)
        cy = sum(y for _, y in positions) / len(positions)
        radii = [math.hypot(x - cx, y - cy) for x, y in positions]
        r_max = max(radii) if radii else 0.0
        interior = [
            (tile_ids[i], math.atan2(positions[i][1] - cy, positions[i][0] - cx))
            for i, r in enumerate(radii)
            if r < interior_radius_frac * r_max
        ]
        if not interior:
            continue
        interior.sort(key=lambda p: p[1])
        step = max(1, len(interior) // n)
        picked_tiles = sorted({interior[i * step][0] for i in range(min(n, len(interior) // step + 1))})
        for t in picked_tiles[:n]:
            # Cast plate to int if numeric for consistency with parquet column type
            p_val: Any = int(plate) if plate.isdigit() else plate
            out_tiles.append((p_val, well, int(t)))

    plates_spanned = len({t[0] for t in out_tiles})
    wells_spanned = len({(t[0], t[1]) for t in out_tiles})
    return {
        "tiles": out_tiles,
        "n_plates_spanned": plates_spanned,
        "n_wells_spanned": wells_spanned,
        "rationale": (
            f"picked {len(out_tiles)} tiles across {plates_spanned} plate(s) / "
            f"{wells_spanned} well(s), interior r < {interior_radius_frac}*r_max"
        ),
    }


def select_tiles_one_per_well(analysis_dir: str | Path, phase: str) -> dict:
    """One interior tile per well — the per-well seg/align diagnostic fan-out.

    For EVERY (plate, well) with brieflow-preprocess metadata, picks the
    most-central tile (min radius from the well centroid) as its representative
    interior tile, so no well enters the run without a segmentation + alignment
    preview attempted on it. Deterministic (no sampling). Distinct from
    `select_validation_tiles_from_metadata` (a fixed n_tiles across >=min_wells,
    for sweeps) — this guarantees full per-well coverage.

    Returns:
        {
          "tiles": [(plate:int|str, well:str, tile:int), ...],  # exactly one/well
          "n_wells": int,
          "wells_without_metadata": [(plate, well), ...],
          "rationale": str,
        }

    Returns `{"tiles": []}` (never a fabricated tile-0) when metadata is missing —
    the caller surfaces a finding + routes to operator selection.
    """
    import math

    try:
        import pandas as pd
    except ImportError:
        return {
            "tiles": [],
            "n_wells": 0,
            "wells_without_metadata": [],
            "rationale": "pandas not available — cannot read preprocess metadata",
        }

    base = Path(analysis_dir) / "brieflow_output" / "preprocess" / "metadata" / phase
    if not base.is_dir():
        return {
            "tiles": [],
            "n_wells": 0,
            "wells_without_metadata": [],
            "rationale": f"preprocess metadata not found at {base} — run preprocess first",
        }

    tiles: list[tuple] = []
    missing: list[tuple] = []
    for plate_dir in sorted(base.iterdir()):
        if not plate_dir.is_dir():
            continue
        plate = plate_dir.name
        p_val: Any = int(plate) if plate.isdigit() else plate
        for row_dir in sorted(plate_dir.iterdir()):
            if not row_dir.is_dir():
                continue
            for col_dir in sorted(row_dir.iterdir()):
                if not col_dir.is_dir():
                    continue
                well = f"{row_dir.name}{col_dir.name}"
                pq = col_dir / "combined_metadata.parquet"
                if not pq.exists():
                    continue
                try:
                    df = pd.read_parquet(pq, columns=["tile", "x_pos", "y_pos"])
                except Exception:
                    missing.append((p_val, well))
                    continue
                df = df.drop_duplicates(subset=["tile"]).sort_values("tile").reset_index(drop=True)
                positions = list(zip(df["x_pos"].tolist(), df["y_pos"].tolist()))
                tile_ids = df["tile"].tolist()
                if not positions:
                    missing.append((p_val, well))
                    continue
                cx = sum(x for x, _ in positions) / len(positions)
                cy = sum(y for _, y in positions) / len(positions)
                # Most-central tile = min radius from the well centroid; interior by
                # construction, so a single pick needs no radius gate.
                best = min(
                    range(len(positions)),
                    key=lambda i: math.hypot(positions[i][0] - cx, positions[i][1] - cy),
                )
                tiles.append((p_val, well, int(tile_ids[best])))

    return {
        "tiles": tiles,
        "n_wells": len(tiles),
        "wells_without_metadata": missing,
        "rationale": (
            f"one central interior tile per well: {len(tiles)} well(s)"
            + (f"; {len(missing)} well(s) missing metadata" if missing else "")
        ),
    }


def _farthest_point_indices(positions: list, n: int) -> list:
    """Indices of `n` points maximally spread across `positions` via farthest-
    point sampling. Seeds from the point farthest from the centroid (an outer
    tile), then greedily adds the point with the largest min-distance to the
    chosen set. Deterministic. Good spatial coverage (corners + spread) for an
    affine alignment fit — unlike angle-only or interior-only sampling.
    """
    import math

    m = len(positions)
    if n >= m:
        return list(range(m))
    cx = sum(p[0] for p in positions) / m
    cy = sum(p[1] for p in positions) / m
    seed = max(range(m), key=lambda i: math.hypot(positions[i][0] - cx, positions[i][1] - cy))
    picked = [seed]
    dist = [math.hypot(px - positions[seed][0], py - positions[seed][1])
            for px, py in positions]
    while len(picked) < n:
        nxt = max(range(m), key=lambda i: dist[i])
        if dist[nxt] <= 0:  # all remaining points coincide with chosen ones
            break
        picked.append(nxt)
        for i, (px, py) in enumerate(positions):
            d = math.hypot(px - positions[nxt][0], py - positions[nxt][1])
            if d < dist[i]:
                dist[i] = d
    return picked


def select_initial_sbs_tiles(
    analysis_dir: str | Path,
    n_tiles: int = 20,
    plate: str | None = None,
    well: str | None = None,
    min_cells: int = 20,
    min_well_frac: float = 0.8,
) -> dict:
    """Pick `n_tiles` SBS tiles maximally spread across a well — merge auto-mode
    INITIAL_SBS_TILES (alignment anchors). Reads per-tile stage coordinates
    (`x_pos`, `y_pos`) from brieflow-preprocess SBS metadata and farthest-point
    samples them, so the anchors span the well extent (not clustered → no
    degenerate affine fit). Tile IDs are grid-consistent across wells, so one
    representative well suffices for geometry.

    `n_tiles` is deliberately generous (clamped to the tiles available). An anchor
    is only useful if its affine fit lands inside `det_range` with a score above
    threshold, and merge needs >= 5 such seeds; on harder screens roughly half of
    any given set fails that filter, so a small set lands under the floor. Measured
    on three screens 2026-08-31: with 6 anchors one passed 6/6 but the others got 2/6 and
    4/6, failing 25 and 32 of 60 wells; seeding all 21 tiles gave both 10 passing.
    The per-screen passing sets barely overlap, so there is no good small subset —
    the robust move is to offer many and let the filter choose. Cost is a few
    seconds of seeding against a phase that runs for minutes.

    Before sampling, tiles too sparse to anchor an alignment are dropped: an initial
    site must have >= `min_cells` cells in at least `min_well_frac` of wells (counted
    from SBS `sbs_info.parquet`; absent-in-a-well counts as 0). An empty tile yields
    zero triangles, and merge `fast_alignment.nearest_neighbors` then hits a zero-size
    array (matterhorn 2026-06-17: tile 188 had 0 cells in wells A1/A3 → 2 wells failed).

    The rule is a fraction of wells rather than all of them, because requiring every
    well vetoes the whole screen over a few dead ones: one sparse screen had a healthy
    median of 348 cells/tile, but five near-empty wells disqualified all 21 tiles, so
    INITIAL_SBS_TILES came back empty and the merge notebook died on None. Wells that
    stay sparse now fail individually at alignment, which merge already tolerates.

    0.8 is where the curve flattens rather than a round number: across the four weakest
    screens tested, 0.9 yields 20 anchors everywhere except the sparsest (7, too thin once
    the det_range/score filter takes its half), 0.8 yields 20 everywhere, and loosening
    further changes nothing.
    The gate is skipped (with a note) if SBS cell outputs aren't present yet.

    Picks the first available (plate, well) unless given. Returns::

        {"tiles": [int, ...], "plate": ..., "well": ..., "rationale": ...}

    Empty tiles (NOT a fabricated fallback) if metadata is missing — caller
    routes to operator selection.
    """
    try:
        import pandas as pd
    except ImportError:
        return {"tiles": [], "plate": None, "well": None,
                "rationale": "pandas not available — cannot read preprocess metadata"}

    base = Path(analysis_dir) / "brieflow_output" / "preprocess" / "metadata" / "sbs"
    if not base.is_dir():
        return {"tiles": [], "plate": None, "well": None,
                "rationale": f"SBS preprocess metadata not found at {base} — run preprocess first"}

    # Resolve (plate, well): use given, else first with a combined_metadata.parquet.
    parquet_path = None
    if plate is not None and well is not None:
        well_row, well_col = _split_well(well)
        parquet_path = base / str(plate) / well_row / well_col / "combined_metadata.parquet"
    else:
        for plate_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            for row_dir in sorted(p for p in plate_dir.iterdir() if p.is_dir()):
                for col_dir in sorted(p for p in row_dir.iterdir() if p.is_dir()):
                    cand = col_dir / "combined_metadata.parquet"
                    if cand.exists():
                        parquet_path = cand
                        plate, well = plate_dir.name, f"{row_dir.name}{col_dir.name}"
                        break
                if parquet_path:
                    break
            if parquet_path:
                break

    if not parquet_path or not Path(parquet_path).exists():
        return {"tiles": [], "plate": plate, "well": well,
                "rationale": f"no combined_metadata.parquet found under {base}"}

    try:
        df = pd.read_parquet(parquet_path, columns=["tile", "x_pos", "y_pos"])
    except Exception as e:
        return {"tiles": [], "plate": plate, "well": well,
                "rationale": f"could not read tile coordinates: {type(e).__name__}: {e}"}

    df = df.drop_duplicates(subset=["tile"]).sort_values("tile").reset_index(drop=True)
    positions = list(zip(df["x_pos"].tolist(), df["y_pos"].tolist()))
    tile_ids = df["tile"].tolist()
    if not positions:
        return {"tiles": [], "plate": plate, "well": well,
                "rationale": "well has no tiles with coordinates"}

    # Cell-density gate: drop tiles sparse in ANY well before spatial sampling.
    n_candidates = len(tile_ids)
    gate_note = ""
    sbs_parquets = sorted(
        (Path(analysis_dir) / "brieflow_output" / "sbs" / "parquets").glob(
            "*/*/*/sbs_info.parquet"
        )
    )
    if sbs_parquets:
        well_counts: dict[str, dict[int, int]] = {}
        for pq in sbs_parquets:
            wname = f"{pq.parts[-3]}{pq.parts[-2]}"  # <row><col> -> well
            try:
                vc = pd.read_parquet(pq, columns=["tile"]).groupby("tile").size()
            except Exception:
                continue
            well_counts[wname] = {int(t): int(c) for t, c in vc.items()}
        if well_counts:
            wells = sorted(well_counts)

            def _ok_frac(t: int) -> float:
                return sum(well_counts[w].get(t, 0) >= min_cells for w in wells) / len(wells)

            def _worst(t: int) -> str:
                return min(wells, key=lambda w: well_counts[w].get(t, 0))

            keep = {t for t in tile_ids if _ok_frac(t) >= min_well_frac}
            excluded = [t for t in tile_ids if t not in keep]
            if excluded:
                detail = ", ".join(
                    f"{t} ({_ok_frac(t):.0%} of wells, worst {_worst(t)})"
                    for t in sorted(excluded)[:8]
                )
                gate_note = (
                    f"; excluded {len(excluded)} tile(s) below {min_well_frac:.0%} of wells "
                    f"with >={min_cells} cells (of {len(wells)} wells): {detail}"
                )
                positions = [p for p, t in zip(positions, tile_ids) if t in keep]
                tile_ids = [t for t in tile_ids if t in keep]
        else:
            gate_note = "; density gate skipped (no readable sbs_info.parquet)"
    else:
        gate_note = "; density gate skipped (SBS cell outputs not present yet)"

    if not positions:
        return {"tiles": [], "plate": plate, "well": well,
                "rationale": (
                    f"no SBS tiles have >={min_cells} cells in all wells — "
                    f"data too sparse for auto initial-site selection{gate_note}"
                )}

    idx = _farthest_point_indices(positions, max(1, n_tiles))
    tiles = sorted(int(tile_ids[i]) for i in idx)
    return {
        "tiles": tiles,
        "plate": plate,
        "well": well,
        "rationale": (
            f"{len(tiles)} tiles farthest-point-sampled across well {well} "
            f"(of {len(positions)} cell-dense tiles, from {n_candidates} total) "
            f"for max spatial spread{gate_note}"
        ),
    }
