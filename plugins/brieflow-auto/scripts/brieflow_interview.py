#!/usr/bin/env python3
"""brieflow_interview.py — interview.json I/O, gating, derivation + resolution.

The interview engine: initialize a per-screen interview from the template,
read/write it, filter/find/set param rows, evaluate is_active gating, compute
screen.yaml-derived values (_COMPUTED_DERIVATIONS), fill template/lab defaults,
record tuned-confirmed provenance, and resolve all phases (the operator-gap
collector). Channel-mapping confirm/record live here too (they mutate the
interview). Depends on brieflow_buckets + brieflow_channels; re-exported from
brieflow_wizard.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from brieflow_buckets import (
    _OPERATOR_BUCKETS,
    VALID_VALUE_SOURCES,
)
from brieflow_channels import default_channel_cmaps
from brieflow_phases import PHASES

# ---------------------------------------------------------------------------
# Initialization (template → per-screen interview.json)
# ---------------------------------------------------------------------------

_DEFAULT_TEMPLATE: Path = (
    Path(__file__).resolve().parent.parent / "data" / "interview_template.json"
)
_DEFAULT_LAB_DEFAULTS: Path = (
    Path(__file__).resolve().parent.parent / "data" / "lab_defaults.yaml"
)


def _screen_id(analysis_dir: Path, screen_yaml_path: Path | None = None) -> str:
    """Derive screen_id from screen.yaml `collection.screen_id`, falling back to
    the analysis_dir name (strip `-analysis` suffix).

    The conventional layout places screen.yaml inside the analysis_dir:
        <analysis_dir>/screen.yaml
        <analysis_dir>/.brieflow/interview.json
    which matches how init_screen_interview resolves it (dst.parent.parent).
    """
    # Default: screen.yaml lives alongside the .brieflow/ dir inside analysis_dir.
    sy = screen_yaml_path or (analysis_dir / "screen.yaml")
    sid = (_load_screen_yaml(sy).get("collection") or {}).get("screen_id")
    if sid:
        return str(sid)
    return analysis_dir.name.removesuffix("-analysis")


def init_screen_interview(
    template_path: str | Path,
    dest_path: str | Path,
    screen_id: str,
    force: bool = False,
    screen_yaml_path: str | Path | None = None,
) -> dict:
    """Copy the plugin's interview_template.json to a screen's interview.json,
    stamp it with screen_id + created_at, and apply `derivation.type=screen_yaml`
    pre-fills from the screen's screen.yaml. Refuses to overwrite unless
    force=True.

    `screen_yaml_path`: explicit path to the screen's screen.yaml. If None,
    looks for `<dest_dir>/../screen.yaml` (the conventional layout: interview
    lives in `<analysis_dir>/.brieflow/` next to `<analysis_dir>/screen.yaml`).

    Returns a dict summarizing the bootstrap:
      {
        "dest": Path,
        "applied": [{"param_name", "notebook", "path", "value"}, ...],
        "missing": [{"param_name", "notebook", "path"}, ...],
      }
    `missing` entries should be surfaced by callers as `wizard-findings/v1
    kind=screen-yaml-path-missing` — the template referenced a screen.yaml
    path that doesn't exist or is null in this screen's screen.yaml (e.g.
    field renamed, screen.yaml schema drifted).
    """
    src = Path(template_path)
    dst = Path(dest_path)
    if dst.exists() and not force:
        raise FileExistsError(
            f"{dst} already exists. Pass force=True to overwrite, or delete it first."
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    interview = load_interview(dst)
    interview["_screen_id"] = screen_id
    interview["_created_at"] = _now_iso()

    # Resolve screen.yaml location (default: sibling of dest_dir).
    if screen_yaml_path is None:
        screen_yaml_path = dst.parent.parent / "screen.yaml"
    screen_yaml_path = Path(screen_yaml_path)
    screen: dict = _load_screen_yaml(screen_yaml_path)
    applied, missing = apply_screen_yaml_derivations(interview, screen)
    interview["_screen_yaml_sha256"] = _file_sha(screen_yaml_path)

    save_interview(interview, dst)
    return {"dest": dst, "applied": applied, "missing": missing}


# Per-row keys that hold a screen's answer; everything else in a row is the template's definition.
_ANSWER_KEYS = frozenset({"value", "value_source", "set_at", "_csv_sha_at_set", "reason"})


def sync_interview_with_template(interview: dict, template: dict) -> list[str]:
    """Bring an interview built from an older plugin up to the current template.

    Each row takes the template's definition (bucket, derivation, gating, notes, ...) and keeps
    its answer; template rows the interview lacks are added unanswered. Returns the param names
    whose definition changed or that were added."""
    rows = {(r.get("notebook"), r.get("param_name")): r for r in interview.get("params", [])}
    changed = []
    for trow in template.get("params", []):
        key = (trow.get("notebook"), trow.get("param_name"))
        row = rows.get(key)
        if row is None:
            interview["params"].append(json.loads(json.dumps(trow)))
            changed.append(trow["param_name"])
            continue
        definition = {k: v for k, v in trow.items() if k not in _ANSWER_KEYS}
        stale = {k for k in set(row) | set(definition) if k not in _ANSWER_KEYS
                 and row.get(k) != definition.get(k)}
        if stale:
            for k in stale:
                if k in definition:
                    row[k] = definition[k]
                else:
                    row.pop(k, None)
            changed.append(trow["param_name"])
    for k in ("_csv_sha", "_n_params", "_schema_version"):
        if k in template:
            interview[k] = template[k]
    return changed


def apply_screen_yaml_derivations(
    interview: dict, screen: dict, only_unset_or_from_screen: bool = False
) -> tuple[list[dict], list[dict]]:
    """Fill rows whose derivation reads screen.yaml. Returns (applied, missing).

    With only_unset_or_from_screen, rows already set from another source (operator, tuned,
    probe) are left alone, so a screen.yaml edit refreshes only what screen.yaml supplied."""
    applied: list[dict] = []
    missing: list[dict] = []
    for p in interview.get("params", []):
        if only_unset_or_from_screen and p.get("value_source") not in (None, "screen-yaml", "default"):
            continue
        derivation = p.get("derivation")
        if not isinstance(derivation, dict):
            continue
        dtype = derivation.get("type")
        value: Any = None
        path = derivation.get("path", "")

        if dtype == "screen_yaml":
            # Scalar leaf at a dotted path. Most common case.
            value = _lookup_dotted(screen, path)
        elif dtype == "screen_yaml_list_field":
            # Extract one field from each item of a list-of-dicts at `path`.
            # e.g. iss.channels[].name → ['DAPI','G','T','A','C'].
            container = _lookup_dotted(screen, path)
            field = derivation.get("field")
            if isinstance(container, list) and field:
                value = [
                    item.get(field) for item in container if isinstance(item, dict)
                ]
                # Treat all-None / empty as "field missing" rather than a valid empty list.
                if not value or all(v is None for v in value):
                    value = None
        elif dtype == "library_default":
            # Constant default from the template — used for canonical values
            # that don't live in screen.yaml (e.g. dapi_cycle=1 by Cheeseman
            # convention). No screen.yaml lookup.
            value = derivation.get("value")
            path = "<library_default>"
        elif dtype == "screen_yaml_computed":
            # Computed value from a registered function over the screen dict.
            # The function returns a dict; this row picks one `field`. Used for
            # values that are deterministic functions of screen.yaml inputs but
            # require math (e.g. map_start/end from sequencing_order + lengths).
            fn = _COMPUTED_DERIVATIONS.get(derivation.get("compute"))
            field = derivation.get("field")
            if fn is None or not field:
                continue
            result = fn(screen)
            if isinstance(result, dict):
                value = result.get(field)
            path = f"<computed:{derivation.get('compute')}.{field}>"
        else:
            # Unknown derivation type — leave as null, surface as missing.
            continue

        if value is None and not derivation.get("null_ok"):
            if only_unset_or_from_screen and p.get("value_source") == "screen-yaml":
                p["value"], p["value_source"] = None, None
            missing.append(
                {
                    "param_name": p.get("param_name"),
                    "notebook": p.get("notebook"),
                    "path": path,
                }
            )
        else:
            # null_ok rows: an explicitly-declared null in screen.yaml is a valid
            # answer (e.g. foci_channel=null → non-foci screen), not "missing" —
            # mark it filled so it isn't re-asked, and downstream gates route off.
            p["value"] = value
            p["value_source"] = (
                "default" if dtype == "library_default" else "screen-yaml"
            )
            p["set_at"] = _now_iso()
            applied.append(
                {
                    "param_name": p.get("param_name"),
                    "notebook": p.get("notebook"),
                    "path": path,
                    "value": value,
                }
            )
    return applied, missing


def _file_sha(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _lookup_dotted(obj: dict, path: str):
    """Resolve a dotted path like `library.raw_design_table_fp` against a
    nested dict. Returns the value, or None if any segment is missing or the
    leaf is null."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


# ---------------------------------------------------------------------------
# Computed derivations — small registered functions that derive several
# correlated values from screen.yaml inputs. Each function takes the parsed
# screen dict and returns a dict of named outputs (or None if inputs are
# missing). Template rows reference these via:
#   {"type": "screen_yaml_computed", "compute": "<fn_name>", "field": "<output_key>"}
# ---------------------------------------------------------------------------


def _compute_barcode_cycle_positions(screen: dict) -> dict | None:
    """Derive (map_start, map_end, recomb_start, recomb_end) from screen.yaml
    `library.sequencing_order` + `library.barcode_length_map` +
    `library.barcode_length_recomb`. Returns None if any input is missing.

    Mirrors `lib.sbs.barcode_cycle_utils.compute_barcode_cycle_positions`,
    INCLUDING skip cycles: `library.skip_cycles` (1-based SBS cycle numbers, e.g.
    ``[6]`` for a cycle that failed to align) is the one operator knob. Skipped
    cycles are dropped from the aligned stack by `align_cycles`, so the read
    barcode shrinks and every region after a skip shifts; the library prefixes
    must be standardized with the same skips (SKIP_CYCLES_MAP / SKIP_CYCLES_RECOMB
    are the skipped positions RELATIVE to each region). Deriving all of it from
    one list is what keeps the region and the library consistent — one screen's 7-wide
    MAP region vs a 6-char library came from setting them separately.

    Also returns ``skip_cycles`` / ``skip_cycles_map`` / ``skip_cycles_recomb``
    (None when nothing is skipped) for the matching marker rows.
    """
    lib = screen.get("library") or {}
    order = lib.get("sequencing_order")
    map_len = lib.get("barcode_length_map")
    recomb_len = lib.get("barcode_length_recomb")
    try:
        skips = sorted({int(c) for c in (lib.get("skip_cycles") or [])})
    except (TypeError, ValueError):
        return None
    if not order or map_len is None or recomb_len is None:
        # simple (single-barcode) mode: no regions to place, but the alignment
        # skip list and its MAP-prefix mirror are still derivable
        return {"skip_cycles": skips or None, "skip_cycles_map": skips or None} if skips else None
    try:
        map_len = int(map_len)
        recomb_len = int(recomb_len)
    except (TypeError, ValueError):
        return None
    first_len, second_len = (map_len, recomb_len) if order == "map_recomb" else (recomb_len, map_len)
    if order not in ("map_recomb", "recomb_map"):
        return None
    first_skips = [c for c in skips if 1 <= c <= first_len]
    second_skips = [c - first_len for c in skips if first_len < c <= first_len + second_len]
    first_eff = first_len - len(first_skips)
    second_eff = second_len - len(second_skips)
    first_pos = (1, first_eff)
    second_pos = (first_eff + 1, first_eff + second_eff)
    if order == "map_recomb":
        map_pos, recomb_pos, map_skips, recomb_skips = first_pos, second_pos, first_skips, second_skips
    else:
        recomb_pos, map_pos, recomb_skips, map_skips = first_pos, second_pos, first_skips, second_skips
    return {
        "map_start": map_pos[0],
        "map_end": map_pos[1],
        "recomb_start": recomb_pos[0],
        "recomb_end": recomb_pos[1],
        "skip_cycles": skips or None,
        "skip_cycles_map": map_skips or None,
        "skip_cycles_recomb": recomb_skips or None,
    }


def _compute_aggregate_fields(screen: dict) -> dict | None:
    """Derive aggregate control_key + filter_queries from screen.yaml library.

    control_key   <- library.negative_controls[0] (the non-targeting label that
                     Stage-1c reclass matches against gene_symbol_0).
    filter_queries <- ["mapped_single_gene == True"], plus
                     "no_recomb_0 != False or no_recomb_0.isna()" for multi-mode
                     (RECOMB) barcoding — keep cells that are not recombined (or
                     have no recomb call). The base filter applies to every
                     screen, so this returns a value even when library.type is unset.
    split_col     <- cellular.split_by when cellular.well_annotations names a table;
                     absent otherwise, so the lab default ("class") fills it.
    group_cols    <- [cellular.group_by] when the table is set (a list is kept as is).
    *_scope / *_reference_group <- "within_perturbation" and cellular.reference_group when
                     both group_by and reference_group are set: each perturbation is scored
                     against its own arm in the reference group (vehicle-referenced design).
    """
    lib = screen.get("library") or {}
    out: dict[str, Any] = {}
    cellular = screen.get("cellular") or {}
    if cellular.get("well_annotations") and cellular.get("split_by"):
        out["split_col"] = cellular["split_by"]
    group_by = cellular.get("group_by") if cellular.get("well_annotations") else None
    if group_by:
        out["group_cols"] = list(group_by) if isinstance(group_by, (list, tuple)) else [group_by]
        reference = cellular.get("reference_group")
        if reference:
            out["bootstrap_control_scope"] = out["control_scope"] = "within_perturbation"
            out["bootstrap_reference_group"] = out["control_reference_group"] = reference
    negs = lib.get("negative_controls") or []
    if negs:
        out["control_key"] = negs[0]
    fq = ["mapped_single_gene == True"]
    if str(lib.get("type") or "").lower() in ("multi", "recomb", "triple"):
        fq.append("no_recomb_0 != False or no_recomb_0.isna()")
    out["filter_queries"] = fq
    return out or None


def _compute_sbs_background(screen: dict) -> dict | None:
    """SBS segmentation background from screen.yaml iss.background_channel_*.

    dapi_cycle <- background_channel_nucleus.cycle, else 1. On a DNA-barcode screen, where any
    base channel's background outlines the cell, cyto_channel defaults to the first base channel
    and cyto_cycle to the DAPI cycle; elsewhere a blank cell channel stays blank and is asked."""
    iss = screen.get("iss") or {}
    nucleus = iss.get("background_channel_nucleus") or {}
    cell = iss.get("background_channel_cell") or {}
    dapi_cycle = nucleus.get("cycle") or 1
    out: dict[str, Any] = {"dapi_cycle": dapi_cycle}
    channel, cycle = cell.get("channel"), cell.get("cycle")
    dna = str((screen.get("library") or {}).get("detection_target") or "").strip().upper() == "DNA"
    if not channel and dna:
        names = [c.get("name") for c in (iss.get("channels") or []) if isinstance(c, dict)]
        channel = next((n for n in names if n and n != "DAPI"), None)
    if channel:
        out["cyto_channel"] = channel
        out["cyto_cycle"] = cycle or dapi_cycle
    return out


def _compute_phenotype_align(screen: dict) -> dict | None:
    """ALIGN from screen.yaml phenotype.channels_imaged_together: channels imaged together at
    each field need no alignment; channels imaged in separate passes are aligned. None if blank."""
    together = (screen.get("phenotype") or {}).get("channels_imaged_together")
    return None if together is None else {"align": not bool(together)}


def _compute_channel_combos(screen: dict) -> dict | None:
    """One aggregation of every phenotype channel, from screen.yaml phenotype.channels."""
    names = [c.get("biological_name") for c in ((screen.get("phenotype") or {}).get("channels") or [])
             if isinstance(c, dict) and c.get("biological_name")]
    return {"channel_combos": [names]} if names else None


def _compute_phenotype_cmaps(screen: dict) -> dict | None:
    """Derive phenotype CHANNEL_CMAPS (one colormap per channel) from screen.yaml
    `phenotype.channels`. Auto-sizes the palette to the channel count so the viz
    cells never IndexError; operator can override for round-aligned colors."""
    chans = (screen.get("phenotype") or {}).get("channels") or []
    names = [c.get("biological_name") or c.get("name")
             for c in chans if isinstance(c, dict)]
    names = [n for n in names if n]
    if not names:
        return None
    return {"channel_cmaps": default_channel_cmaps(names)}


def _compute_sbs_cmaps(screen: dict) -> dict | None:
    """Derive SBS CHANNEL_CMAPS (one colormap per channel) from screen.yaml
    `iss.channels`. Auto-sizes the palette to the channel count."""
    chans = (screen.get("iss") or {}).get("channels") or []
    names = [c.get("name") or c.get("biological_name")
             for c in chans if isinstance(c, dict)]
    names = [n for n in names if n]
    if not names:
        return None
    return {"channel_cmaps": default_channel_cmaps(names)}


def _compute_sbs_segment_cells(screen: dict) -> dict | None:
    """Derive SBS SEGMENT_CELLS from screen.yaml `library.detection_target`.

    DNA barcodes give nuclear spots → nuclei-only segmentation (False); RNA
    (T7-amplified / mRNA) gives cytoplasmic spots → segment cells (True).
    Returns None if detection_target is unset (operator confirms)."""
    target = (screen.get("library") or {}).get("detection_target")
    if target is None:
        return None
    t = str(target).strip().upper()
    if t == "DNA":
        return {"segment_cells": False}
    if t == "RNA":
        return {"segment_cells": True}
    return None


def _compute_sbs_sort_calls(screen: dict) -> dict | None:
    """Derive SBS SORT_CALLS from screen.yaml `library.detection_target`.

    The template note is explicit: "count for mRNA, peak for DNA protocols."
    DNA barcodes → `peak`; RNA (mRNA) → `count`. Mirrors `_compute_sbs_segment_cells`
    — the *only other* detection_target-driven SBS param the plugin auto-derived.
    Returns None if detection_target is unset, so the lab-default (`count`)
    backfills it (and a DNA screen no longer silently mis-sorts on the count default)."""
    target = (screen.get("library") or {}).get("detection_target")
    if target is None:
        return None
    t = str(target).strip().upper()
    if t == "DNA":
        return {"sort_calls": "peak"}
    if t == "RNA":
        return {"sort_calls": "count"}
    return None


def _channel_metadata_entries(channels: list, name_key: str) -> list | None:
    """Build a channels_metadata list[dict] from screen.yaml channel entries.

    One dict per channel, in acquisition order. `name` is the only hard-required
    field (HCS/OME annotation — consumed by viewers, not by processing). Pulls
    biological annotation (target/marker/marker_type) where screen.yaml provides
    it; index is positional; color cycles the default cmap palette. Returns None
    if no named channel is present (operator authors it)."""
    if not isinstance(channels, list):
        return None
    names = [c.get(name_key) for c in channels if isinstance(c, dict) and c.get(name_key)]
    if not names:
        return None
    cmaps = default_channel_cmaps(names)
    entries: list[dict] = []
    for i, c in enumerate(channels):
        if not isinstance(c, dict) or not c.get(name_key):
            continue
        target = c.get("marker_of")
        marker = c.get("marker")
        marker_type = c.get("marker_type")
        full_label = ", ".join(x for x in (target, marker) if x) or None
        entries.append({
            "name": c.get(name_key),
            "index": len(entries),
            "channel_type": "fluorescent",
            "color": cmaps[len(entries)] if len(entries) < len(cmaps) else None,
            "description": None,
            "biological_annotation": {
                "biological_target": target,
                "marker": marker,
                "marker_type": marker_type,
                "full_label": full_label,
            },
        })
    return entries or None


def _compute_channels_metadata(screen: dict) -> dict | None:
    """Derive SBS + PHENOTYPE CHANNELS_METADATA (OME-Zarr channel annotation)
    from screen.yaml `iss.channels` / `phenotype.channels`. Template rows pick
    one side via field=sbs_channels_metadata | phenotype_channels_metadata."""
    iss = (screen.get("iss") or {}).get("channels") or []
    ph = (screen.get("phenotype") or {}).get("channels") or []
    out = {
        "sbs_channels_metadata": _channel_metadata_entries(iss, "name"),
        "phenotype_channels_metadata": _channel_metadata_entries(ph, "biological_name"),
    }
    return out if any(v is not None for v in out.values()) else None


_OBJECTIVE_MAG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[xX]")


def objective_magnification(objective: Any) -> float | None:
    """Magnification of a screen.yaml objective ("40X 0.95 NA", "20x", 40), or None."""
    if isinstance(objective, (int, float)) and not isinstance(objective, bool):
        return float(objective) if objective > 0 else None
    if not isinstance(objective, str):
        return None
    m = _OBJECTIVE_MAG_RE.search(objective)
    return float(m.group(1)) if m else None


def merge_approach_policy(lab: dict | None = None) -> dict:
    """The MERGE_APPROACH thresholds from lab_defaults.yaml `merge_approach`."""
    if lab is None:
        import yaml as _yaml

        lab = (
            _yaml.safe_load(_DEFAULT_LAB_DEFAULTS.read_text()) or {}
            if _DEFAULT_LAB_DEFAULTS.exists()
            else {}
        )
    policy = lab.get("merge_approach") or {}
    return {
        "max_cells_per_tile": float(policy.get("max_cells_per_tile", 60)),
        "min_objective_x": float(policy.get("min_objective_x", 40)),
    }


def derive_merge_approach(
    screen: dict, median_cells_per_tile: float | None = None, lab: dict | None = None
) -> dict:
    """Decide the merge approach: "positions" for sparse, high-magnification phenotype tiles.

    The measured median phenotype cells per tile wins when it exists (value_source
    auto-probe); before phenotype outputs exist the phenotype objective decides
    (screen-yaml); with neither, "fast" (default). Binning is reported but does not
    decide: it changes pixel size, not the field of view that sets cells per tile.

    Returns:
        dict: {"approach": "fast" | "positions", "value_source": str, "reason": str}.
    """
    policy = merge_approach_policy(lab)
    max_cells, min_x = policy["max_cells_per_tile"], policy["min_objective_x"]
    ph = (screen or {}).get("phenotype") or {}
    if median_cells_per_tile is not None:
        sparse = median_cells_per_tile < max_cells
        return {
            "approach": "positions" if sparse else "fast",
            "value_source": "auto-probe",
            "reason": (
                f"median {median_cells_per_tile:g} phenotype cells per tile "
                f"{'<' if sparse else '>='} {max_cells:g} "
                f"(lab_defaults merge_approach.max_cells_per_tile)"
            ),
        }
    mag = objective_magnification(ph.get("objective"))
    if mag is not None:
        high = mag >= min_x
        binning = ph.get("binning")
        return {
            "approach": "positions" if high else "fast",
            "value_source": "screen-yaml",
            "reason": (
                f"phenotype.objective {ph.get('objective')!r} ({mag:g}x"
                + (f", binning {binning}" if binning else "")
                + f") {'>=' if high else '<'} {min_x:g}x "
                "(lab_defaults merge_approach.min_objective_x), until phenotype cell "
                "counts exist"
            ),
        }
    return {
        "approach": "fast",
        "value_source": "default",
        "reason": (
            "no phenotype.objective in screen.yaml and no phenotype cell counts yet; "
            "rechecked from measured cells per tile when merge starts"
        ),
    }


def _compute_merge_approach(screen: dict) -> dict | None:
    """MERGE_APPROACH from the phenotype objective alone (None without one)."""
    if objective_magnification(((screen or {}).get("phenotype") or {}).get("objective")) is None:
        return None
    return {"merge_approach": derive_merge_approach(screen)["approach"]}


def apply_merge_approach(
    interview: dict,
    screen: dict,
    median_cells_per_tile: float | None = None,
    lab: dict | None = None,
) -> dict | None:
    """Write the derived MERGE_APPROACH into the merge row with its source and reason.

    An operator value is never replaced. Returns the decision with `changed` (the value
    differs from what the row held), or None when the interview has no MERGE_APPROACH row.
    """
    row = find_param(interview, "MERGE_APPROACH", notebook="merge")
    if row is None:
        return None
    if row.get("value_source") == "operator":
        return {
            "approach": row.get("value"),
            "value_source": "operator",
            "reason": "operator override",
            "changed": False,
        }
    decision = derive_merge_approach(screen, median_cells_per_tile, lab)
    changed = row.get("value") != decision["approach"]
    if changed or row.get("value_source") != decision["value_source"]:
        set_value(interview, "MERGE_APPROACH", decision["approach"], decision["value_source"],
                  notebook="merge")
    row["reason"] = decision["reason"]
    return {**decision, "changed": changed}


# organism name (case/format-insensitive) -> NCBI taxid, used only when
# experiment.organism_ontology_term_id is absent. Extend as screens require.
_ORGANISM_TAXID = {
    "homo_sapiens": "9606", "human": "9606", "hsapiens": "9606",
    "mus_musculus": "10090", "mouse": "10090", "mmusculus": "10090",
    "rattus_norvegicus": "10116", "rat": "10116",
}


def _compute_species_id(screen: dict) -> dict | None:
    """Derive NCBI taxid for UniProt + CORUM/MSigDB benchmark generation from
    screen.yaml `experiment`. Prefer `organism_ontology_term_id` (e.g.
    "NCBITaxon:9606" -> "9606"); fall back to the `organism` name via table.
    Returns None if neither is set (operator is then asked). Human (9606) is
    NOT assumed on absence."""
    exp = screen.get("experiment") or {}
    term = exp.get("organism_ontology_term_id")
    if term and "NCBITaxon:" in str(term):
        taxid = str(term).split("NCBITaxon:")[-1].strip()
        if taxid.isdigit():
            return {"species_id": taxid}
    name = exp.get("organism")
    if name:
        key = str(name).strip().lower().replace(" ", "_").replace("-", "_")
        taxid = _ORGANISM_TAXID.get(key) or _ORGANISM_TAXID.get(key.replace("_", ""))
        if taxid:
            return {"species_id": taxid}
    return None


def _compute_chemistry(screen: dict) -> dict | None:
    """Normalize screen.yaml `iss.chemistry` (human-readable "four color") to
    the notebook's token form ("four_color" | "combinatorial"). Returns None
    if unset (operator asked)."""
    chem = (screen.get("iss") or {}).get("chemistry")
    if not chem:
        return None
    return {"chemistry": str(chem).strip().lower().replace(" ", "_").replace("-", "_")}


def _compute_call_reads_method(screen: dict) -> dict | None:
    """Pick the base-calling method from chemistry: four_color → "median" (the
    lab-standard default), combinatorial → "frac" (the recommended dye-fraction
    caller). None if chemistry is unset (operator asked). Operator may override
    (e.g. combinatorial "merfish")."""
    chem = _compute_chemistry(screen)
    if not chem:
        return None
    return {"call_reads_method": "frac" if chem["chemistry"] == "combinatorial" else "median"}


def _compute_combinatorial_channels(screen: dict) -> dict | None:
    """For combinatorial chemistry, split screen.yaml iss.channels into base-
    encoding channels (BASES) and non-base channels (EXTRA_CHANNELS), using
    iss.combinatorial.code to identify base channels. BASES is ordered by
    CHANNEL_NAMES order (the notebook requires BASES to match channel order
    after EXTRA_CHANNELS are removed). None if channels or code are unset."""
    iss = screen.get("iss") or {}
    chans = [c.get("name") for c in (iss.get("channels") or []) if isinstance(c, dict)]
    chans = [c for c in chans if c]
    code = (iss.get("combinatorial") or {}).get("code")
    if not chans or not isinstance(code, dict) or not code:
        return None
    if not all(isinstance(v, list) for v in code.values()):
        return None
    on_channels = {ch for ons in code.values() for ch in (ons or [])}
    bases = [c for c in chans if c in on_channels]
    extra = [c for c in chans if c not in on_channels]
    if not bases:
        return None
    return {"bases": bases, "extra_channels": extra}


_COMPUTED_DERIVATIONS: dict[str, Any] = {
    "sbs_background": _compute_sbs_background,
    "channel_combos": _compute_channel_combos,
    "phenotype_align": _compute_phenotype_align,
    "barcode_cycle_positions": _compute_barcode_cycle_positions,
    "aggregate_fields": _compute_aggregate_fields,
    "phenotype_cmaps": _compute_phenotype_cmaps,
    "sbs_cmaps": _compute_sbs_cmaps,
    "sbs_segment_cells": _compute_sbs_segment_cells,
    "sbs_sort_calls": _compute_sbs_sort_calls,
    "channels_metadata": _compute_channels_metadata,
    "merge_approach": _compute_merge_approach,
    "species_id": _compute_species_id,
    "chemistry": _compute_chemistry,
    "call_reads_method": _compute_call_reads_method,
    "combinatorial_channels": _compute_combinatorial_channels,
}


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def load_interview(path: str | Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _json_safe(o):
    """Coerce non-JSON-native values to native Python. Sweep winners and probe
    results arrive as numpy scalars/arrays (e.g. numpy.int64 from a sweep score),
    which json can't serialize — so a tuned value would otherwise crash the save
    *after* the (expensive) sweep already ran."""
    if hasattr(o, "tolist"):    # numpy array OR scalar — tolist() coerces BOTH
        return o.tolist()       # (np.int64(200).tolist()->200; np.array([1,2]).tolist()->[1,2])
    if hasattr(o, "item"):      # other single-value array-likes
        return o.item()
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def save_interview(interview: dict, path: str | Path) -> None:
    """Atomic write: tmp file + rename. Protects the in-progress record from
    partial writes if the process is killed mid-update. `default=_json_safe`
    coerces numpy scalars/arrays so a tuned/probed value can never crash the save."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(interview, indent=2, default=_json_safe) + "\n")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Filter / lookup
# ---------------------------------------------------------------------------


def filter_params(
    interview: dict,
    notebook: str | None = None,
    bucket: str | Iterable[str] | None = None,
) -> list[dict]:
    """Return param rows matching the given notebook and/or bucket."""
    buckets = {bucket} if isinstance(bucket, str) else (set(bucket) if bucket else None)
    out: list[dict] = []
    for row in interview["params"]:
        if notebook is not None and row["notebook"] != notebook:
            continue
        if buckets is not None and row["bucket"] not in buckets:
            continue
        out.append(row)
    return out


def find_param(
    interview: dict, param_name: str, notebook: str | None = None
) -> dict | None:
    """Return the first param row matching param_name (and optionally notebook)."""
    for row in interview["params"]:
        if row["param_name"] != param_name:
            continue
        if notebook is not None and row["notebook"] != notebook:
            continue
        return row
    return None


# ---------------------------------------------------------------------------
# Write a value (with provenance + timestamp)
# ---------------------------------------------------------------------------


def _suggest_param_alternatives(
    interview: dict, param_name: str, notebook: str | None
) -> list[str]:
    """Return case-insensitive name matches for diagnostic messages. The template
    intentionally carries some case-only duplicates (e.g. sbs.peak_width vs
    sbs.PEAK_WIDTH track the same parameter at config-key vs notebook-marker
    layers), so we can't silently fold them — but we can suggest both forms so
    the caller picks the right one."""
    target = param_name.lower()
    hits: list[str] = []
    for row in interview.get("params", []):
        if row.get("param_name", "").lower() != target:
            continue
        if row.get("param_name") == param_name:
            continue  # the form the caller already tried
        if notebook is not None and row.get("notebook") != notebook:
            hits.append(f"{row['param_name']!r} (notebook={row['notebook']!r})")
        else:
            hits.append(repr(row["param_name"]))
    return hits


def set_value(
    interview: dict,
    param_name: str,
    value: Any,
    source: str,
    notebook: str | None = None,
) -> dict:
    """Fill in value/value_source/set_at for one param row. Returns the row."""
    if source not in VALID_VALUE_SOURCES:
        raise ValueError(
            f"unknown value_source: {source!r}; expected one of {VALID_VALUE_SOURCES}"
        )
    row = find_param(interview, param_name, notebook=notebook)
    if row is None:
        msg = f"param not found: {param_name}"
        if notebook is not None:
            msg += f" (notebook={notebook})"
        suggestions = _suggest_param_alternatives(interview, param_name, notebook)
        if suggestions:
            msg += f" — did you mean: {', '.join(suggestions)}?"
        raise KeyError(msg)
    row["value"] = value
    row["value_source"] = source
    row["set_at"] = _now_iso()
    # Stamp the CSV-schema sha the row was set under so future template
    # revisions can detect per-row staleness even when the whole-file sha
    # matches by coincidence.
    if "_csv_sha" in interview:
        row["_csv_sha_at_set"] = interview["_csv_sha"]
    return row


# ---------------------------------------------------------------------------
# Gating: decide whether a param row is active for the current screen flavor.
#
# Two representations coexist (structured wins when both present):
#   - structured `gating` block (canonical): a dict on the row, e.g.
#       {"param": "foci_channel", "op": "not_null"}
#       {"param": "num_sims", "op": "is_set"}
#       {"param": "sbs_data_format", "equals": "tiff"}   # equals shortcut
#       {"param": "barcode_type", "op": "in", "values": ["multi", "recomb"]}
#     Supported ops: equals (default), not_equals, is_set, not_null, in.
#     `equals`/`value` carry the scalar; `values` carries the `in` list.
#     `notebook` optionally pins the gate param's notebook.
#   - prose `only when X==Y` in `notes` (legacy, equality-only): still parsed
#     for the ~72 rows that use it. Used only when no structured block exists.
# ---------------------------------------------------------------------------


_GATING_RE = re.compile(r"only when (\w+)\s*==\s*['\"]?([^'\"\s,;.]+)['\"]?")

#: Gating ops the structured `gating` block understands.
_GATE_OPS = {"equals", "not_equals", "is_set", "not_null", "in"}


def _resolve_gate_row(
    interview: dict, key: str, row_notebook: str | None, gate_notebook: str | None = None
) -> dict | None:
    """Find the gating param, preferring the gated row's own notebook.

    Flags like `segment_cells` / `cyto_channel` exist in both sbs and
    phenotype; a phenotype gate must read phenotype's flag, not whichever row
    is first. Falls back to a global lookup for legitimate cross-notebook gates
    (the gating param lives in another notebook). `gate_notebook` overrides the
    notebook to resolve against when set on the structured block.
    """
    nb = gate_notebook or row_notebook
    return find_param(interview, key, notebook=nb) or find_param(interview, key)


def _gate_satisfied(val: Any, op: str, expected: Any, expected_values: Any) -> bool:
    """Evaluate one gate condition against the gating param's current value.

    A gating param that is not yet filled (`val is None`) leaves the dependent
    row inactive for every op except the explicit not-set checks — the caller
    revisits once the gate is answered.
    """
    if op in ("is_set", "not_null"):
        return val is not None
    if val is None:
        return False
    if op == "equals":
        return str(val).lower() == str(expected).lower()
    if op == "not_equals":
        return str(val).lower() != str(expected).lower()
    if op == "in":
        return any(str(val).lower() == str(v).lower() for v in (expected_values or []))
    raise ValueError(f"unknown gating op: {op!r} (expected one of {sorted(_GATE_OPS)})")


def _eval_structured_gate(g: dict, param: dict, interview: dict) -> bool:
    key = g.get("param")
    if not key:
        return True  # malformed/empty block → not a real gate
    op = g.get("op") or ("equals" if "equals" in g else None)
    if op is None:
        return True  # no op and no `equals` shortcut → nothing to evaluate
    gate_row = _resolve_gate_row(interview, key, param.get("notebook"), g.get("notebook"))
    val = gate_row.get("value") if gate_row else None
    expected = g.get("value", g.get("equals"))
    return _gate_satisfied(val, op, expected, g.get("values"))


def is_active(param: dict, interview: dict) -> bool:
    """True if the param's gating is satisfied by currently-filled values.

    A param with no gating clause is always active. A structured `gating` block
    (if present) is authoritative; otherwise the legacy prose `only when X==Y`
    clauses in `notes` are evaluated. A gating param not yet filled leaves the
    dependent row inactive (caller revisits after the gating answer comes in).
    """
    g = param.get("gating")
    if isinstance(g, dict) and g.get("param"):
        return _eval_structured_gate(g, param, interview)

    notes = param.get("notes") or ""
    matches = list(_GATING_RE.finditer(notes))
    if not matches:
        return True
    for m in matches:
        key, expected = m.group(1), m.group(2)
        gate_row = _resolve_gate_row(interview, key, param.get("notebook"))
        if gate_row is None or gate_row.get("value") is None:
            return False
        if str(gate_row["value"]).lower() != str(expected).lower():
            return False
    return True


# ---------------------------------------------------------------------------
# Tuned-value confirmation loop (post-run)
# ---------------------------------------------------------------------------
#
# Contract: each phase notebook contains a `# === TUNED EXPORT ===` cell that
# writes `<analysis-dir>/.brieflow/tuned_<phase>.json` after its derivation
# cells run. Schema:
#
#     {<param_name>: {"derived": <value>, "src": <cell-name-or-tag>}, ...}
#
# After the notebook exits, the wizard calls `pull_tuned_values` then
# `confirm_tuned_loop` (latter is wizard-agent driven; helpers here just do
# the read/write plumbing).


def tuned_export_path(analysis_dir: str | Path, phase: str) -> Path:
    return Path(analysis_dir) / ".brieflow" / f"tuned_{phase}.json"


# Notebook-derived values, read back from the config section the notebook writes.
TUNED_CONFIG_KEYS: dict[str, dict[str, str]] = {
    "sbs": {
        "NUCLEI_DIAMETER": "nuclei_diameter",
        "CELL_DIAMETER": "cell_diameter",
        "THRESHOLD_READS": "threshold_peaks",
        "PEAK_WIDTH": "peak_width",
    },
    "phenotype": {"NUCLEI_DIAMETER": "nuclei_diameter", "CELL_DIAMETER": "cell_diameter"},
}


def pull_tuned_values(analysis_dir: str | Path, phase: str) -> dict:
    """The values the phase notebook derived, as ``{param: {derived, src}}``.

    Older notebooks write `.brieflow/tuned_<phase>.json` from a TUNED EXPORT cell and that
    file wins when present; otherwise the values are read from the notebook's own section of
    `config/config.yml`. Returns {} when neither has them."""
    p = tuned_export_path(analysis_dir, phase)
    if p.exists():
        return json.loads(p.read_text())
    keys = TUNED_CONFIG_KEYS.get(phase)
    cfg_path = Path(analysis_dir) / "config" / "config.yml"
    if not keys or not cfg_path.exists():
        return {}
    import yaml

    section = (yaml.safe_load(cfg_path.read_text()) or {}).get(phase) or {}
    return {
        param: {"derived": section[key], "src": f"config.yml {phase}.{key}"}
        for param, key in keys.items()
        if section.get(key) is not None
    }


def record_tuned_confirmed(
    interview: dict,
    param_name: str,
    confirmed_value: Any,
    notebook: str | None = None,
) -> dict:
    """Convenience wrapper around set_value that uses the `tuned-confirmed`
    provenance source. The wizard agent calls this for each tuned param after
    the operator confirms or overrides the derived value."""
    return set_value(
        interview,
        param_name=param_name,
        value=confirmed_value,
        source="tuned-confirmed",
        notebook=notebook,
    )


# ---------------------------------------------------------------------------
# Tier 1 — auto-probe helpers (W-001/W-002 follow-up)
# ---------------------------------------------------------------------------


# Canonical type map for path-metadata columns. Adjust here when new columns
# (e.g. `round`, `time`) emerge in screen filenames.
_CANONICAL_TYPE_FOR = {
    "plate": "int",
    "well": "str",
    "tile": "int",
    "cycle": "int",
    "round": "int",
    "channel": "str",
    "time": "int",
    "z": "int",
}


def derive_metadata_order_type(metadata_list: Iterable[str]) -> dict:
    """Given an ordered list of capture-group names (e.g. ["plate", "cycle",
    "well", "channel"]), return a dict mapping each → canonical type name.
    Unknown columns default to "str" — caller should surface those for
    operator confirmation."""
    return {col: _CANONICAL_TYPE_FOR.get(col, "str") for col in metadata_list}


def confirm_channel_mapping(
    interview: dict,
    fluorophore_tokens: Iterable[str],
    biological_labels,
    side: str = "phenotype",
    round_order: Iterable[int] | None = None,
) -> dict | list[dict]:
    """Build the fluorophore→biological mapping the wizard surfaces to the
    operator (F-014). Auto-detects single-round vs multi-round phenotype:

    **Single-round (or SBS)** — `biological_labels` is a flat sequence the
    same length as `fluorophore_tokens`. Returns a single dict
    `{token: label}` for one AskUserQuestion confirmation.

    **Multi-round phenotype** — `biological_labels` is a list of equal-length
    sequences (one per round). Pass `round_order=[1, 2, ...]` to disambiguate
    when one of the rounds happens to coincide with the single-round shape.
    Returns a list of dicts (one per round) — the caller surfaces each
    round in its own AskUserQuestion so the operator confirms per-round.

    Multi-round example (snowdon: 2 rounds × 5 channels = 10 biological
    markers across same fluorophore set):

        tokens = ['DAPI','GFP','Cy3','Cy5','AF750']
        bio    = [
            ['DAPI', 'ATACsee', 'H4Kacme', 'pSer5', 'KI67'],   # round 1
            ['DAPI2', 'H3K4me3', 'H3K27ac', 'pSer2', 'H3K9me3'],  # round 2
        ]
        confirm_channel_mapping(iv, tokens, bio, 'phenotype', round_order=[1, 2])
        # → [{'DAPI':'DAPI','GFP':'ATACsee', ...}, {'DAPI':'DAPI2','GFP':'H3K4me3', ...}]

    Args:
        interview: loaded interview dict
        fluorophore_tokens: filename channel tokens shared across rounds
        biological_labels:
            - flat sequence (single-round / SBS): one label per token
            - list-of-sequences (multi-round phenotype): one inner list per
              round, each the same length as `fluorophore_tokens`
        side: 'sbs' or 'phenotype'
        round_order: explicit round indices for multi-round mode (e.g. [1, 2]);
            required when biological_labels is list-of-lists. Single-round
            ignores this.

    Returns:
        - dict {token: label} for single-round mode
        - list[dict] [{token: label_r1}, {token: label_r2}, ...] for multi-round

    Raises ValueError if shapes don't match the chosen mode.
    """
    tokens = list(fluorophore_tokens)
    labels = list(biological_labels)

    # Detect multi-round: outer is a sequence of sequences (and not strings).
    is_multi = (
        len(labels) > 0
        and not isinstance(labels[0], (str, bytes))
        and hasattr(labels[0], "__iter__")
    )

    if is_multi:
        rounds = [list(r) for r in labels]
        if round_order is None:
            raise ValueError(
                f"multi-round phenotype mapping requires round_order=[...] "
                f"(got {len(rounds)} round-label-lists, no round_order specified)"
            )
        ro = list(round_order)
        if len(ro) != len(rounds):
            raise ValueError(
                f"round_order length ({len(ro)}) doesn't match number of "
                f"round-label-lists ({len(rounds)})"
            )
        for i, r in enumerate(rounds):
            if len(r) != len(tokens):
                raise ValueError(
                    f"round_order[{i}]={ro[i]} mapping length mismatch: "
                    f"{len(tokens)} tokens vs {len(r)} labels"
                )
        return [
            {tok: lab for tok, lab in zip(tokens, round_labels)}
            for round_labels in rounds
        ]

    # Single-round / SBS path
    if len(tokens) != len(labels):
        raise ValueError(
            f"channel mapping length mismatch: {len(tokens)} tokens vs {len(labels)} labels"
        )
    return {tok: lab for tok, lab in zip(tokens, labels)}


def record_channel_mapping(
    interview: dict,
    confirmed_mapping,
    side: str = "phenotype",
    round_order: Iterable[int] | None = None,
) -> list[dict]:
    """Persist the operator-confirmed fluorophore→biological mapping into the
    interview rows for CHANNEL_NAMES and the channels_metadata.name entries.

    Accepts EITHER a single dict (single-round / SBS) OR a list-of-dicts
    (multi-round phenotype, one dict per round). For multi-round the per-round
    biological label lists are concatenated round-major into a single flat
    list, matching the layout phenotype_channels_metadata expects (e.g.,
    snowdon: round_1 [DAPI, ATACsee, H4Kacme, pSer5, KI67] then round_2
    [DAPI2, H3K4me3, H3K27ac, pSer2, H3K9me3] → 10-entry flat list).

    Returns the rows touched.
    """
    if isinstance(confirmed_mapping, dict):
        # single-round path
        biological_order = list(confirmed_mapping.values())
    else:
        # multi-round: list-of-dicts, concatenate values round-major
        rounds = list(confirmed_mapping)
        if not rounds:
            raise ValueError("confirmed_mapping is empty")
        if not all(isinstance(r, dict) for r in rounds):
            raise ValueError(
                "confirmed_mapping must be a dict OR list of dicts; "
                f"got {[type(r).__name__ for r in rounds]}"
            )
        biological_order = []
        for r in rounds:
            biological_order.extend(r.values())
        # Also persist round_order if provided (so downstream consumers know
        # how the flat list is structured).
        if round_order is not None:
            ro_row = find_param(interview, "PHENOTYPE_ROUND_ORDER", notebook="preprocess")
            if ro_row is not None:
                ro_row["value"] = list(round_order)
                ro_row["value_source"] = "operator"
                ro_row["set_at"] = _now_iso()

    touched = []

    # The operator-confirmed biological labels ARE the canonical channel name
    # list — write them to CHANNEL_NAMES (the per-side notebook row, normally a
    # screen.yaml list-field). CHANNELS_METADATA is computed from screen.yaml
    # (channels_metadata derivation) and *_CHANNEL_ORDER is the auto-probed raw
    # acquisition order, so neither is written here.
    cn = find_param(interview, "CHANNEL_NAMES", notebook=side)
    if cn is not None:
        cn["value"] = biological_order
        cn["value_source"] = "operator"
        cn["set_at"] = _now_iso()
        touched.append(cn)

    return touched


# ---------------------------------------------------------------------------
# Interview completeness (per-phase predicate, replaces a binary version)
# ---------------------------------------------------------------------------


def interview_completeness(
    analysis_dir: str | Path,
    phase: str,
    template_path: str | Path | None = None,
) -> dict:
    """Report per-row state of a phase against the current template.

    Iteration is template-driven so newly-added template rows surface as
    missing, and bucket drift (e.g. a row reclassified user → archival in
    a CSV revision) surfaces as stale even when the file-level `_csv_sha`
    happens to match. The single-sha check on its own was too coarse —
    Pass-3 found the gap (interview-template-drift finding).

    Returns:
        {
          "filled":   [param, ...],  # value is non-null AND no bucket drift
          "missing":  [param, ...],  # absent from interview OR null when required
          "stale":    [param, ...],  # bucket/required-status differs from template
          "orphaned": [param, ...],  # interview-only rows no longer in template
        }
    """
    analysis_dir = Path(analysis_dir)
    interview_path = analysis_dir / ".brieflow" / "interview.json"
    interview = load_interview(interview_path)

    if template_path is None:
        template_path = (
            Path(__file__).resolve().parent.parent / "data" / "interview_template.json"
        )
    template = json.loads(Path(template_path).read_text())
    cur_sha = template.get("_csv_sha")
    interview_sha = interview.get("_csv_sha")

    iv_by_key = {(r["notebook"], r["param_name"]): r for r in interview["params"]}
    tmpl_by_key = {(r["notebook"], r["param_name"]): r for r in template["params"]}

    filled: list[str] = []
    missing: list[str] = []
    stale: list[str] = []

    for key, tmpl_row in tmpl_by_key.items():
        if key[0] != phase:
            continue
        name = key[1]
        tmpl_bucket = tmpl_row.get("bucket")
        iv_row = iv_by_key.get(key)
        if iv_row is None:
            # Newly added in template; not yet in interview at all.
            if tmpl_bucket in ("user", "user_tuned_seed"):
                missing.append(name)
            continue
        if not is_active(iv_row, interview):
            continue
        bucket_drift = iv_row.get("bucket") != tmpl_bucket
        row_sha = iv_row.get("_csv_sha_at_set") or interview_sha
        sha_drift = bool(cur_sha and row_sha and row_sha != cur_sha)
        if bucket_drift or sha_drift:
            stale.append(name)
        # A row counts as "filled" when EITHER:
        #   - value is non-null (the common case), OR
        #   - value is null BUT value_source is set (operator/auto-probe/etc.
        #     intentionally confirmed null — e.g. SBS_CHANNEL_ORDER=null for
        #     multichannel ND2, *_METADATA_*=null for ND2 sidecar-metadata
        #     fields that don't apply to this screen). Distinguishing
        #     "operator addressed this and the answer is null" from "operator
        #     never saw this row" is what value_source!=None signals.
        if iv_row.get("value") is not None or iv_row.get("value_source") is not None:
            filled.append(name)
        elif tmpl_bucket in ("user", "user_tuned_seed"):
            missing.append(name)

    orphaned: list[str] = [
        key[1]
        for key, _ in iv_by_key.items()
        if key[0] == phase and key not in tmpl_by_key
    ]

    return {
        "filled": filled,
        "missing": missing,
        "stale": stale,
        "orphaned": orphaned,
    }


def expected_operator_params(
    analysis_dir: str | Path,
    phase: str,
    template_path: str | Path | None = None,
) -> dict[str, str]:
    """Return `{param_name: bucket}` for params currently expected to have
    a marker in this phase's notebook.

    Reads the CURRENT TEMPLATE (not the interview), so orphan rows (rows
    that exist only in interview.json from a prior CSV schema) are not
    included. Evaluates per-row gating (`only when X==Y`) against the
    current interview values, so conditional rows that aren't applicable
    (e.g. multi-mode-only params when `barcode_type=simple`) are also
    excluded.

    The warn-vs-block downgrade (archival / output / tuned-output /
    nonexistent → WARN) is applied by `preflight_notebook` against the
    bucket values returned here.
    """
    analysis_dir = Path(analysis_dir)
    if template_path is None:
        template_path = (
            Path(__file__).resolve().parent.parent / "data" / "interview_template.json"
        )
    template = json.loads(Path(template_path).read_text())
    interview = load_interview(analysis_dir / ".brieflow" / "interview.json")
    return {
        r["param_name"]: r["bucket"]
        for r in template["params"]
        if r["notebook"] == phase and is_active(r, interview)
    }


# ---------------------------------------------------------------------------
# Up-front interview engine (runner bootstrap)
# ---------------------------------------------------------------------------

# _OPERATOR_BUCKETS (the buckets the operator answers up front) is defined in
# brieflow_buckets and re-exported above. Everything else is resolved without
# asking.


def load_notebook_defaults(lab_defaults_path: str | Path | None = None) -> dict:
    """Return {(notebook, param_name_lower): value} from lab_defaults.yaml
    `notebook_defaults`. These are values the lab has standardized across screens
    (proven invariant); they fill still-null operator rows during resolution.
    Returns {} if the file or section is absent."""
    import yaml as _yaml

    p = Path(lab_defaults_path) if lab_defaults_path else _DEFAULT_LAB_DEFAULTS
    if not p.exists():
        return {}
    data = _yaml.safe_load(p.read_text()) or {}
    out: dict[tuple, Any] = {}
    for nb, params in (data.get("notebook_defaults") or {}).items():
        for name, val in (params or {}).items():
            out[(nb, name.lower())] = val
    return out


def _separate_design_and_library(iv: dict) -> bool:
    """Keep the standardized barcode library off the raw design table's path.

    The SBS notebook writes DF_BARCODE_LIBRARY_FP, so when it names the same file as the
    design (DF_DESIGN_FP, from screen.yaml) the design would be overwritten. A library path
    that was not set by the operator moves to ``<design stem>_standardized.tsv``."""
    design = find_param(iv, "DF_DESIGN_FP", notebook="sbs")
    library = find_param(iv, "DF_BARCODE_LIBRARY_FP", notebook="sbs")
    if not design or not library or not design.get("value") or not library.get("value"):
        return False
    if Path(str(design["value"])).as_posix() != Path(str(library["value"])).as_posix():
        return False
    if library.get("value_source") == "operator":
        return False
    p = Path(str(design["value"]))
    set_value(iv, "DF_BARCODE_LIBRARY_FP", str(p.with_name(f"{p.stem}_standardized.tsv")),
              "lab-default", notebook="sbs")
    return True


def fill_lab_defaults(iv: dict, nd_map: dict) -> bool:
    """Fill active, still-null rows from lab_defaults `notebook_defaults`; True if any was.

    Runs to a fixpoint: a row gated on a driver that is itself lab-filled later in the
    list would be skipped on a single pass and never revisited. Bounded by row count.
    """
    changed = False
    for _ in range(len(iv["params"]) + 1):
        pass_filled = False
        for row in iv["params"]:
            if not is_active(row, iv):
                continue
            if row.get("value") is not None or row.get("value_source") is not None:
                continue
            key = (row.get("notebook"), (row.get("param_name") or "").lower())
            if key in nd_map:
                set_value(iv, row["param_name"], nd_map[key], "lab-default",
                          notebook=row.get("notebook"))
                pass_filled = True
                changed = True
        if not pass_filled:
            break
    return changed


def resolve_all_phases(
    analysis_dir: str | Path,
    screen_yaml_path: str | Path | None = None,
    template_path: str | Path | None = None,
    lab_defaults_path: str | Path | None = None,
) -> dict:
    """Up-front interview: bootstrap screen.yaml derivations, fill lab-standard
    defaults, then collect the active, still-null operator rows across all phases
    (the gaps to ask once).

    Precedence for a row's value: operator / screen.yaml (already set) >
    lab_defaults.notebook_defaults > notebook baked-in default. Lab-default fills
    are written back to the interview (value_source="lab-default") so the marker
    patch step (interactive or runner) applies them.

    Returns::

        {
            "filled": [param_name, ...],   # active rows with a value/value_source
            "gaps":   [{"param_name": str, "notebook": str, "notes": str}, ...],
        }
    """
    analysis_dir = Path(analysis_dir)
    iv_path = analysis_dir / ".brieflow" / "interview.json"
    tmpl = Path(template_path) if template_path else _DEFAULT_TEMPLATE
    if not iv_path.exists():
        init_screen_interview(
            tmpl,
            iv_path,
            _screen_id(analysis_dir, screen_yaml_path),
            screen_yaml_path=screen_yaml_path,
        )
    iv = load_interview(iv_path)
    if screen_yaml_path is None:
        screen_yaml_path = analysis_dir / "screen.yaml"
    screen = _load_screen_yaml(screen_yaml_path)
    refreshed = False
    template = json.loads(Path(tmpl).read_text())
    if template.get("_csv_sha") and iv.get("_csv_sha") != template.get("_csv_sha"):
        # The plugin's template changed since this interview was built: adopt its row definitions.
        if sync_interview_with_template(iv, template):
            apply_screen_yaml_derivations(iv, screen, only_unset_or_from_screen=True)
            refreshed = True
    sha = _file_sha(screen_yaml_path)
    if sha and iv.get("_screen_yaml_sha256") != sha:
        # screen.yaml changed since the interview was built: re-derive what it supplies.
        apply_screen_yaml_derivations(iv, screen, only_unset_or_from_screen=True)
        iv["_screen_yaml_sha256"] = sha
        refreshed = True

    # MERGE_APPROACH gates the merge rows, so it resolves before the lab fill.
    from brieflow_probe import probe_phenotype_cells_per_tile

    density = probe_phenotype_cells_per_tile(analysis_dir)
    before = json.dumps(find_param(iv, "MERGE_APPROACH", notebook="merge"), sort_keys=True)
    apply_merge_approach(iv, screen, density.get("median_cells_per_tile"))
    after = json.dumps(find_param(iv, "MERGE_APPROACH", notebook="merge"), sort_keys=True)
    changed = before != after or refreshed

    changed = fill_lab_defaults(iv, load_notebook_defaults(lab_defaults_path)) or changed
    changed = _separate_design_and_library(iv) or changed

    # Final read-only pass: classify active rows into filled vs operator gaps.
    filled: list[str] = []
    gaps: list[dict] = []
    for row in iv["params"]:
        if not is_active(row, iv):
            continue
        if row.get("value") is not None or row.get("value_source") is not None:
            filled.append(row["param_name"])
            continue
        # Only operator-bucket rows are up-front questions. agent-resolved (sweep
        # winners / eval-table selections) and auto/auto-probe (notebook- or data-
        # derived) are resolved during the run, never asked.
        if row.get("bucket") in _OPERATOR_BUCKETS:
            gaps.append(
                {
                    "param_name": row["param_name"],
                    "notebook": row["notebook"],
                    "notes": row.get("notes", ""),
                }
            )
    if changed:
        save_interview(iv, iv_path)
    out = {"filled": filled, "gaps": gaps}
    # Surface candidate lab-default overrides implied by the operator's prose
    # additional_notes (the agent/operator confirms; nothing is auto-applied).
    out["note_overrides"] = scan_additional_notes(screen, iv)
    return out


def _load_screen_yaml(path: str | Path | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        import yaml as _yaml

        return _yaml.safe_load(p.read_text()) or {}
    except Exception:
        return {}


# Candidate-override patterns keyed to common Cheeseman lab-note phrasings. Each
# entry: (compiled regex, param it implies, short suggestion). These SURFACE
# overrides for agent/operator review — they are never auto-applied.
_NOTE_PATTERNS = [
    (re.compile(r"(first|1st)\b.{0,40}\bcycle\b.{0,40}"
                r"(bad|poor|off|drop|skip|left it out|left out|did ?n.?t trust|misalign|blurr|out of focus)",
                re.I | re.S), "SKIP_CYCLES", "a cycle looks bad — consider skipping it"),
    (re.compile(r"\b(drop(ped)?|skip(ped)?|exclud\w*|left out)\b.{0,30}\bcycle", re.I), "SKIP_CYCLES",
     "a cycle is being dropped — set SKIP_CYCLES"),
    (re.compile(r"(not round|irregular|hard to segment|difficult to segment|spread out|odd shape|"
                r"weird shape|non-?circular)", re.I), "SEGMENTATION_METHOD",
     "cells hard to segment — consider a different model (e.g. cpsam)"),
    (re.compile(r"(faded|disappear\w*|signal\b.{0,20}(lost|gone|went away)|ignor\w*|"
                r"do ?n.?t (use|analyz\w*)|not (using|analyz\w*))", re.I), "REMOVE_CHANNEL",
     "a channel may be unusable — consider removing/excluding it"),
    (re.compile(r"(puncta|foci|sharp spots|distinct spots|bright spots|spot-?like)", re.I), "FOCI_CHANNEL",
     "a punctate marker is described — confirm the foci channel"),
]


def scan_additional_notes(screen: dict, interview: dict | None = None) -> list[dict]:
    """Scan screen.yaml `additional_notes` (free-text operator prose) for phrases
    that imply an override of a lab default / a key-decision, and return candidate
    overrides for the agent/operator to confirm.

    Returns a list of ``{"param", "suggestion", "excerpt"}`` — one per matching
    sentence. Heuristic + conservative: it SURFACES candidates, never applies
    them (the agent maps prose → set_value after confirming with the operator).

    If ``interview`` is given, only candidates whose ``param`` is a real interview
    row are emitted — so a template rename can't leave the patterns pointing at a
    parameter that no longer exists.
    """
    notes = (screen or {}).get("additional_notes") or ""
    if not isinstance(notes, str) or not notes.strip():
        return []
    # Block-scalar notes wrap mid-sentence; collapse internal whitespace so a
    # phrase split across a line break ("bright\nspots") still matches, then
    # split into sentences on terminal punctuation only.
    flat = re.sub(r"\s+", " ", notes).strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", flat) if s.strip()]
    out: list[dict] = []
    seen: set = set()
    for sent in sentences:
        for rx, param, suggestion in _NOTE_PATTERNS:
            if rx.search(sent):
                key = (param, sent)
                if key in seen:
                    continue
                # If we have the interview, only surface params that really exist
                # (a rename shouldn't leave the pattern pointing at a dead param).
                if interview is not None and find_param(interview, param) is None:
                    continue
                seen.add(key)
                out.append({"param": param, "suggestion": suggestion, "excerpt": sent})
    return out


# Phrases that assert the run was clean/unremarkable. Matching one of these in
# the prose while the data census shows an anomaly is the damavand failure mode:
# the note was trusted and never checked against the actual filetree.
_REASSURING_RX = re.compile(
    r"nothing unusual|standard\s+(?:run|sequencing)|no issues|went cleanly|"
    r"clean run|as expected|uneventful",
    re.I,
)

# Explicit cycle count stated in prose ("11 cycles", "12 sequencing cycles").
_CYCLE_COUNT_RX = re.compile(r"(\d+)\s*(?:sequencing\s*)?cycles", re.I)


def _census_int_map(m: Any) -> dict:
    """Normalize a census dict whose keys may be ints or strs (a json
    round-trip turns ``{2: 1}`` into ``{"2": 1}``). Coerce keys to int where
    possible so callers can index by plate number regardless of provenance."""
    if not isinstance(m, dict):
        return {}
    out: dict = {}
    for k, v in m.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            out[k] = v
    return out


def verify_notes_against_data(screen: dict, census: dict) -> list[dict]:
    """Reconcile the operator's free-text ``additional_notes`` prose against a
    data census (the dict from ``census_tree`` in brieflow_validate_tree.py) and
    return a list of contradictions.

    The note is operator prose and is otherwise trusted; this catches the case
    where the prose claims a clean/standard run but the raw filetree disagrees —
    e.g. damavand's "Standard sequencing run, nothing unusual" alongside
    non-uniform, surplus sequencing cycles and literal "Dont use" junk dirs.

    Each returned dict is ``{"kind", "severity", "message", "excerpt"}`` with
    severity ``"warning"`` | ``"action_needed"``. Returns ``[]`` when there is
    no note or no contradiction. Pure: reads only the passed dicts. Defensive
    against missing keys, a None / non-string note, and int-vs-str census keys.
    """
    notes = (screen or {}).get("additional_notes")
    if not isinstance(notes, str) or not notes.strip():
        return []
    census = census or {}
    flat = re.sub(r"\s+", " ", notes).strip()

    # Pull census facts defensively (keys may be missing or json-coerced).
    cycles_per_plate = _census_int_map(census.get("cycles_per_plate"))
    surplus_cycles = _census_int_map(census.get("surplus_cycles"))
    junk_dirs = [j for j in (census.get("junk_dirs") or []) if j]
    uniform_cycles = census.get("uniform_cycles", True)
    max_cycles = census.get("max_cycles") or 0
    declared = census.get("declared_cycles")

    declared_mismatch = declared is not None and max_cycles != declared
    has_anomaly = (
        uniform_cycles is False
        or bool(surplus_cycles)
        or bool(junk_dirs)
        or declared_mismatch
    )

    out: list[dict] = []
    seen_kinds: set = set()

    def _emit(kind: str, severity: str, message: str, excerpt: str = "") -> None:
        if kind in seen_kinds:
            return
        seen_kinds.add(kind)
        out.append(
            {"kind": kind, "severity": severity, "message": message, "excerpt": excerpt}
        )

    # Rule 1: a reassuring "clean run" claim while the data shows any anomaly.
    m = _REASSURING_RX.search(flat)
    if m and has_anomaly:
        anomalies: list[str] = []
        if uniform_cycles is False:
            per = {f"plate_{k}": v for k, v in sorted(cycles_per_plate.items())}
            anomalies.append(f"non-uniform sequencing-cycle counts ({per})")
        if surplus_cycles:
            per = {f"plate_{k}": v for k, v in sorted(surplus_cycles.items())}
            anomalies.append(f"surplus cycles beyond declared ({per})")
        if declared_mismatch:
            anomalies.append(
                f"max cycle count {max_cycles} != declared iss.cycles {declared}"
            )
        if junk_dirs:
            anomalies.append(f"junk dirs in the regex path ({', '.join(junk_dirs)})")
        _emit(
            "reassuring-claim-vs-anomaly",
            "action_needed",
            "note describes a clean/standard run but the raw data shows: "
            + "; ".join(anomalies),
            _excerpt_around(flat, m.start()),
        )

    # Rule 2: an explicit cycle count in prose that disagrees with the data.
    cm = _CYCLE_COUNT_RX.search(flat)
    if cm:
        stated = int(cm.group(1))
        ref = declared if declared is not None else (max_cycles or None)
        if ref is not None and stated != ref:
            ref_label = (
                f"declared iss.cycles {declared}"
                if declared is not None
                else f"observed max of {max_cycles}"
            )
            _emit(
                "cycle-count-mismatch",
                "warning",
                f"note states {stated} cycles but the data shows {ref_label}",
                _excerpt_around(flat, cm.start()),
            )

    # Rule 3: junk dirs sitting in the regex path.
    if junk_dirs:
        _emit(
            "junk-dirs-present",
            "warning",
            "non-cycle/non-round dirs sit in the regex path: "
            + ", ".join(junk_dirs),
            "",
        )

    # Rule 4: per-plate cycle counts differ.
    if uniform_cycles is False:
        per = {f"plate_{k}": v for k, v in sorted(cycles_per_plate.items())}
        _emit(
            "non-uniform-cycles",
            "warning",
            f"sequencing-cycle counts differ across plates: {per}",
            "",
        )

    return out


def _excerpt_around(flat: str, pos: int, width: int = 60) -> str:
    """A short snippet of the (whitespace-collapsed) note centered near ``pos``,
    for surfacing which phrase triggered a contradiction."""
    start = max(0, pos - 10)
    snippet = flat[start : start + width].strip()
    if start > 0:
        snippet = "…" + snippet
    if start + width < len(flat):
        snippet = snippet + "…"
    return snippet


# ---------------------------------------------------------------------------
# Review — a human-checkable summary of how every parameter got resolved, so the
# operator can double-check the (large) auto-resolved set before committing to a
# run. Read-only; classifies each active row by its provenance.
# ---------------------------------------------------------------------------

#: value_source / bucket -> review category. Order = display order + severity.
_REVIEW_CATEGORY = {
    "operator": "operator-set",
    "tuned-confirmed": "operator-set",
    "screen-yaml": "from screen.yaml",
    "lab-default": "lab default",
    "default": "derived",
    "gen": "derived",
    "auto-probe": "auto-probed",
}


def _review_category(row: dict) -> str:
    """Classify a resolved interview row for the review report."""
    vs = row.get("value_source")
    if vs in _REVIEW_CATEGORY:
        return _REVIEW_CATEGORY[vs]
    if row.get("value") is not None:
        return "derived"
    # still unfilled — resolves where?
    b = row.get("bucket")
    if b in _OPERATOR_BUCKETS:
        return "NEEDS ANSWER"
    if b == "auto-probe":
        return "pending: auto-probe at runtime"
    if b == "agent-resolved":
        return "pending: sweep at runtime"
    return "pending"


def interview_review(
    interview: dict, notebook: str | None = None, note_overrides: list | None = None
) -> dict:
    """Build a human-checkable review of how every active param resolved — for the
    operator to double-check before a run (or per-notebook). Read-only.

    Returns::

        {"rows":    [{"notebook","param","value","source","category"}, ...],
         "summary": {category: count, ...},
         "attention": {"needs_answer": [...], "note_overrides": [...],
                       "derived_decisions": [...], "runtime_pending": [...]}}

    `rows` lists active params in template order. `attention` calls out what the
    operator should look at: rows still to answer, prose-derived override
    candidates (pass `note_overrides` from resolve_all_phases), and values that
    won't exist until the run (auto-probe / sweep)."""
    rows: list[dict] = []
    summary: dict[str, int] = {}
    needs_answer: list[str] = []
    runtime_pending: list[str] = []
    detected_confirm: list[dict] = []
    derived_decisions: list[dict] = []
    for r in interview.get("params", []):
        if notebook is not None and r.get("notebook") != notebook:
            continue
        if not is_active(r, interview):
            continue
        cat = _review_category(r)
        summary[cat] = summary.get(cat, 0) + 1
        entry = {
            "notebook": r.get("notebook"),
            "param": r.get("param_name"),
            "value": r.get("value"),
            "source": r.get("value_source"),
            "category": cat,
        }
        rows.append(entry)
        if r.get("reason"):
            derived_decisions.append(
                {
                    "param": f"{r['notebook']}.{r['param_name']}",
                    "value": r.get("value"),
                    "source": r.get("value_source"),
                    "reason": r["reason"],
                }
            )
        if cat == "NEEDS ANSWER":
            needs_answer.append(f"{r['notebook']}.{r['param_name']}")
        elif cat.startswith("pending"):
            runtime_pending.append(f"{r['notebook']}.{r['param_name']}")
        # Decision-bearing rows the data-probe *inferred* (path patterns, channel
        # orientation, format/organization). Promote them out of the skim list into
        # an explicit confirm section — these are exactly the values that look
        # resolved but can be silently wrong (e.g. a path pattern missing `cycle`).
        if (
            r.get("value_source") == "auto-probe"
            and r.get("param_name") in _DETECTED_CONFIRM_PARAMS
            and r.get("value") not in (None, "", [], {})
        ):
            detected_confirm.append(
                {"param": f"{r['notebook']}.{r['param_name']}", "value": r.get("value")}
            )
    return {
        "rows": rows,
        "summary": summary,
        "attention": {
            "needs_answer": needs_answer,
            "note_overrides": list(note_overrides or []),
            "detected_confirm": detected_confirm,
            "derived_decisions": derived_decisions,
            "runtime_pending": runtime_pending,
        },
    }


#: Probe-inferred, decision-bearing preprocess params. When auto-probe fills these
#: they look "resolved" but can be silently wrong (a path pattern missing `cycle`, a
#: mis-oriented channel flip), so the review surfaces them for explicit confirmation
#: rather than burying them as "nothing to do now".
_DETECTED_CONFIRM_PARAMS = {
    "SBS_PATH_PATTERN", "PHENOTYPE_PATH_PATTERN",
    "SBS_PATH_METADATA", "PHENOTYPE_PATH_METADATA",
    "SBS_CHANNEL_ORDER", "PHENOTYPE_CHANNEL_ORDER",
    "SBS_CHANNEL_ORDER_FLIP", "PHENOTYPE_CHANNEL_ORDER_FLIP",
    "SBS_DATA_FORMAT", "PHENOTYPE_DATA_FORMAT",
    "SBS_DATA_ORGANIZATION", "PHENOTYPE_DATA_ORGANIZATION",
    "PHENOTYPE_ROUND_ORDER",
}


#: review categories that the operator should actively confirm (vs just skim).
_REVIEW_ORDER = [
    "NEEDS ANSWER", "operator-set", "from screen.yaml", "lab default",
    "derived", "auto-probed", "pending: auto-probe at runtime",
    "pending: sweep at runtime", "pending",
]


def render_interview_review(review: dict) -> str:
    """Render :func:`interview_review` as readable markdown for the operator."""
    lines: list[str] = ["# Interview review — confirm before running", ""]
    s = review["summary"]
    # _REVIEW_ORDER first (display order), then any category not in it — so a new
    # value_source never silently drops out of the summary count.
    ordered = [c for c in _REVIEW_ORDER if c in s] + [c for c in s if c not in _REVIEW_ORDER]
    lines.append("**Resolved:** " + ", ".join(f"{s[c]} {c}" for c in ordered) + ".")
    att = review["attention"]
    if att["needs_answer"]:
        lines += ["", "## ⛔ Needs your answer", *[f"- {x}" for x in att["needs_answer"]]]
    if att["note_overrides"]:
        lines += ["", "## 📝 From your notes — confirm these overrides"]
        for o in att["note_overrides"]:
            lines.append(f"- **{o['param']}**: {o['suggestion']}  — _\"{o['excerpt']}\"_")
    if att.get("detected_confirm"):
        lines += ["", "## 📋 Detected from your data — confirm before launch"]
        for d in att["detected_confirm"]:
            v = d["value"]
            shown = v if isinstance(v, str) else repr(v)
            lines.append(f"- **{d['param']}** = {shown}")
    if att.get("derived_decisions"):
        lines += ["", "## 🧭 Derived decisions — set the value yourself to override"]
        for d in att["derived_decisions"]:
            lines.append(f"- **{d['param']}** = {d['value']!r} ({d['source']}): {d['reason']}")
    if att["runtime_pending"]:
        lines += ["", f"## ⏳ Resolved at runtime ({len(att['runtime_pending'])}: "
                  "auto-probe / sweep) — nothing to do now"]
    # full per-phase listing
    by_phase: dict = {}
    for r in review["rows"]:
        by_phase.setdefault(r["notebook"], []).append(r)
    lines += ["", "## All resolved parameters (by phase)"]
    for ph in PHASES + [k for k in by_phase if k not in PHASES]:
        if ph not in by_phase:
            continue
        lines += ["", f"### {ph}"]
        for r in by_phase[ph]:
            val = r["value"]
            shown = "—" if val is None else (val if isinstance(val, str) else repr(val))
            lines.append(f"- `{r['param']}` = {shown}  [{r['category']}]")
    return "\n".join(lines)


def render_interview_review_html(review: dict) -> str:
    """Render :func:`interview_review` as a self-contained HTML summary artifact —
    the one-glance "everything that was auto-decided" view for the run.

    Same content as :func:`render_interview_review` (badge counts, the attention
    sections, then every active param grouped by phase), as a portable HTML file
    the operator opens instead of scrolling a chat log.
    """
    from html import escape

    def esc(v) -> str:
        if v is None:
            return "—"
        return escape(v if isinstance(v, str) else repr(v))

    s = review["summary"]
    ordered = [c for c in _REVIEW_ORDER if c in s] + [c for c in s if c not in _REVIEW_ORDER]
    att = review["attention"]

    p: list[str] = [
        '<!doctype html><html><head><meta charset="utf-8">',
        "<title>Interview review</title><style>",
        "body{font-family:system-ui,sans-serif;margin:1.5rem;background:#fafafa;color:#222}",
        "h1{font-size:1.3rem}h2{font-size:1.05rem;margin-top:1.4rem}h3{font-size:.95rem;color:#333}",
        ".badges span{display:inline-block;background:#eef;border:1px solid #ccd;border-radius:12px;"
        "padding:2px 10px;margin:2px;font-size:.85rem}",
        ".attn{background:#fff6f6;border-left:3px solid #d66;padding:.4rem .8rem;margin:.4rem 0}",
        "table{border-collapse:collapse;width:100%;margin-top:.4rem}",
        "td,th{border:1px solid #ddd;padding:4px 8px;font-size:.85rem;text-align:left}",
        "th{background:#f0f0f0}code{background:#f6f6f6;padding:1px 4px;border-radius:3px}",
        "</style></head><body>",
        "<h1>Interview review — confirm before running</h1>",
        '<div class="badges">'
        + "".join(f"<span>{s[c]} {escape(c)}</span>" for c in ordered)
        + "</div>",
    ]

    if att["needs_answer"]:
        p.append("<h2>⛔ Needs your answer</h2><div class='attn'><ul>")
        p += [f"<li>{escape(str(x))}</li>" for x in att["needs_answer"]]
        p.append("</ul></div>")
    if att["note_overrides"]:
        p.append("<h2>📝 From your notes — confirm these overrides</h2><ul>")
        for o in att["note_overrides"]:
            p.append(
                f"<li><b>{escape(o['param'])}</b>: {escape(str(o['suggestion']))} — "
                f"<i>\"{escape(str(o['excerpt']))}\"</i></li>"
            )
        p.append("</ul>")
    if att.get("detected_confirm"):
        p.append("<h2>📋 Detected from your data — confirm before launch</h2><ul>")
        for d in att["detected_confirm"]:
            p.append(f"<li><b>{escape(d['param'])}</b> = {esc(d['value'])}</li>")
        p.append("</ul>")
    if att.get("derived_decisions"):
        p.append("<h2>🧭 Derived decisions — set the value yourself to override</h2><ul>")
        for d in att["derived_decisions"]:
            p.append(
                f"<li><b>{escape(d['param'])}</b> = {esc(d['value'])} "
                f"({escape(str(d['source']))}): {escape(str(d['reason']))}</li>"
            )
        p.append("</ul>")
    if att["runtime_pending"]:
        p.append(
            f"<h2>⏳ Resolved at runtime ({len(att['runtime_pending'])}: "
            "auto-probe / sweep) — nothing to do now</h2>"
        )

    by_phase: dict = {}
    for r in review["rows"]:
        by_phase.setdefault(r["notebook"], []).append(r)
    p.append("<h2>All resolved parameters (by phase)</h2>")
    for ph in PHASES + [k for k in by_phase if k not in PHASES]:
        if ph not in by_phase:
            continue
        p.append(f"<h3>{escape(ph)}</h3><table><tr><th>Parameter</th><th>Value</th><th>Source</th></tr>")
        for r in by_phase[ph]:
            p.append(
                f"<tr><td><code>{escape(r['param'])}</code></td><td>{esc(r['value'])}</td>"
                f"<td>{escape(str(r.get('category') or r.get('source') or ''))}</td></tr>"
            )
        p.append("</table>")
    p.append("</body></html>")
    return "".join(p)


def apply_preprocess_probe(
    interview: dict, report: dict, *, notebook: str = "preprocess"
) -> list[str]:
    """Write a ``probe_preprocess()`` report's prefilled values into the
    interview with ``value_source="auto-probe"``. Operator-bucket rows the probe
    filled then count as resolved (``resolve_all_phases`` won't ask them); rows
    the probe couldn't fill (``report["operator_needed"]``) stay unfilled and are
    asked as normal. Returns the param names written.

    Pairs with the data-probe: the probe inspects the raw data, this records its
    findings into the operator-visible interview without flipping any bucket.
    """
    written: list[str] = []
    for param, value in (report.get("prefilled") or {}).items():
        try:
            # Never clobber an explicit operator override. The probe can't
            # determine some rows (e.g. channel_order_flip, which it hard-defaults
            # to False) — when the operator has set one, auto-probe must defer.
            row = find_param(interview, param, notebook=notebook)
            if (
                row is not None
                and row.get("value_source") == "operator"
                and row.get("value") not in (None, "", [], {})
            ):
                continue
            set_value(interview, param, value, "auto-probe", notebook=notebook)
            written.append(param)
        except (KeyError, ValueError):
            pass
    rounds = (report.get("prefilled") or {}).get("PHENOTYPE_ROUND_ORDER")
    align = find_param(interview, "ALIGN", notebook="phenotype")
    if isinstance(rounds, list) and len(rounds) > 1 and align is not None and align.get(
        "value_source"
    ) in (None, "auto-probe"):
        # Several phenotype rounds were imaged in separate passes, so their channels are aligned.
        set_value(interview, "ALIGN", True, "auto-probe", notebook="phenotype")
        written.append("ALIGN")
    return written


# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
