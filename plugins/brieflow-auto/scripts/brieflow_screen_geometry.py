#!/usr/bin/env python3
"""
brieflow_screen_geometry.py — One-shot probe of a screen's geometry.

HARDENING_TODO #23 prerequisite. Walks the analysis dir's config + input
trees and emits `<analysis-dir>/.brieflow/geometry.json` — a dict of
scalar counts that the scaling-formula resolver evaluates against to
size each rule's mem cap.

Probe sources (in priority order):
  1. analysis/config/sbs_combo.tsv and phenotype_combo.tsv — these are the
     canonical (plate, well, cycle|round, channel, tile) tables brieflow
     uses internally; they're the most accurate source for tile/well/
     cycle counts.
  2. analysis/config/config.yml — channel-combo definitions, sgRNA design
     table paths.
  3. sgRNA design table file (if pointed to by config) — library size.

Idempotent: rerunning overwrites geometry.json. Gitignored by convention.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError as e:
    print(f"ERROR: yaml not installed: {e}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------


def _read_combo_tsv(path: Path) -> list[dict]:
    """Read a (plate, well, cycle|round, channel, tile) combo TSV."""
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path) as f:
        for row in csv.DictReader(f, delimiter="\t"):
            rows.append(row)
    return rows


def _unique(rows: list[dict], key: str) -> set:
    return {r[key] for r in rows if r.get(key)}


def _max_tiles_per_well(rows: list[dict], group_key: str | None = None) -> int:
    """Compute the maximum (tile count) across (plate, well) or (plate, well, group_key).

    For SBS the group_key is `cycle`; for phenotype it's `round`. Counts
    distinct tiles per (plate, well) when group_key is None.
    """
    by: dict[tuple, set] = {}
    for r in rows:
        if not r.get("tile"):
            continue
        if group_key:
            k = (r.get("plate"), r.get("well"), r.get(group_key))
        else:
            k = (r.get("plate"), r.get("well"))
        by.setdefault(k, set()).add(r["tile"])
    if not by:
        return 0
    return max(len(t) for t in by.values())


def _max_tiles_per_well_per_round(rows: list[dict], group_key: str) -> int:
    """For per_well_per_round / per_well_per_cycle scaling — peak tile
    count for a single (plate, well, round) bucket."""
    by: dict[tuple, set] = {}
    for r in rows:
        if not r.get("tile") or not r.get(group_key):
            continue
        k = (r.get("plate"), r.get("well"), r.get(group_key))
        by.setdefault(k, set()).add(r["tile"])
    if not by:
        return 0
    return max(len(t) for t in by.values())


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------


def probe_geometry(analysis_dir: Path, config_path: Path) -> dict:
    """Walk the analysis dir + config and return a geometry dict.

    Best-effort: any field that can't be computed lands as None rather
    than failing. Resolver tolerates partial geometry.
    """
    geo: dict = {
        "_schema_version": 1,
        "_probed_at": datetime.now().isoformat(timespec="seconds"),
        "_analysis_dir": str(analysis_dir),
        "_config_path": str(config_path),
    }

    # Load config — soft-required.
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        cfg = {}

    # Probe SBS combo TSV.
    sbs_combo_fp = analysis_dir / cfg.get("preprocess", {}).get(
        "sbs_combo_fp", "config/sbs_combo.tsv"
    )
    sbs_rows = _read_combo_tsv(sbs_combo_fp)
    geo["sbs_combo_path"] = str(sbs_combo_fp)
    if sbs_rows:
        geo["n_plates"] = len(_unique(sbs_rows, "plate"))
        geo["n_wells"] = len({(r.get("plate"), r.get("well")) for r in sbs_rows})
        geo["n_cycles"] = len(_unique(sbs_rows, "cycle"))
        geo["n_channels_per_cycle_sbs"] = len(_unique(sbs_rows, "channel"))
        geo["n_tiles_per_well_sbs"] = _max_tiles_per_well(sbs_rows, group_key=None)
        geo["n_tiles_per_well_per_cycle_sbs"] = _max_tiles_per_well_per_round(
            sbs_rows, "cycle"
        )
    else:
        geo["n_plates"] = None
        geo["n_wells"] = None
        geo["n_cycles"] = None
        geo["n_channels_per_cycle_sbs"] = None
        geo["n_tiles_per_well_sbs"] = None
        geo["n_tiles_per_well_per_cycle_sbs"] = None

    # Probe phenotype combo TSV.
    ph_combo_fp = analysis_dir / cfg.get("preprocess", {}).get(
        "phenotype_combo_fp", "config/phenotype_combo.tsv"
    )
    ph_rows = _read_combo_tsv(ph_combo_fp)
    geo["phenotype_combo_path"] = str(ph_combo_fp)
    if ph_rows:
        # Cross-check plate/well counts against sbs; phenotype may be authoritative.
        ph_plates = len(_unique(ph_rows, "plate"))
        ph_wells = len({(r.get("plate"), r.get("well")) for r in ph_rows})
        if geo["n_plates"] is None:
            geo["n_plates"] = ph_plates
        if geo["n_wells"] is None:
            geo["n_wells"] = ph_wells
        geo["n_rounds_phenotype"] = len(_unique(ph_rows, "round"))
        geo["n_channels_per_round_phenotype"] = len(_unique(ph_rows, "channel"))
        geo["n_tiles_per_well_phenotype"] = _max_tiles_per_well(ph_rows, group_key=None)
        geo["n_tiles_per_well_per_round_phenotype"] = _max_tiles_per_well_per_round(
            ph_rows, "round"
        )
    else:
        geo["n_rounds_phenotype"] = None
        geo["n_channels_per_round_phenotype"] = None
        geo["n_tiles_per_well_phenotype"] = None
        geo["n_tiles_per_well_per_round_phenotype"] = None

    # Channel combos (phenotype side, from config).
    ph_channels = cfg.get("preprocess", {}).get("phenotype_channels_metadata", []) or []
    geo["n_channels_phenotype"] = len(ph_channels) if ph_channels else None

    # Channel-combo count — used by per_well_per_combo rules
    # (format_singlecell_anndata, phate_leiden_clustering, etc.). Probe from
    # `aggregate.channel_combos` config if present; otherwise leave None.
    agg = cfg.get("aggregate", {}) or {}
    channel_combos = agg.get("channel_combos") or agg.get("dataset_combos") or []
    if isinstance(channel_combos, list) and channel_combos:
        geo["n_channel_combos"] = len(channel_combos)
    else:
        # Cluster phase config commonly carries the combos. Try there too.
        cluster = cfg.get("cluster", {}) or {}
        channel_combos = cluster.get("channel_combos") or []
        geo["n_channel_combos"] = len(channel_combos) if channel_combos else None

    # Library size — read perturbation design table (sgRNA file) if config
    # points to one. Try common config keys.
    library_fp = None
    for k in (
        "sgRNA_design_fp",
        "perturbation_table_fp",
        "barcode_table_fp",
        "library_fp",
    ):
        for section in (
            cfg,
            cfg.get("preprocess", {}),
            cfg.get("classify", {}),
            cfg.get("aggregate", {}),
        ):
            v = section.get(k) if isinstance(section, dict) else None
            if v:
                library_fp = analysis_dir / v
                break
        if library_fp:
            break

    if library_fp and library_fp.exists():
        try:
            n_sgRNAs = 0
            n_genes_set: set = set()
            sgRNAs_per_gene: dict[str, int] = {}
            with open(library_fp) as f:
                # Try TSV first, then CSV.
                first_line = f.readline()
                delim = "\t" if "\t" in first_line else ","
                f.seek(0)
                reader = csv.DictReader(f, delimiter=delim)
                # Common gene-id column names.
                gene_col = None
                for cand in (
                    "gene_id",
                    "gene_symbol",
                    "gene",
                    "target_gene",
                    "gene_name",
                ):
                    if cand in (reader.fieldnames or []):
                        gene_col = cand
                        break
                for row in reader:
                    n_sgRNAs += 1
                    if gene_col:
                        g = row.get(gene_col)
                        if g:
                            n_genes_set.add(g)
                            sgRNAs_per_gene[g] = sgRNAs_per_gene.get(g, 0) + 1
            geo["library_n_sgRNAs"] = n_sgRNAs
            geo["library_n_genes"] = len(n_genes_set) if n_genes_set else None
            geo["library_path"] = str(library_fp)
            geo["sgRNAs_per_gene_max"] = (
                max(sgRNAs_per_gene.values()) if sgRNAs_per_gene else None
            )
        except OSError:
            pass

    return geo


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--analysis-dir", required=True, type=Path)
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Config path. Default: <analysis-dir>/config/config.yml",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path. Default: <analysis-dir>/.brieflow/geometry.json",
    )
    p.add_argument(
        "--print",
        action="store_true",
        help="Print to stdout instead of writing to file.",
    )
    args = p.parse_args()

    analysis_dir = args.analysis_dir.resolve()
    if not analysis_dir.exists():
        print(f"ERROR: --analysis-dir {analysis_dir} does not exist.", file=sys.stderr)
        return 2
    config_path = (args.config or analysis_dir / "config" / "config.yml").resolve()

    geometry = probe_geometry(analysis_dir, config_path)

    if args.print:
        print(json.dumps(geometry, indent=2))
        return 0

    out = args.output or (analysis_dir / ".brieflow" / "geometry.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(geometry, indent=2))
    print(f"wrote {out}", file=sys.stderr)
    # Brief summary on stderr for operator visibility.
    summary = {
        k: v
        for k, v in geometry.items()
        if k.startswith("n_") or k.startswith("library_")
    }
    print(f"geometry summary: {json.dumps(summary, indent=2)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
