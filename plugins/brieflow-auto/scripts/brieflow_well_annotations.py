#!/usr/bin/env python3
"""brieflow_well_annotations.py — per-well conditions for grouped aggregation and clustering.

screen.yaml ``cellular.well_annotations`` names a table beside it (TSV, or CSV by extension):
``plate``, ``well``, then one column per per-well variable (seeding density, treatment, the
experiment group). ``cellular.split_by`` names the column whose values each get their own
aggregated dataset and clustering. The aggregate notebook joins the table onto every cell and
the pipeline raises on a well missing from it, so the copy written to ``config/`` holds exactly
the wells to analyze: rows with a value in ``split_by`` and ``group_by``, restricted to wells that reached merge
(a well without a ``merge_final`` parquet would put a missing input into the aggregate DAG).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

OUT_REL = "config/well_annotations.tsv"

# value sources the aggregate hook may rewrite; anything else was set on purpose
DERIVED_SOURCES = (None, "lab-default", "screen-yaml")


def build_well_annotations(analysis_dir: Path, screen_yaml: Path | None = None) -> dict | None:
    """Write ``config/well_annotations.tsv`` from the table screen.yaml names.

    Args:
        analysis_dir: The screen's analysis directory.
        screen_yaml: screen.yaml path; defaults to ``analysis_dir/screen.yaml``.

    Returns:
        None when screen.yaml names no table, else a report ``{"path", "kept", "wells",
        "blank_group", "not_merged", "groups", "split_by"}``; ``wells`` and ``not_merged``
        are sorted ``(plate, well)`` pairs.

    Raises:
        ValueError: The table lacks ``plate``, ``well`` or the split column, or a plate is
            not an integer.
        OSError: The table cannot be read.
    """
    import pandas as pd
    import yaml

    analysis_dir = Path(analysis_dir)
    screen_yaml = Path(screen_yaml) if screen_yaml else analysis_dir / "screen.yaml"
    if not screen_yaml.exists():
        return None
    screen = yaml.safe_load(screen_yaml.read_text()) or {}
    src, split_by = annotation_spec(screen)
    if not src:
        return None
    table_fp = (screen_yaml.parent / src).resolve()
    sep = "," if table_fp.suffix.lower() == ".csv" else "\t"
    table = pd.read_csv(table_fp, sep=sep, dtype=str)
    group_by = (screen.get("cellular") or {}).get("group_by")
    group_cols = list(group_by) if isinstance(group_by, (list, tuple)) else [group_by] if group_by else []
    need = {"plate", "well"} | ({split_by} if split_by else set()) | set(group_cols)
    missing = sorted(need - set(table.columns))
    if missing:
        raise ValueError(f"{table_fp} lacks column(s) {missing}")
    for col in table.columns:
        table[col] = table[col].str.strip()
    try:
        table["plate"] = table["plate"].astype(int)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{table_fp}: every plate must be an integer ({exc})") from exc

    blank = 0
    for col in ([split_by] if split_by else []) + group_cols:
        has_value = table[col].fillna("") != ""
        blank += int((~has_value).sum())
        table = table[has_value]
    root = results_root(analysis_dir)
    merged = [merge_final_path(root, p, w).exists() for p, w in zip(table["plate"], table["well"])]
    not_merged = sorted(
        (int(p), w) for p, w, m in zip(table["plate"], table["well"], merged) if not m
    )
    table = table[merged]

    out = analysis_dir / OUT_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out, sep="\t", index=False)
    return {
        "path": OUT_REL,
        "kept": len(table),
        "wells": sorted((int(p), w) for p, w in zip(table["plate"], table["well"])),
        "blank_group": blank,
        "not_merged": not_merged,
        "groups": sorted(table[split_by].unique()) if split_by and len(table) else [],
        "split_by": split_by,
    }


def resolve_aggregate_annotations(
    analysis_dir: Path, rows: list[dict], build=None, split_default: Any = "class"
) -> dict:
    """Point WELL_ANNOTATIONS_FP and SPLIT_COL at what screen.yaml names now.

    Runs on every aggregate pass so a screen.yaml edited after setup takes effect. A row
    whose value_source is not in ``DERIVED_SOURCES`` was set by the operator and is left
    untouched.

    Args:
        analysis_dir: The screen's analysis directory.
        rows: The aggregate notebook's interview rows, mutated in place.
        build: Stand-in for :func:`build_well_annotations` (tests).
        split_default: SPLIT_COL when screen.yaml names no split (the lab default).

    Returns:
        ``{"pause", "notes", "wells", "clear"}``: ``pause`` is a blocker reason or None,
        ``notes`` progress messages, ``wells`` the kept ``(plate, well)`` pairs (None when
        no table was built) and ``clear`` the params whose notebook marker must go to None.
    """
    import yaml

    build = build or build_well_annotations
    out: dict = {"pause": None, "notes": [], "wells": None, "clear": []}
    by_name = {r.get("param_name"): r for r in rows}
    ann_row, split_row = by_name.get("WELL_ANNOTATIONS_FP"), by_name.get("SPLIT_COL")

    screen_yaml = Path(analysis_dir) / "screen.yaml"
    try:
        screen = (yaml.safe_load(screen_yaml.read_text()) or {}) if screen_yaml.exists() else {}
    except yaml.YAMLError as exc:
        out["pause"] = f"aggregate: config error: screen.yaml does not parse: {exc}"
        return out
    table, split_by = annotation_spec(screen)
    if split_row is not None and split_row.get("value_source") in DERIVED_SOURCES:
        if table and split_by:
            split_row["value"], split_row["value_source"] = split_by, "screen-yaml"
        elif split_row.get("value_source") == "screen-yaml":
            split_row["value"], split_row["value_source"] = split_default, "lab-default"

    if ann_row is None or ann_row.get("value_source") not in DERIVED_SOURCES:
        return out
    try:
        rep = build(analysis_dir)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        out["pause"] = (
            f"aggregate: config error in screen.yaml cellular.well_annotations: {exc}. "
            "Fix the table or screen.yaml and re-run."
        )
        return out
    if rep is None:
        if ann_row.get("value_source") == "screen-yaml":
            ann_row["value"], ann_row["value_source"] = None, "lab-default"
            out["clear"].append("WELL_ANNOTATIONS_FP")
            out["notes"].append("aggregate: screen.yaml names no well annotations; cleared")
        return out
    if rep["kept"] == 0:
        out["pause"] = (
            f"aggregate: config error: no well in {table} has a {rep['split_by'] or 'row'} "
            f"value and merge output ({len(rep['not_merged'])} without merge output, "
            f"{rep['blank_group']} without a group). Check its plate and well format."
        )
        return out

    ann_row["value"], ann_row["value_source"] = rep["path"], "screen-yaml"
    out["wells"] = rep["wells"]
    dropped = rep["not_merged"]
    left_out = f"; left out without merge output: {dropped}" if dropped else ""
    out["notes"].append(
        f"aggregate: {rep['kept']} wells annotated, split by {rep['split_by']} into "
        f"{len(rep['groups'])} groups; {rep['blank_group']} rows without a group "
        f"skipped{left_out}"
    )
    return out


def set_annotated_test_wells(rows: list[dict], wells: list[tuple[int, str]]) -> str:
    """Point TEST_PLATE and TEST_WELL_1/2 at wells the annotation table kept.

    The notebook joins the table onto the test wells, so a test well outside it fails.
    Unset and auto-probed rows are (re)picked; an operator-set plate is kept and its
    annotated wells are used.

    Args:
        rows: The aggregate notebook's interview rows, mutated in place.
        wells: The kept ``(plate, well)`` pairs, sorted.

    Returns:
        A progress message naming the test wells.
    """
    by_name = {r.get("param_name"): r for r in rows}
    auto = (None, "auto-probe")
    plate_row = by_name.get("TEST_PLATE")
    plate = plate_row.get("value") if plate_row else None
    on_plate = [w for p, w in wells if plate is not None and str(p) == str(plate)]
    if not on_plate:
        if plate_row is not None and plate_row.get("value_source") not in auto:
            return f"aggregate: TEST_PLATE={plate} has no annotated wells; test wells left as set"
        plate = wells[0][0]
        on_plate = [w for p, w in wells if p == plate]
        if plate_row is not None:
            plate_row["value"], plate_row["value_source"] = plate, "auto-probe"
    picked = [on_plate[0], on_plate[1] if len(on_plate) > 1 else on_plate[0]]
    for name, well in zip(("TEST_WELL_1", "TEST_WELL_2"), picked):
        row = by_name.get(name)
        if row is None or row.get("value_source") not in auto:
            continue
        if row.get("value") is None or str(row.get("value")) not in on_plate:
            row["value"], row["value_source"] = well, "auto-probe"
    wells_now = [by_name[n].get("value") for n in ("TEST_WELL_1", "TEST_WELL_2") if n in by_name]
    return f"aggregate: test wells = plate {plate} {wells_now} (of {len(wells)} annotated wells)"


def fill_min_cell_cutoffs(analysis_dir: Path, row: dict) -> list[str]:
    """Complete the MIN_CELL_CUTOFFS row for every cell class in the aggregate combos.

    Returns:
        The classes that took the ``all`` cutoff; empty when nothing changed.
    """
    combo_fp = Path(analysis_dir) / "config/aggregate_combo.tsv"
    if not isinstance(row.get("value"), dict) or not combo_fp.exists():
        return []
    with open(combo_fp) as fh:
        classes = sorted({r["cell_class"] for r in csv.DictReader(fh, delimiter="\t")})
    done, filled = complete_min_cell_cutoffs(row["value"], classes)
    if filled:
        row["value"] = done
    return filled


def complete_min_cell_cutoffs(
    cutoffs: dict | None, cell_classes: list[str]
) -> tuple[dict | None, list[str]]:
    """Give every cell class a cutoff, copying ``all`` to classes the operator did not name.

    The cluster pipeline looks up ``min_cell_cutoffs[cell_class]`` for every class in the
    aggregate combos, so a split into annotation groups needs one entry per group.

    Returns:
        (completed cutoffs, classes that were filled in).
    """
    if not isinstance(cutoffs, dict) or "all" not in cutoffs:
        return cutoffs, []
    filled = [c for c in cell_classes if c not in cutoffs]
    return {**cutoffs, **{c: cutoffs["all"] for c in filled}}, filled


def annotation_spec(screen: dict) -> tuple[str | None, str | None]:
    """(table path relative to screen.yaml, split column) from screen.yaml ``cellular``."""
    cellular = screen.get("cellular") or {}
    return cellular.get("well_annotations") or None, cellular.get("split_by") or None


def results_root(analysis_dir: Path) -> Path:
    """The pipeline's results folder: config.yml ``all.root_fp``, else ``brieflow_output``."""
    import yaml

    cfg_fp = Path(analysis_dir) / "config/config.yml"
    root = None
    if cfg_fp.exists():
        try:
            root = ((yaml.safe_load(cfg_fp.read_text()) or {}).get("all") or {}).get("root_fp")
        except yaml.YAMLError:
            root = None
    return Path(analysis_dir) / (root or "brieflow_output")


def merge_final_path(root: Path, plate: Any, well: str) -> Path:
    """The ``merge_final`` parquet a well writes when it reached merge."""
    return Path(root) / f"merge/parquets/P-{plate}_W-{well}__merge_final.parquet"
