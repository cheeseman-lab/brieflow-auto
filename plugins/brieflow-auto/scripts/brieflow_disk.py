#!/usr/bin/env python3
"""Per-phase disk-requirement estimate + pre-launch availability check.
Companion to the mem resolver: a formula over probe_geometry output.
Constants calibrated to the snowdon ~19.3 TB run (2026-05) — lab-tunable."""
from __future__ import annotations
import shutil

# GB per geometry unit, per phase. Calibrated to snowdon ~19.3 TB observed sizes.
_FORMULAS = {
    # NOTE: n_wells = distinct (plate, well) PAIRS (brieflow_screen_geometry:
    # len({(plate, well)})), so it already spans plates — do NOT also multiply by
    # n_plates (that triple-counts plates; damavand's 3 plates → 3x over). Total
    # work units = n_wells * tiles_per_well.
    # preprocess zarr: wells*(ph+sbs tiles)*channels ~ dominant
    "preprocess": lambda g: _u(g, "n_wells") * (
        _u(g, "n_tiles_per_well_phenotype") * _u(g, "n_channels_per_round_phenotype") * _u(g, "n_rounds_phenotype")
        + _u(g, "n_tiles_per_well_sbs") * _u(g, "n_channels_per_cycle_sbs") * _u(g, "n_cycles")
    ) * 0.023,  # ~23 MB/channel-tile  # lab-tunable, calibrated to snowdon ~19.3 TB
    "sbs": lambda g: _u(g, "n_wells") * _u(g, "n_tiles_per_well_sbs") * 0.95,  # lab-tunable, calibrated to snowdon ~19.3 TB
    "phenotype": lambda g: _u(g, "n_wells") * _u(g, "n_tiles_per_well_phenotype") * 0.28,  # lab-tunable, calibrated to snowdon ~19.3 TB
    "merge": lambda g: _u(g, "n_wells") * 1.67,  # lab-tunable, calibrated to snowdon ~19.3 TB
    "aggregate": lambda g: _u(g, "n_wells") * _u(g, "n_channel_combos") * 1.08,  # lab-tunable, calibrated to snowdon ~19.3 TB
    "cluster": lambda g: 5.0,  # modest, single-digit GB  # lab-tunable, calibrated to snowdon ~19.3 TB
}


def _u(g: dict, key: str) -> float:
    v = g.get(key)
    return float(v) if v else 0.0


def compute_disk_requirements(geometry: dict) -> dict:
    breakdown, missing = {}, []
    for phase, f in _FORMULAS.items():
        gb = round(f(geometry), 2)
        breakdown[phase] = gb
        if gb == 0 and phase != "cluster":
            missing.append(phase)
    return {
        "required_gb": round(sum(breakdown.values()), 2),
        "phase_breakdown": breakdown,
        "geometry": geometry,
        "under_estimated_phases": missing,
    }


def check_disk_available(required_gb: float, path, headroom: float = 1.2) -> dict:
    """Returns ok=False (not raises) when path does not exist or is unreadable."""
    need = required_gb * headroom
    try:
        free_gb = shutil.disk_usage(str(path)).free / 1e9
    except OSError as e:
        return {
            "ok": False,
            "error": str(e),
            "required_gb": round(required_gb, 2),
            "available_gb": None,
            "needed_with_headroom_gb": round(need, 2),
            "headroom": headroom,
        }
    return {
        "ok": free_gb >= need,
        "required_gb": round(required_gb, 2),
        "needed_with_headroom_gb": round(need, 2),
        "available_gb": round(free_gb, 2),
        "headroom": headroom,
    }
