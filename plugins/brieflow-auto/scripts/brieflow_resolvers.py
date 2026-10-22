#!/usr/bin/env python3
"""brieflow_resolvers.py — the resolution registry.

Every interview row that isn't asked of the operator (bucket ``auto-probe`` or
``agent-resolved``) is resolved by SOMETHING. This module is the single
discoverable map from such a param to the thing that resolves it, plus a
coverage validator (:func:`validate_resolver_coverage`) the test suite runs
against the real ``interview_template.json`` so there are no *orphan promises*
— a param flagged auto-probe/agent-resolved with nothing behind it that would
silently resolve to null.

A param resolves via one of:

  * a **live python helper** — named here, resolved against the
    ``brieflow_wizard`` facade namespace (e.g. ``infer_path_pattern``,
    ``detect_data_format``, ``select_test_tile``, ``adaptive_sweep_headless``).
  * a **derivation clause** (:data:`DERIVATION`) — the row carries a
    ``library_default`` / ``screen_yaml`` derivation that ``resolve_all_phases``
    applies; no probe needed.
  * **agent interview** (:data:`AGENT_INTERVIEW`) — the agent fills it during
    the interactive interview by inspecting staged data (e.g. reading
    ``merge_final.parquet`` columns). No standalone python helper; legitimately
    resolved, just not by this layer. (Headless runner resolution of these
    is a separate concern — see ``resolve_phase`` / the per-phase gap analysis.)
  * a **known unimplemented probe** (:data:`KNOWN_UNIMPLEMENTED_PROBES`) — a
    promised auto-probe with no implementation yet, tracked explicitly so it's
    visible (resolves to null until built), not silently broken.

``brieflow_wizard.py`` re-exports the public symbols here after the module split.
"""

from __future__ import annotations

from typing import Any, Iterable

# Sentinels for non-helper resolution paths.
DERIVATION = "<derivation>"
AGENT_INTERVIEW = "<agent-interview>"

#: Promised auto-probes with no implementation yet. ``param_name -> reason``.
#: These resolve to null until the probe is built; tracked here so the gap is
#: visible and version-controlled rather than silently hidden.
KNOWN_UNIMPLEMENTED_PROBES: dict[str, str] = {
    # (empty) — DET_RANGE now has probe_det_range. Re-add a row here if a promised
    # auto-probe is declared in the template before its helper exists.
}

#: The discoverable resolution map: ``param_name -> resolver``. The resolver is
#: either a live helper name (resolved against the brieflow_wizard namespace), a
#: sentinel (DERIVATION / AGENT_INTERVIEW), or omitted iff the param is in
#: KNOWN_UNIMPLEMENTED_PROBES. Same-named rows across notebooks share an entry
#: (e.g. TEST_PLATE in sbs/phenotype/merge all use select_test_tile).
RESOLVER_MAP: dict[str, str] = {
    # --- agent-resolved (run-time sweeps / eval-table selections) ---
    "THRESHOLD_READS": "adaptive_sweep_headless",
    "PEAK_WIDTH": "adaptive_sweep_headless",
    "FINAL_LEIDEN_RESOLUTIONS": "select_final_resolutions",
    # --- auto-probe with a live python helper (probe_preprocess composes these) ---
    "SBS_PATH_PATTERN": "infer_path_pattern",
    "PHENOTYPE_PATH_PATTERN": "infer_path_pattern",
    "SBS_DATA_FORMAT": "detect_data_format",
    "PHENOTYPE_DATA_FORMAT": "detect_data_format",
    "SBS_DATA_ORGANIZATION": "detect_organization",
    "PHENOTYPE_DATA_ORGANIZATION": "detect_organization",
    "SBS_N_Z_PLANES": "detect_z_planes",
    "PHENOTYPE_N_Z_PLANES": "detect_z_planes",
    "PHENOTYPE_ROUND_ORDER": "detect_round_order",
    "SBS_CHANNEL_ORDER": "probe_preprocess",
    "PHENOTYPE_CHANNEL_ORDER": "probe_preprocess",
    "TEST_PLATE": "select_test_tile",
    "TEST_WELL": "select_test_tile",
    "TEST_TILE": "select_test_tile",
    "INITIAL_SBS_TILES": "select_initial_sbs_tiles",
    # --- auto-probe filled by probe_preprocess (derived from the path pattern) ---
    "SBS_PATH_METADATA": "probe_preprocess",
    "PHENOTYPE_PATH_METADATA": "probe_preprocess",
    "SBS_METADATA_ORDER_TYPE": "probe_preprocess",
    "PHENOTYPE_METADATA_ORDER_TYPE": "probe_preprocess",
    "SBS_CHANNEL_ORDER_FLIP": "probe_preprocess",
    "PHENOTYPE_CHANNEL_ORDER_FLIP": "probe_preprocess",
    # tiff-only external-metadata path vars (gated *_DATA_FORMAT==tiff)
    "SBS_METADATA_IMAGES_DIR_FP": AGENT_INTERVIEW,
    "SBS_METADATA_PATH_PATTERN": AGENT_INTERVIEW,
    "SBS_METADATA_PATH_METADATA": AGENT_INTERVIEW,
    "SBS_METADATA_PATH_ORDER_TYPE": AGENT_INTERVIEW,
    "PHENOTYPE_METADATA_IMAGES_DIR_FP": AGENT_INTERVIEW,
    "PHENOTYPE_METADATA_PATH_PATTERN": AGENT_INTERVIEW,
    "PHENOTYPE_METADATA_PATH_METADATA": AGENT_INTERVIEW,
    "PHENOTYPE_METADATA_PATH_ORDER_TYPE": AGENT_INTERVIEW,
    # aggregate + cluster test selectors
    # aggregate TEST_WELL_1/2: auto-resolved at phase time (resolve_phase C1c) from
    # two distinct phenotype-metadata wells. Points at the live well-enumeration
    # helper so the registry reflects the real resolution path, not a dead sentinel.
    "TEST_WELL_1": "list_wells_from_metadata",
    "TEST_WELL_2": "list_wells_from_metadata",
    # aggregate (list) + cluster (underscore-joined string) TEST_CHANNEL_COMBO:
    # auto-resolved at phase time (resolve_phase C1c) from the operator's
    # CHANNEL_COMBOS key-decision (CHANNEL_COMBOS[0]); no standalone helper — it's
    # a per-phase reformat of an existing interview value, so it stays AGENT_INTERVIEW.
    "TEST_CHANNEL_COMBO": AGENT_INTERVIEW,
    "DET_RANGE": "probe_det_range",
    # MERGE_APPROACH: screen.yaml objective first, measured phenotype density once it exists.
    "MERGE_APPROACH": "probe_phenotype_cells_per_tile",
    # Written by the dye-balance probe the runner runs after the SBS notebook
    # (brieflow_dye_balance_probe.py); the probe applies its own winners to the
    # notebook markers, config and interview, so there is no standalone resolver.
    "DYE_SCALE": AGENT_INTERVIEW,
    "BRIGHTNESS_REGIONS": AGENT_INTERVIEW,
}

_SENTINELS = frozenset({DERIVATION, AGENT_INTERVIEW})

# Buckets whose rows must be accounted for by this registry.
_REGISTERED_BUCKETS = frozenset({"auto-probe", "agent-resolved"})


def validate_resolver_coverage(rows: Iterable[dict], namespace: Any) -> dict[str, Any]:
    """Assert every auto-probe / agent-resolved param has a known resolution.

    For each such row the ``param_name`` must be either:
      * in :data:`KNOWN_UNIMPLEMENTED_PROBES` (tracked gap), or
      * in :data:`RESOLVER_MAP` pointing to a sentinel, or
      * in :data:`RESOLVER_MAP` pointing to a name that is a live callable on
        ``namespace`` (the brieflow_wizard facade).

    Returns ``{"ok": bool, "orphans": [...], "dead_resolvers": [...]}``:
      * ``orphans``        — params with no registry entry and not a known gap.
      * ``dead_resolvers`` — params whose named resolver isn't a live callable.
    """
    orphans: list[str] = []
    dead: list[str] = []
    for row in rows:
        if row.get("bucket") not in _REGISTERED_BUCKETS:
            continue
        name = row.get("param_name", "<unnamed>")
        if name in KNOWN_UNIMPLEMENTED_PROBES:
            continue
        resolver = RESOLVER_MAP.get(name)
        if resolver is None:
            orphans.append(name)
            continue
        if resolver in _SENTINELS:
            continue
        fn = getattr(namespace, resolver, None)
        if not callable(fn):
            dead.append(f"{name}->{resolver}")
    return {"ok": not orphans and not dead, "orphans": orphans, "dead_resolvers": dead}


# ---------------------------------------------------------------------------
# Per-phase run-time resolution (the deferred 0d item)
# ---------------------------------------------------------------------------
# resolve_phase is the single scoped home for what the runner loop used to do
# inline per phase: resolve sweep tiles, patch filled interview values into the
# notebook, run the parameter sweeps (adaptive or fixed) and patch winners, then
# the phase-specific N0 steps (cluster FINAL_LEIDEN_RESOLUTIONS selection, merge
# INITIAL_SBS_TILES probe).
#
# It is a PURE ORCHESTRATOR: every side-effecting capability is injected via
# `ctx`, so brieflow_resolvers needs no import of brieflow_sweep / brieflow_markers
# / brieflow_interview / the runner conductor (no cycles), and the runner
# loop tests that monkeypatch those callables flow straight through ctx.
#
# ctx keys (all required):
#   state:     analysis_dir, conda_env, lab, iv, iv_path, nb_path, tag
#   callables: notify, select_validation_tiles_from_metadata, is_active,
#              patch_marker_block_value, lab_sweep_spec, lab_sweep_band,
#              adaptive_sweep_headless, sweep_headless, pick_sweep_winner,
#              record_tuned_confirmed, save_interview, resolve_cluster_final,
#              resolve_merge_initial_sbs_tiles
#   optional:  adaptive_marimo_sweep_multitile (reactive live-kernel sweep;
#              preferred for adaptive bands, falls back to adaptive_sweep_headless)
#              resolve_merge_approach (MERGE_APPROACH from measured cell density)


def _pinned_sweep(analysis_dir) -> dict:
    """Operator-pinned sweep scope from `screen.yaml`: `sweep.tiles` (explicit
    `[plate, well, tile]` triples) or `sweep.wells` (restrict the one-tile-per-well
    sample to these wells). A line whose signal lives in a few wells cannot be tuned
    on a plate-wide sample — one line decoded on 2 of 22 wells and the sweep found no
    finite rate on the other 20."""
    out = {"tiles": [], "wells": set()}
    try:
        from pathlib import Path

        import yaml

        p = Path(analysis_dir) / "screen.yaml"
        if not p.exists():
            return out
        sweep = (yaml.safe_load(p.read_text()) or {}).get("sweep") or {}
        out["tiles"] = [(t[0], str(t[1]), int(t[2])) for t in (sweep.get("tiles") or [])]
        out["wells"] = {str(w) for w in (sweep.get("wells") or [])}
    except Exception:
        pass
    return out


def _apply_sweep_pin(tiles: list, pin: dict) -> list:
    """Explicit tiles win; otherwise keep only pinned wells; an empty match falls
    back to the unpinned sample rather than sweeping nothing."""
    if pin.get("tiles"):
        return list(pin["tiles"])
    if pin.get("wells"):
        kept = [t for t in tiles if str(t[1]) in pin["wells"]]
        return kept or list(tiles)
    return list(tiles)


def _bounded_sweep_tiles(tiles: list, max_tiles: int) -> list:
    """Subsample a (plate,row,col)-sorted tile list to a bounded, well-spread set
    so the parameter sweep stays O(max_tiles x values) instead of O(n_wells x
    values) — the one-tile-per-well fan-out is intractable on large plates. No-op
    when max_tiles is falsy or the list already fits."""
    if not max_tiles or len(tiles) <= max_tiles:
        return list(tiles)
    step = len(tiles) / max_tiles
    return [tiles[int(i * step)] for i in range(max_tiles)]


def resolve_phase(phase: str, ctx: dict) -> dict:
    """Run the per-phase agent resolution for `phase`. Returns
    ``{"paused": bool, "reason": str | None}`` — paused=True means an operator
    decision is required (only the cluster N0 step can pause); the caller should
    set_paused + notify + halt. Behavior-identical to the prior inline blocks.
    """
    import asyncio

    analysis_dir = ctx["analysis_dir"]
    conda_env = ctx["conda_env"]
    lab = ctx["lab"]
    iv = ctx["iv"]
    iv_path = ctx["iv_path"]
    nb_path = ctx["nb_path"]
    tag = ctx["tag"]
    notify = ctx["notify"]

    # C0: SKIP_CYCLES_MAP must mirror SKIP_CYCLES for simple barcodes. The imaging
    # skip is applied to the reads (SKIP_CYCLES=[k] → reads are PREFIX_LENGTH-1 bp),
    # so the barcode-library prefix must be trimmed the SAME way via SKIP_CYCLES_MAP,
    # or N-1 bp reads never equal N bp library prefixes and SINGLE_MAP_RATE collapses
    # to ~0. The wizard otherwise leaves SKIP_CYCLES_MAP=None (lab-default) while
    # SKIP_CYCLES gets set from the "dropped a cycle" note — matterhorn 2026-06-17 hit
    # exactly this (11 bp reads vs 12 bp library → 0% mapping, eval_mapping crash).
    # Simple barcodes only; multi (MAP/RECOMB) regions are resolved separately.
    if phase == "sbs":
        _sbs = {r.get("param_name"): r for r in iv.get("params", []) if r.get("notebook") == "sbs"}
        _bt, _sc, _scm = (
            _sbs.get("BARCODE_TYPE"),
            _sbs.get("SKIP_CYCLES"),
            _sbs.get("SKIP_CYCLES_MAP"),
        )
        if (
            _bt
            and _bt.get("value") == "simple"
            and _sc
            and _sc.get("value")
            and _scm is not None
            and _scm.get("value") is None
        ):
            _scm["value"] = list(_sc["value"])
            _scm["value_source"] = "gen"
            notify(
                analysis_dir,
                tag,
                f"sbs: SKIP_CYCLES_MAP={_scm['value']} derived (mirrors SKIP_CYCLES "
                "for simple barcodes — keeps library prefix length == read length)",
            )
        # C0b: validate SKIP_CYCLES holds real cycle NUMBERS, not 0-based indices.
        # The notebook does SKIP_CYCLES_INDICES = [SBS_CYCLES.index(c) for c in
        # SKIP_CYCLES]; an entry not in the detected cycle set raises an opaque
        # "ValueError: 0 is not in list" (kazbek set [0] for "first cycle"; cycles
        # are 1..N). Surface it clearly pre-run instead. OUTSTANDING #5.
        if _sc and _sc.get("value"):
            try:
                import pandas as _pd

                _sf = analysis_dir / "config" / "sbs_samples.tsv"
                if _sf.exists():
                    _cyc = sorted(_pd.read_csv(_sf, sep="\t")["cycle"].unique().tolist())
                    _bad = [c for c in _sc["value"] if c not in _cyc]
                    if _bad:
                        notify(
                            analysis_dir,
                            tag,
                            f"sbs: SKIP_CYCLES={_sc['value']} has {_bad} not in detected "
                            f"cycles {_cyc} — SKIP_CYCLES uses 1-based cycle NUMBERS, not "
                            f"0-based indices (first cycle = {_cyc[0]}). Fix before running.",
                            priority="action_needed",
                        )
            except Exception:
                pass

    # C0c (merge): SBS_METADATA_CYCLE must not be a SKIPPED cycle. fast_alignment pairs
    # SBS↔phenotype tiles (find_closest_tiles) using this cycle's SBS stage coordinates;
    # a dropped/bad cycle (in SKIP_CYCLES) can carry unreliable stage coords and mis-pair
    # entire wells. matterhorn 2026-06-17: well A3's cycle-1 stage coords were offset
    # ~1200 px vs cycles 2-12, so nearest-tile pairing picked the wrong phenotype tile and
    # fast_alignment found no registration (0/6 anchors) — while 5 wells passed 6/6.
    # SBS_METADATA_CYCLE defaults to 1, which is exactly the cycle most often skipped.
    # Bump to the lowest non-skipped cycle when the configured cycle is itself skipped.
    if phase == "merge":
        # Global SKIP_CYCLES (single-config screens) …
        _skip_global = (
            next(
                (
                    r.get("value")
                    for r in iv.get("params", [])
                    if r.get("notebook") == "sbs" and r.get("param_name") == "SKIP_CYCLES"
                ),
                None,
            )
            or []
        )
        # … UNION per-plate skip_cycles from screen.yaml `iss.plate_overrides`
        # (branch-and-converge screens: e.g. jannu plate 1 skips its bad cycle 1
        # while plate 2 keeps all cycles). A cycle skipped on ANY plate carries the
        # unreliable stage coords that mis-pair tiles, and SBS_METADATA_CYCLE is a
        # single global merge param, so it must avoid the skip on every plate.
        # Without this the per-plate skip never reaches the merge resolver, so the
        # default cycle 1 (the usual casualty) silently mis-registers that plate —
        # jannu 2026-07-06: plate-1 well B2's cycle-1 stage coords were offset
        # ~1130 px vs cycles 2-12, so fast_alignment mis-paired tiles and failed.
        # (The matterhorn 2026-06-17 case one comment up was the global-SKIP_CYCLES
        # variant of the same bug; this branch covers the per-plate variant.)
        _skip_plate: list = []
        try:
            import yaml as _yaml

            _sy = analysis_dir / "screen.yaml"
            if _sy.exists():
                _doc = _yaml.safe_load(_sy.read_text()) or {}
                _ov = ((_doc.get("iss") or {}).get("plate_overrides")) or {}
                for _pov in _ov.values():
                    _skip_plate.extend((_pov or {}).get("skip_cycles") or [])
        except Exception:
            pass
        _skip = sorted({*_skip_global, *_skip_plate})
        _mc = next(
            (
                r
                for r in iv.get("params", [])
                if r.get("notebook") == "merge" and r.get("param_name") == "SBS_METADATA_CYCLE"
            ),
            None,
        )
        if _mc is not None and _skip and (_mc.get("value") is None or _mc.get("value") in _skip):
            _new = next((c for c in range(1, 1000) if c not in _skip), _mc.get("value"))
            if _new != _mc.get("value"):
                _mc["value"] = _new
                _mc["value_source"] = "gen"
                notify(
                    analysis_dir,
                    tag,
                    f"merge: SBS_METADATA_CYCLE={_new} derived (avoiding skipped cycle(s) "
                    f"{_skip} — a dropped cycle's SBS stage coords can mis-pair tiles)",
                )

    # C0d (merge): MERGE_APPROACH from the measured phenotype cells per tile, before the
    # filled values are patched into the notebook.
    if phase == "merge" and ctx.get("resolve_merge_approach"):
        _ma = ctx["resolve_merge_approach"](analysis_dir, lab, iv, iv_path)
        if _ma and _ma.get("changed"):
            notify(
                analysis_dir, tag, f"merge: MERGE_APPROACH={_ma['approach']} ({_ma['reason']})"
            )

    # C1: resolve sweep tiles for phases with sweep bands. The parameter sweep runs
    # values x tiles, so the tile count must NOT scale with the dataset — the
    # per-well fan-out (one tile per well) makes a large plate's sweep intractable.
    # Bound to a well-spread sample (max_tiles) regardless of plate size.
    phase_sweep_bands = lab.get("sweep_bands", {}).get(phase) or {}
    max_parallel_kernels = lab.get("sweep_tiles", {}).get("max_parallel_kernels", 3)
    # a loaded host (three screens sweeping at once on the 360-core VM) pushes a
    # tile's cellpose baseline past the default 15 min and the tile is lost as a
    # TimeoutExpired; the lab can lengthen it
    kernel_timeout_s = float(lab.get("sweep_tiles", {}).get("kernel_timeout_s", 900.0))
    sweep_tiles: list = []
    if phase_sweep_bands:
        try:
            max_tiles = lab.get("sweep_tiles", {}).get("max_tiles", 10)
            per_well = ctx.get("select_tiles_one_per_well")
            if per_well is not None:
                tiles_res = per_well(analysis_dir, phase)
            else:
                n_tiles = lab.get("sweep_tiles", {}).get("n_tiles", 5)
                tiles_res = ctx["select_validation_tiles_from_metadata"](
                    analysis_dir, phase, n_tiles=n_tiles
                )
            sweep_tiles = _bounded_sweep_tiles(
                _apply_sweep_pin(tiles_res.get("tiles", []), _pinned_sweep(analysis_dir)),
                max_tiles,
            )
        except Exception:
            sweep_tiles = []

    # C1b: resolve the phase's TEST tile (TEST_PLATE/WELL/TILE). Nothing else wires
    # the select_test_tile resolver for the runner, so without this the phase
    # notebook's test-image cell crashes on TEST_WELL=None. Only fills unset rows
    # (operator/manual values win); pauses to the operator if no tile is selectable.
    sel_fn = ctx.get("select_test_tile_from_metadata")
    test_rows = [
        r
        for r in iv.get("params", [])
        if r.get("notebook") == phase
        and r.get("param_name") in ("TEST_PLATE", "TEST_WELL", "TEST_TILE")
    ]
    if sel_fn and test_rows and any(r.get("value") is None for r in test_rows):
        # merge has no preprocess/metadata/merge subtree — it joins sbs+phenotype
        # and its notebook needs only a (plate, well) present in BOTH trees (it
        # reads .../metadata/phenotype/<P>/<W> and .../metadata/sbs/<P>/<W> for the
        # same well, and uses no TEST_TILE). Resolve from the phenotype metadata,
        # which shares the plate/well grid with sbs — the same assumption the merge
        # notebook makes. Other phases resolve from their own metadata subtree.
        meta_phase = "phenotype" if phase in ("merge", "aggregate", "cluster") else phase
        try:
            sel = sel_fn(analysis_dir, meta_phase)
        except Exception as e:
            sel = {"tiles": [], "rationale": f"{type(e).__name__}: {e}"}
        tiles = sel.get("tiles") or []
        if tiles:
            vmap = {
                "TEST_PLATE": sel.get("plate"),
                "TEST_WELL": sel.get("well"),
                "TEST_TILE": tiles[0],
            }
            for r in test_rows:
                v = vmap.get(r["param_name"])
                if r.get("value") is None and v is not None:
                    r["value"] = v
                    r["value_source"] = "auto-probe"
            notify(
                analysis_dir,
                tag,
                f"{phase}: test tile = plate {sel.get('plate')} well {sel.get('well')} tile {tiles[0]}",
            )
        else:
            notify(
                analysis_dir,
                tag,
                f"{phase}: no test tile auto-selectable ({sel.get('rationale', '')}) — "
                "set TEST_PLATE/WELL/TILE by hand",
                priority="action_needed",
            )
            return {"paused": True, "reason": f"{phase}: test tile needs operator selection"}

    # C1c: resolve the aggregate/cluster TEST selectors that have no live probe and
    # are mapped to a sentinel (TEST_WELL_1/2, TEST_CHANNEL_COMBO). Without this the
    # aggregate/cluster notebooks crash on None (TEST_WELL_* → NoneType not iterable;
    # TEST_CHANNEL_COMBO → a "ChCo-None" missing-file). Only fills unset rows for the
    # current phase (operator/manual values win); warns (no hard-pause) if the source
    # data isn't there.
    if phase in ("aggregate", "cluster"):
        c1c_rows = [r for r in iv.get("params", []) if r.get("notebook") == phase]

        # WELL_ANNOTATIONS_FP / SPLIT_COL (aggregate): rebuilt each pass from screen.yaml
        annotated = None
        if phase == "aggregate":
            from brieflow_well_annotations import resolve_aggregate_annotations

            split_default = ((lab.get("notebook_defaults") or {}).get("aggregate") or {}).get(
                "SPLIT_COL", "class"
            )
            ann = resolve_aggregate_annotations(
                analysis_dir, c1c_rows, ctx.get("build_well_annotations"), split_default
            )
            if ann["pause"]:
                return {"paused": True, "reason": ann["pause"]}
            for name in ann["clear"]:
                ctx["patch_marker_block_value"](nb_path, name, "None")
            for note in ann["notes"]:
                notify(analysis_dir, tag, note)
            annotated = ann["wells"]

        # TEST_WELL_1/2 (aggregate only): two DISTINCT wells, from the annotated wells when
        # a table was built (the notebook joins it onto the test wells), else from phenotype
        # metadata (the merge/aggregate/cluster phases share the plate/well grid with
        # phenotype — same assumption as meta_phase in C1b).
        well_rows = [r for r in c1c_rows if r.get("param_name") in ("TEST_WELL_1", "TEST_WELL_2")]
        if annotated and well_rows:
            from brieflow_well_annotations import set_annotated_test_wells

            notify(analysis_dir, tag, set_annotated_test_wells(c1c_rows, annotated))
        elif well_rows and any(r.get("value") is None for r in well_rows):
            list_wells = ctx.get("list_wells_from_metadata")
            if list_wells is None:
                from brieflow_tiles import list_wells_from_metadata as list_wells
            try:
                wells = list_wells(analysis_dir, "phenotype")
            except Exception:
                wells = []
            if wells:
                wmap = {
                    "TEST_WELL_1": wells[0],
                    "TEST_WELL_2": wells[1] if len(wells) >= 2 else wells[0],
                }
                for r in well_rows:
                    v = wmap.get(r["param_name"])
                    if r.get("value") is None and v is not None:
                        r["value"] = v
                        r["value_source"] = "auto-probe"
                notify(
                    analysis_dir,
                    tag,
                    f"{phase}: test wells = {wmap['TEST_WELL_1']}, {wmap['TEST_WELL_2']} "
                    f"(of {len(wells)} distinct phenotype wells)",
                )
            else:
                notify(
                    analysis_dir,
                    tag,
                    f"{phase}: no phenotype-metadata wells found — set TEST_WELL_1/2 by hand",
                    priority="action_needed",
                )

        # TEST_CHANNEL_COMBO (aggregate AND cluster): the first operator CHANNEL_COMBOS
        # combo. FORMAT DIFFERS per phase — aggregate wants a LIST, cluster wants an
        # underscore-joined STRING (it builds a "ChCo-<combo>" filename).
        combo_rows = [r for r in c1c_rows if r.get("param_name") == "TEST_CHANNEL_COMBO"]
        if combo_rows and any(r.get("value") is None for r in combo_rows):
            channel_combos = None
            for r in iv.get("params", []):
                if r.get("param_name") == "CHANNEL_COMBOS" and r.get("value"):
                    channel_combos = r.get("value")
                    break
            if channel_combos:
                try:
                    combo = channel_combos[0]
                    value = combo if phase == "aggregate" else "_".join(combo)
                except Exception:
                    combo, value = None, None
                if value is not None:
                    for r in combo_rows:
                        if r.get("value") is None:
                            r["value"] = value
                            r["value_source"] = "auto-probe"
                    notify(
                        analysis_dir,
                        tag,
                        f"{phase}: TEST_CHANNEL_COMBO={value!r} (from CHANNEL_COMBOS[0])",
                    )
            else:
                notify(
                    analysis_dir,
                    tag,
                    f"{phase}: CHANNEL_COMBOS missing/empty — set TEST_CHANNEL_COMBO by hand",
                    priority="action_needed",
                )

        # MIN_CELL_CUTOFFS (cluster): classes the operator did not name take the "all" cutoff
        cut_rows = [r for r in c1c_rows if r.get("param_name") == "MIN_CELL_CUTOFFS"]
        if phase == "cluster" and cut_rows:
            from brieflow_well_annotations import fill_min_cell_cutoffs

            filled = fill_min_cell_cutoffs(analysis_dir, cut_rows[0])
            if filled:
                notify(
                    analysis_dir,
                    tag,
                    f"cluster: MIN_CELL_CUTOFFS gives {', '.join(filled)} the 'all' cutoff",
                )

    # I1: patch filled interview values into the notebook before sweep.
    if nb_path.exists():
        for row in iv.get("params", []):
            if row.get("notebook") != phase:
                continue
            # A resolved None (value_source set) is written too, so it replaces an older value.
            unresolved = row.get("value") is None and row.get("value_source") is None
            if unresolved or not ctx["is_active"](row, iv):
                continue
            try:
                ctx["patch_marker_block_value"](nb_path, row["param_name"], repr(row["value"]))
            except (KeyError, ValueError):
                pass

    # Parameter sweeps (adaptive coarse-to-fine or fixed band).
    if nb_path.exists():
        # Resume-safe: skip a band param already tuned-confirmed (a prior run swept
        # it but the phase didn't finish). Avoids re-paying the long tuning grid on
        # every relaunch — mirrors the runner's "completed phases skip" semantics.
        _confirmed = {
            r.get("param_name")
            for r in iv.get("params", [])
            if r.get("notebook") == phase
            and r.get("value") is not None
            and r.get("value_source") == "tuned-confirmed"
            # Don't skip a param whose recorded winner is NaN (a prior crashed
            # sweep): re-sweep it instead of reusing the garbage. (OUTSTANDING #12)
            and not (isinstance(r.get("value"), float) and r.get("value") != r.get("value"))
        }
        # Joint sweep: params listed in lab sweep_joint[phase] interact (e.g.
        # PEAK_WIDTH rescales the peak score THRESHOLD_READS gates on), so they are
        # optimized as a pair on one live kernel per tile — exhaustive product of
        # their coarse bands plus a local refine, no re-segmentation. Falls back to
        # the sequential per-param sweeps below if the joint route is absent/fails.
        joint_done: set = set()
        # A winner below the evidence floor is never recorded; the phase pauses instead.
        evidence = ctx.get("sweep_evidence") or (lambda df, winner=None: (True, ""))
        unresolved: tuple[str, str] | None = None
        joint_names = list((lab.get("sweep_joint") or {}).get(phase) or [])
        joint_fn = ctx.get("joint_marimo_sweep_multitile")
        if (
            joint_names
            and joint_fn is not None
            and sweep_tiles
            and not (set(joint_names) & _confirmed)
        ):
            specs = {p: ctx["lab_sweep_spec"](lab, phase, p) for p in joint_names}
            if all(isinstance(s, dict) and s.get("coarse") for s in specs.values()):
                work_dir = (
                    analysis_dir
                    / ".brieflow"
                    / "sweeps"
                    / phase
                    / ("joint_" + "_".join(joint_names))
                )
                work_dir.mkdir(parents=True, exist_ok=True)
                try:
                    winners, jdf = asyncio.run(
                        joint_fn(
                            nb_path,
                            specs,
                            sweep_tiles,
                            work_dir=work_dir,
                            conda_env=conda_env,
                            max_parallel_kernels=max_parallel_kernels,
                            timeout_s=kernel_timeout_s,
                        )
                    )
                    pair = tuple(winners[p] for p in joint_names)
                    ok, detail = evidence(jdf, pair)
                    if not ok:
                        unresolved = (f"joint {joint_names}", detail)
                        winners = {}
                    for p, w in winners.items():
                        if hasattr(w, "item"):
                            w = w.item()
                        ctx["patch_marker_block_value"](nb_path, p, repr(w))
                        ctx["record_tuned_confirmed"](iv, p, w, notebook=phase)
                    joint_done = set(winners)
                    notify(
                        analysis_dir,
                        tag,
                        f"{phase}/joint {joint_names}: winners={winners} (from {len(jdf)} pairs)",
                    )
                except Exception as e:
                    notify(
                        analysis_dir,
                        tag,
                        f"{phase}/joint {joint_names}: joint sweep failed ({e}) — "
                        "falling back to sequential per-param sweeps",
                    )

        for param in phase_sweep_bands:
            if unresolved:
                break
            if param in joint_done:
                continue
            if param in _confirmed:
                notify(
                    analysis_dir,
                    tag,
                    f"{phase}/{param}: already tuned-confirmed — skipping sweep (resume)",
                )
                continue
            spec = ctx["lab_sweep_spec"](lab, phase, param)
            adaptive = isinstance(spec, dict) and spec.get("coarse")
            band = ctx["lab_sweep_band"](lab, phase, param)
            if not band:
                continue
            if not sweep_tiles:
                notify(
                    analysis_dir, tag, f"{phase}/{param}: no sweep tiles resolved — skipping sweep"
                )
                continue
            work_dir = analysis_dir / ".brieflow" / "sweeps" / phase / param
            work_dir.mkdir(parents=True, exist_ok=True)
            try:
                if adaptive:
                    # Prefer the reactive (live-kernel) coarse-to-fine sweep: it
                    # segments each tile ONCE and mutates only the downstream
                    # marker, instead of re-segmenting per (value, tile). Fall
                    # back to the headless subprocess path when it's not wired
                    # (ctx lacks it) or the live-kernel route is unavailable in
                    # this env (it raises when no kernel comes up) — the headless
                    # path keeps its own failure isolation.
                    reactive = ctx.get("adaptive_marimo_sweep_multitile")
                    if reactive is not None:
                        try:
                            df = asyncio.run(
                                reactive(
                                    nb_path,
                                    param,
                                    spec,
                                    sweep_tiles,
                                    work_dir=work_dir,
                                    conda_env=conda_env,
                                    max_parallel_kernels=max_parallel_kernels,
                                    timeout_s=kernel_timeout_s,
                                )
                            )
                        except Exception as re_err:
                            notify(
                                analysis_dir,
                                tag,
                                f"{phase}/{param}: reactive sweep unavailable ({re_err}) "
                                "— falling back to headless subprocess sweep",
                            )
                            df = asyncio.run(
                                ctx["adaptive_sweep_headless"](
                                    nb_path,
                                    param,
                                    spec,
                                    sweep_tiles,
                                    work_dir=work_dir,
                                    conda_env=conda_env,
                                )
                            )
                    else:
                        df = asyncio.run(
                            ctx["adaptive_sweep_headless"](
                                nb_path,
                                param,
                                spec,
                                sweep_tiles,
                                work_dir=work_dir,
                                conda_env=conda_env,
                            )
                        )
                else:
                    df = asyncio.run(
                        ctx["sweep_headless"](
                            nb_path,
                            param,
                            band,
                            sweep_tiles,
                            work_dir=work_dir,
                            conda_env=conda_env,
                        )
                    )
                if not df.empty:
                    winner = ctx["pick_sweep_winner"](df)
                    # pick_sweep_winner returns a numpy scalar; coerce to native so
                    # repr() emits "200" not "np.int64(200)" — otherwise the notebook
                    # config-write yaml.dumps a numpy object tag that the next
                    # safe_load (sweep cells + config step) rejects, cascading to
                    # nan winners + "config not written".
                    if hasattr(winner, "item"):
                        winner = winner.item()
                    ok, detail = evidence(df, winner)
                    if not ok:
                        unresolved = (param, detail)
                        continue
                    ctx["patch_marker_block_value"](nb_path, param, repr(winner))
                    ctx["record_tuned_confirmed"](iv, param, winner, notebook=phase)
                    notify(
                        analysis_dir,
                        tag,
                        f"{phase}/{param}: sweep winner={winner} (from {len(df)} values)",
                    )
            except Exception as e:
                notify(
                    analysis_dir,
                    tag,
                    f"{phase}/{param}: sweep failed ({e}) — proceeding with existing value",
                )

    # Persist any tuned-confirmed provenance written above.
    ctx["save_interview"](iv, iv_path)
    if nb_path.exists() and unresolved:
        reason = (
            f"{phase}/{unresolved[0]}: sweep winner not backed by data ({unresolved[1]}) — "
            "parameter left unresolved; rerun the sweep where it can finish (e.g. inside a "
            "Slurm allocation) or set the value by hand"
        )
        return {"paused": True, "reason": reason}

    # N0 (cluster): FINAL_LEIDEN_RESOLUTIONS is agent-resolved — run the notebook,
    # parse the resolution table, select near-target resolutions, patch the marker.
    if phase == "cluster":
        final = ctx["resolve_cluster_final"](analysis_dir, conda_env, lab, iv, iv_path)
        if not final:
            return {
                "paused": True,
                "reason": (
                    "cluster: could not parse a resolution table to select "
                    "FINAL_LEIDEN_RESOLUTIONS — check the cluster notebook run"
                ),
            }
        notify(
            analysis_dir,
            tag,
            f"cluster: FINAL_LEIDEN_RESOLUTIONS={final} (agent-selected; the decision journal "
            "says whether the genes/cluster target was reached)",
        )

    # N0 (merge fast+auto): INITIAL_SBS_TILES probed from SBS tile geometry. The
    # helper short-circuits on operator override / positions / manual approach.
    if phase == "merge":
        tiles = ctx["resolve_merge_initial_sbs_tiles"](analysis_dir, lab, iv, iv_path)
        if tiles:
            notify(
                analysis_dir, tag, f"merge: INITIAL_SBS_TILES={tiles} (auto-probed, well-spread)"
            )
        # DET_RANGE auto-probed from the ph/sbs pixel-size ratio.
        det = ctx["resolve_merge_det_range"](analysis_dir, lab, iv, iv_path)
        if det:
            notify(
                analysis_dir,
                tag,
                f"merge: DET_RANGE={det} (auto-probed from the ph/sbs pixel-size ratio)",
            )

    return {"paused": False, "reason": None}
