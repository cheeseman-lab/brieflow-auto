#!/usr/bin/env python3
"""
brieflow_resolve_resources.py — Compute per-rule mem_mb from screen
geometry + scaling formulas, emit snakemake --set-resources lines.

HARDENING_TODO #23. Resolution priority (high → low):
  1. analysis/slurm/mem_recommendations.json (manual_override + observation-derived)
  2. scaling_formulas.yaml × geometry.json (formula-evaluated)
  3. analysis/slurm/config.yaml set-resources: (legacy hand-curated floor)
  4. snakemake default mem_mb (passthrough)

A rule is resolved at exactly ONE layer — the first that produces a
positive mem_mb. Subsequent layers don't override.

Closed-enum scaling labels — each maps to a single geometry field:
  constant                       — flat mem_mb
  per_tile                       — N = 1
  per_well_sbs                   — N = n_tiles_per_well_sbs
  per_well_phenotype             — N = n_tiles_per_well_phenotype
  per_well_per_cycle_sbs         — N = n_tiles_per_well_per_cycle_sbs
  per_well_per_round_phenotype   — N = n_tiles_per_well_per_round_phenotype
  per_well_per_combo             — N = n_channel_combos (proxy until cell-level
                                       per-combo counts are probed)
  per_perturbation               — N = sgRNAs_per_gene_max
  per_plate                      — N = n_plates

mem_mb = (k_mb * N + b_mb) * margin    for linear scalings
mem_mb = mem_mb * margin                for constant, and per_tile given as mem_mb
N is scaled by sample_fraction / sample_fraction_ref when an entry sets sample_fraction_ref
(preprocess.sample_fraction from config/config.yml).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError as e:
    print(f"ERROR: yaml not installed: {e}", file=sys.stderr)
    sys.exit(1)


SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_BASELINE = SCRIPT_DIR.parent / "data" / "scaling_formulas_baseline.yaml"


# ---------------------------------------------------------------------------
# Scaling enum → geometry field
# ---------------------------------------------------------------------------


SCALING_DIM_FIELD: dict[str, str | None] = {
    "constant": None,
    "per_tile": None,  # N = 1
    "per_well_sbs": "n_tiles_per_well_sbs",
    "per_well_phenotype": "n_tiles_per_well_phenotype",
    "per_well_per_cycle_sbs": "n_tiles_per_well_per_cycle_sbs",
    "per_well_per_round_phenotype": "n_tiles_per_well_per_round_phenotype",
    "per_well_per_combo": "n_channel_combos",
    "per_perturbation": "sgRNAs_per_gene_max",
    "per_plate": "n_plates",
}


VALID_SCALINGS = set(SCALING_DIM_FIELD.keys())


def scaling_dim(scaling: str, geometry: dict) -> int | None:
    """Return N for a scaling label given geometry, or None if unresolvable.

    `constant` → returns None (caller skips N×k+b path).
    `per_tile` → returns 1.
    Other labels → look up the geometry field.
    """
    if scaling == "constant":
        return None
    if scaling == "per_tile":
        return 1
    field = SCALING_DIM_FIELD.get(scaling)
    if field is None:
        return None
    val = geometry.get(field)
    if val is None:
        return None
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


# ---------------------------------------------------------------------------
# Formula evaluation
# ---------------------------------------------------------------------------


def evaluate_formula(entry: dict, geometry: dict) -> int | None:
    """Return resolved mem_mb for one rule entry, or None if unresolvable."""
    scaling = entry.get("scaling")
    if scaling not in VALID_SCALINGS:
        return None
    margin = float(entry.get("margin", 1.0))

    # per_tile rules are written as a flat mem_mb (N = 1); only constant read it before.
    if scaling == "constant" or (scaling == "per_tile" and "k_mb" not in entry):
        m = entry.get("mem_mb")
        if m is None:
            return None
        return int(float(m) * margin)

    n = scaling_dim(scaling, geometry)
    if n is None:
        return None
    ref = entry.get("sample_fraction_ref")
    fraction = geometry.get("sample_fraction")
    if ref and fraction:
        n = n * float(fraction) / float(ref)
    k = float(entry.get("k_mb", 0))
    b = float(entry.get("b_mb", 0))
    raw = k * n + b
    if raw <= 0:
        return None
    return int(raw * margin)


# ---------------------------------------------------------------------------
# Layer loaders
# ---------------------------------------------------------------------------


def load_mem_recs(analysis_dir: Path) -> dict:
    """Read mem_recommendations.json. Prefer slurm/ canonical; legacy locations
    accepted for backwards-compat."""
    for cand in (
        analysis_dir / "slurm" / "mem_recommendations.json",
        analysis_dir / "cluster_optimization" / "results" / "mem_recommendations.json",
    ):
        if cand.exists():
            try:
                with open(cand) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
    return {}


def load_formulas(analysis_dir: Path) -> dict:
    """Read scaling_formulas.yaml. Prefer analysis-side override; fall back
    to plugin baseline."""
    for cand in (analysis_dir / "slurm" / "scaling_formulas.yaml", PLUGIN_BASELINE):
        if cand.exists():
            try:
                with open(cand) as f:
                    data = yaml.safe_load(f) or {}
                return data
            except (yaml.YAMLError, OSError):
                continue
    return {}


def load_geometry(analysis_dir: Path) -> dict:
    """Read geometry.json. Empty dict if missing (resolver tolerates)."""
    p = analysis_dir / ".brieflow" / "geometry.json"
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_sample_fraction(analysis_dir: Path) -> float | None:
    """preprocess.sample_fraction from config/config.yml (None if absent)."""
    p = analysis_dir / "config" / "config.yml"
    try:
        cfg = yaml.safe_load(p.read_text()) or {}
        value = (cfg.get("preprocess") or {}).get("sample_fraction")
        return float(value) if value else None
    except (OSError, yaml.YAMLError, TypeError, ValueError, AttributeError):
        return None


def load_slurm_floor(analysis_dir: Path) -> dict[str, int]:
    """Read analysis/slurm/config.yaml's set-resources: section for the
    floor layer."""
    p = analysis_dir / "slurm" / "config.yaml"
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            cfg = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return {}
    floor = cfg.get("set-resources") or {}
    out: dict[str, int] = {}
    for rule, res in floor.items():
        if isinstance(res, dict) and "mem_mb" in res:
            try:
                out[rule] = int(res["mem_mb"])
            except (TypeError, ValueError):
                continue
    return out


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve(analysis_dir: Path) -> dict[str, dict]:
    """Resolve mem_mb for every rule reachable from the layered config.

    Returns {rule: {mem_mb, source}} where source is one of:
      manual_override | observation | formula | slurm_floor
    Rules not mentioned in any layer are omitted (snakemake default applies).
    """
    mem_recs = load_mem_recs(analysis_dir)
    formulas_data = load_formulas(analysis_dir)
    geometry = load_geometry(analysis_dir)
    fraction = load_sample_fraction(analysis_dir)
    if fraction:
        geometry = {**geometry, "sample_fraction": fraction}
    floor = load_slurm_floor(analysis_dir)

    formula_rules = formulas_data.get("rules") or {}

    resolved: dict[str, dict] = {}
    rule_universe = set(mem_recs.keys()) | set(formula_rules.keys()) | set(floor.keys())

    for rule in sorted(rule_universe):
        # Layer 1: mem_recs (manual_override or observation-derived).
        rec = mem_recs.get(rule) or {}
        rec_mem = rec.get("mem_mb_recommended")
        if rec_mem is not None and int(rec_mem) > 0:
            src = (
                "manual_override"
                if any(
                    m in (rec.get("obs_source") or "")
                    for m in ("manual_override", "manual_from_sacct", "manual_post_oom")
                )
                else "observation"
            )
            resolved[rule] = {"mem_mb": int(rec_mem), "source": src}
            continue

        # Layer 2: formula.
        entry = formula_rules.get(rule)
        if entry:
            m = evaluate_formula(entry, geometry)
            if m is not None and m > 0:
                resolved[rule] = {
                    "mem_mb": m,
                    "source": "formula",
                    "scaling": entry.get("scaling"),
                }
                continue

        # Layer 3: slurm config.yaml set-resources floor.
        if rule in floor:
            resolved[rule] = {"mem_mb": floor[rule], "source": "slurm_floor"}
            continue

        # Layer 4: nothing — snakemake default applies.

    return resolved


def formula_caps(analysis_dir: Path) -> dict[str, int]:
    """Formula-layer mem_mb per rule (geometry x baseline/override), ignoring the other layers."""
    geometry = load_geometry(analysis_dir)
    fraction = load_sample_fraction(analysis_dir)
    if fraction:
        geometry = {**geometry, "sample_fraction": fraction}
    caps = {}
    for rule, entry in (load_formulas(analysis_dir).get("rules") or {}).items():
        m = evaluate_formula(entry, geometry) if isinstance(entry, dict) else None
        if m:
            caps[rule] = m
    return caps


def to_set_resources_args(resolved: dict[str, dict]) -> list[str]:
    """Format resolved caps as snakemake `--set-resources rule:mem_mb=N` args."""
    out: list[str] = []
    for rule, meta in sorted(resolved.items()):
        out.append(f"{rule}:mem_mb={meta['mem_mb']}")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--analysis-dir", required=True, type=Path)
    p.add_argument(
        "--format",
        choices=["snakemake", "json", "table"],
        default="snakemake",
        help="Output format: snakemake (--set-resources args, "
        "default), json (full resolved dict), table (human).",
    )
    p.add_argument(
        "--rule", default=None, help="Limit output to this single rule (debug)."
    )
    args = p.parse_args()

    analysis_dir = args.analysis_dir.resolve()
    if not analysis_dir.exists():
        print(f"ERROR: --analysis-dir {analysis_dir} does not exist.", file=sys.stderr)
        return 2

    resolved = resolve(analysis_dir)
    if args.rule:
        resolved = {args.rule: resolved[args.rule]} if args.rule in resolved else {}

    if args.format == "json":
        print(json.dumps(resolved, indent=2, sort_keys=True))
    elif args.format == "table":
        print(f"{'Rule':<32} {'mem_mb':>10}  source           scaling")
        print("-" * 80)
        for rule, meta in sorted(resolved.items()):
            print(
                f"{rule:<32} {meta['mem_mb']:>10}  {meta['source']:<15}  "
                f"{meta.get('scaling', '')}"
            )
        print(f"\n  {len(resolved)} rules resolved.")
    else:  # snakemake
        args_list = to_set_resources_args(resolved)
        if args_list:
            print(" ".join(args_list))
    return 0


if __name__ == "__main__":
    sys.exit(main())
