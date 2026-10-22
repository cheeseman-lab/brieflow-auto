#!/usr/bin/env python3
"""screen.yaml contract: the keys the plugin reads, the experimental facts an operator must
supply, and staging an operator's screen.yaml into an analysis repository.

    brieflow_screen_yaml.py check --analysis-dir <dir>     # exit 3 and list what is missing
    brieflow_screen_yaml.py find  --analysis-dir <dir> [--candidate PATH ...]
    brieflow_screen_yaml.py stage --analysis-dir <dir> --source <screen.yaml>

screen.yaml is written by the operator, never by the agent. A field left blank here is a
fact only the operator knows, so setup and the runner stop and name it instead of asking
for it piecemeal or guessing it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

# Dotted paths the plugin reads (interview derivations, computed derivations, preflight,
# probes, runner). tests/test_screen_yaml_contract.py holds this equal to the code and to the
# canonical template's keys.
READ_KEYS = (
    "collection.screen_id",
    "experiment.organism_ontology_term_id",
    "cellular.well_annotations",
    "cellular.split_by",
    "cellular.group_by",
    "cellular.reference_group",
    "library.type",
    "library.detection_target",
    "library.raw_design_table_fp",
    "library.barcode_col",
    "library.barcode_map_col",
    "library.barcode_recomb_col",
    "library.barcode_length_map",
    "library.barcode_length_recomb",
    "library.sequencing_order",
    "library.gene_symbol_col",
    "library.gene_id_col",
    "library.negative_controls",
    "library.skip_cycles",
    "iss.cycles",
    "iss.chemistry",
    "iss.combinatorial.code",
    "iss.channels",
    "iss.background_channel_nucleus.cycle",
    "iss.background_channel_cell.channel",
    "iss.background_channel_cell.cycle",
    "iss.plate_overrides",
    "phenotype.objective",
    "phenotype.binning",
    "phenotype.channels",
    "phenotype.channels_imaged_together",
    "phenotype.segment_cells",
    "phenotype.background_channel_cell",
    "phenotype.foci_channel",
    "phenotype.second_obj_channel",
    "pipeline.conda_env",
    "data.input_sbs_dir",
    "data.input_phenotype_dir",
    "additional_notes",
)

# Template keys the plugin does not read; brieflow's annotation context and archives may.
DESCRIPTIVE_KEYS = (
    "collection.title",
    "collection.publication_doi",
    "experiment.screen_title",
    "experiment.organism",
    "experiment.tissue_ontology_term_id",
    "experiment.tissue",
    "experiment.tissue_type",
    "experiment.disease_ontology_term_id",
    "experiment.disease",
    "experiment.development_stage_ontology_term_id",
    "experiment.development_stage",
    "experiment.assay_ontology_term_id",
    "experiment.assay",
    "experiment.pseudobulk",
    "cellular.growth_conditions",
    "cellular.plate_type",
    "cellular.imaging_timepoint",
    "cellular.seeding.density_cells_cm2",
    "cellular.induction.duration",
    "library.vector",
    "library.gene_selection",
    "library.positive_controls",
    "library.number_of_genes",
    "iss.objective",
    "iss.binning",
    "iss.cycle_order",
    "iss.background_channel_nucleus.channel",
    "phenotype.background_channel_nucleus",
    "microscope.system",
    "pipeline.github",
    "pipeline.version",
    "data.storage_format",
    "data.location",
)

# Experimental facts with no derivation: blank means the operator has not filled them in.
REQUIRED_KEYS = (
    "library.type",
    "library.detection_target",
    "library.raw_design_table_fp",
    "library.gene_symbol_col",
    "iss.cycles",
    "iss.chemistry",
    "pipeline.conda_env",
    "data.input_sbs_dir",
    "data.input_phenotype_dir",
)
REQUIRED_SIMPLE = ("library.barcode_col",)
REQUIRED_MULTI = (
    "library.barcode_map_col",
    "library.barcode_recomb_col",
    "library.barcode_length_map",
    "library.barcode_length_recomb",
    "library.sequencing_order",
)


def missing_required(screen: dict) -> list[str]:
    """Required facts still blank in a parsed screen.yaml, as dotted paths in template order."""
    screen = screen or {}
    missing = [k for k in REQUIRED_KEYS if _blank(_lookup(screen, k))]
    lib_type = str(_lookup(screen, "library.type") or "").strip().lower()
    if lib_type == "simple":
        missing += [k for k in REQUIRED_SIMPLE if _blank(_lookup(screen, k))]
    elif lib_type == "multi":
        missing += [k for k in REQUIRED_MULTI if _blank(_lookup(screen, k))]
    chemistry = str(_lookup(screen, "iss.chemistry") or "").strip().lower()
    if chemistry == "combinatorial" and _blank(_lookup(screen, "iss.combinatorial.code")):
        missing.append("iss.combinatorial.code")
    sbs = [c for c in (_lookup(screen, "iss.channels") or []) if isinstance(c, dict)]
    if not sbs or any(_blank(c.get("name")) for c in sbs):
        missing.append("iss.channels[].name")
    ph = [c for c in (_lookup(screen, "phenotype.channels") or []) if isinstance(c, dict)]
    if not ph or any(_blank(c.get("biological_name")) for c in ph):
        missing.append("phenotype.channels[].biological_name")
    return missing


def check(analysis_dir: Path) -> dict:
    """{"path", "exists", "parses", "missing"} for <analysis_dir>/screen.yaml."""
    path = Path(analysis_dir) / "screen.yaml"
    out = {"path": str(path), "exists": path.is_file(), "parses": False, "missing": []}
    if not out["exists"]:
        out["missing"] = ["screen.yaml"]
        return out
    try:
        screen = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        out["error"] = str(e)
        return out
    out["parses"] = True
    out["missing"] = missing_required(screen)
    return out


def find_operator_screen_yaml(analysis_dir: Path, candidates: list[Path] = ()) -> list[Path]:
    """Filled-in screen.yaml files the operator may have brought, best first.

    Looks at the given candidates, the repository root above analysis/, and the current
    directory; the template copy in analysis/ counts only when it is already filled in."""
    analysis_dir = Path(analysis_dir).resolve()
    seen, found = set(), []
    for p in [*map(Path, candidates), analysis_dir.parent / "screen.yaml", Path.cwd() / "screen.yaml",
              analysis_dir / "screen.yaml"]:
        p = p.resolve()
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        try:
            screen = yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError:
            continue
        if not missing_required(screen):
            found.append(p)
    return found


def stage(source: Path, analysis_dir: Path) -> list[Path]:
    """Copy the operator's screen.yaml to <analysis_dir>/screen.yaml, keeping the old file as
    screen.yaml.template-<timestamp>, and bring along the files it names by relative path
    (cellular.well_annotations, library.raw_design_table_fp) when they sit beside the source
    and are not already in place. Returns the paths written."""
    source, analysis_dir = Path(source).resolve(), Path(analysis_dir).resolve()
    dest = analysis_dir / "screen.yaml"
    written = []
    if source != dest:
        if dest.exists():
            dest.rename(dest.with_name(f"screen.yaml.template-{datetime.now():%Y%m%d%H%M%S}"))
        shutil.copy2(source, dest)
        written.append(dest)
    screen = yaml.safe_load(dest.read_text()) or {}
    for key in ("cellular.well_annotations", "library.raw_design_table_fp"):
        rel = _lookup(screen, key)
        if not rel or Path(rel).is_absolute():
            continue
        src, dst = source.parent / rel, analysis_dir / rel
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            written.append(dst)
    return written


def _lookup(obj, dotted: str):
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _blank(value) -> bool:
    return value is None or (isinstance(value, (str, list, dict)) and len(value) == 0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("check", "find", "stage"):
        s = sub.add_parser(name)
        s.add_argument("--analysis-dir", type=Path, required=True)
        if name == "find":
            s.add_argument("--candidate", type=Path, action="append", default=[])
        if name == "stage":
            s.add_argument("--source", type=Path, required=True)
    args = p.parse_args(argv)
    if args.cmd == "check":
        report = check(args.analysis_dir)
        print(json.dumps(report, indent=2))
        return 0 if report["parses"] and not report["missing"] else 3
    if args.cmd == "find":
        found = find_operator_screen_yaml(args.analysis_dir, args.candidate)
        print(json.dumps([str(f) for f in found], indent=2))
        return 0 if found else 3
    for path in stage(args.source, args.analysis_dir):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
