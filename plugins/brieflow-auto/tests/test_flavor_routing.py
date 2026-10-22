"""Flavor-routing tests for brieflow_wizard.py.

The plugin must stay agnostic to screen flavor and route the correct path from
the interview gating layer — never hardcode a flavor, never read the wrong
notebook's flag. A variety of screens (nuclei-only vs cell+nucleus, single- vs
multi-round phenotype) all flow through the same template; these tests pin that
the routing reads the RIGHT flag for the RIGHT phase.

Covers the axes the snowdon run exposed (nuclei-only F08/F16, multi-round
channel mapping).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_wizard.py"
TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "interview_template.json"


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def iv(bw, tmp_path):
    dst = tmp_path / ".brieflow" / "interview.json"
    bw.init_screen_interview(TEMPLATE, dst, "flavor-screen")
    return bw.load_interview(dst)


# ---------------------------------------------------------------------------
# Cross-notebook gating isolation — `segment_cells`, `cyto_channel`, `cyto_index`
# each exist in BOTH sbs and phenotype. A gate `only when segment_cells==true`
# on a phenotype row must read PHENOTYPE's segment_cells, not whichever row is
# first in the param list. Otherwise a nuclei-only phenotype routes wrong.
# ---------------------------------------------------------------------------


def test_phenotype_cyto_gating_reads_phenotype_flag_when_sbs_disagrees(bw, iv):
    """Nuclei-only phenotype, but SBS segments cells: the two `segment_cells`
    rows disagree. The phenotype `cyto_channel` gate must read phenotype's
    flag (False) → inactive. Reading the SBS flag (True) would wrongly ask for
    a cytoplasm channel on a nuclei-only screen and feed the F08 crash path."""
    bw.find_param(iv, "SEGMENT_CELLS", notebook="sbs")["value"] = True
    bw.find_param(iv, "SEGMENT_CELLS", notebook="phenotype")["value"] = False
    ph_cyto = bw.find_param(iv, "CYTO_CHANNEL", notebook="phenotype")
    assert bw.is_active(ph_cyto, iv) is False


def test_sbs_cyto_gating_reads_sbs_flag_when_phenotype_disagrees(bw, iv):
    """Mirror: SBS segments cells, phenotype is nuclei-only. The SBS
    `cyto_channel` gate must read SBS's flag (True) → active."""
    bw.find_param(iv, "SEGMENT_CELLS", notebook="sbs")["value"] = True
    bw.find_param(iv, "SEGMENT_CELLS", notebook="phenotype")["value"] = False
    sbs_cyto = bw.find_param(iv, "CYTO_CHANNEL", notebook="sbs")
    assert bw.is_active(sbs_cyto, iv) is True


def test_nuclei_only_phenotype_routes_segmentation_rows_out(bw, iv):
    """Nuclei-only phenotype: CYTO_CHANNEL goes inactive so the wizard never asks
    for it and never sets a (crash-inducing) cyto value. (CYTO_INDEX is computed
    in-notebook from CYTO_CHANNEL — not an interview row.) SBS set True to prove
    the routing reads phenotype's own SEGMENT_CELLS flag, not the first match."""
    bw.find_param(iv, "SEGMENT_CELLS", notebook="sbs")["value"] = True
    bw.find_param(iv, "SEGMENT_CELLS", notebook="phenotype")["value"] = False
    row = bw.find_param(iv, "CYTO_CHANNEL", notebook="phenotype")
    assert bw.is_active(row, iv) is False, "CYTO_CHANNEL should be inactive nuclei-only"


def test_cell_plus_nucleus_phenotype_keeps_cyto_active(bw, iv):
    """Cell+nucleus phenotype: cyto_channel stays active (operator is asked)."""
    bw.find_param(iv, "SEGMENT_CELLS", notebook="phenotype")["value"] = True
    ph_cyto = bw.find_param(iv, "CYTO_CHANNEL", notebook="phenotype")
    assert bw.is_active(ph_cyto, iv) is True


# ---------------------------------------------------------------------------
# Multi-round phenotype channel mapping — confirm_channel_mapping auto-detects
# single- vs multi-round shape (snowdon: 2 rounds x 5 fluorophores).
# ---------------------------------------------------------------------------


def test_confirm_channel_mapping_single_round_returns_dict(bw, iv):
    out = bw.confirm_channel_mapping(iv, ["DAPI", "GFP"], ["DAPI", "COXIV"], side="phenotype")
    assert out == {"DAPI": "DAPI", "GFP": "COXIV"}


def test_confirm_channel_mapping_multiround_returns_per_round_dicts(bw, iv):
    tokens = ["DAPI", "GFP", "Cy3", "Cy5", "AF750"]
    bio = [
        ["DAPI", "ATACsee", "H4Kacme", "pSer5", "KI67"],
        ["DAPI2", "H3K4me3", "H3K27ac", "pSer2", "H3K9me3"],
    ]
    out = bw.confirm_channel_mapping(iv, tokens, bio, side="phenotype", round_order=[1, 2])
    assert isinstance(out, list) and len(out) == 2
    assert out[0]["GFP"] == "ATACsee"
    assert out[1]["GFP"] == "H3K4me3"


def test_confirm_channel_mapping_multiround_requires_round_order(bw, iv):
    bio = [["DAPI", "X"], ["DAPI2", "Y"]]
    with pytest.raises(ValueError):
        bw.confirm_channel_mapping(iv, ["DAPI", "GFP"], bio, side="phenotype")


def test_confirm_channel_mapping_length_mismatch_raises(bw, iv):
    with pytest.raises(ValueError):
        bw.confirm_channel_mapping(iv, ["DAPI", "GFP", "Cy3"], ["DAPI", "COXIV"], side="phenotype")


# ---------------------------------------------------------------------------
# Non-foci axis — FOCI_CHANNEL is null_ok screen-yaml: null = non-foci screen,
# a value = foci screen. The FOCI_CHANNEL_INDEX is computed in-notebook from
# FOCI_CHANNEL (not an interview row), so the foci pipeline routes off FOCI_CHANNEL.
# ---------------------------------------------------------------------------


def test_non_foci_screen_has_null_foci_channel(bw, iv):
    # default null → non-foci screen; FOCI_CHANNEL is the only foci interview row
    assert bw.find_param(iv, "FOCI_CHANNEL")["value"] is None
    assert bw.find_param(iv, "FOCI_CHANNEL_INDEX") is None  # computed, not an interview row


def test_foci_screen_sets_foci_channel(bw, iv):
    row = bw.find_param(iv, "FOCI_CHANNEL")
    row["value"] = "CENPA"
    assert bw.find_param(iv, "FOCI_CHANNEL")["value"] == "CENPA"


# ---------------------------------------------------------------------------
# Bootstrap axis — BOOTSTRAP_CELL_CLASS / BOOTSTRAP_CHANNEL_COMBO are computed
# in-notebook (largest CHANNEL_COMBO / all CELL_CLASSES), so they are NOT
# interview rows.
# ---------------------------------------------------------------------------


def test_bootstrap_targets_are_computed_not_interview_rows(bw, iv):
    for name in ("BOOTSTRAP_CELL_CLASS", "BOOTSTRAP_CHANNEL_COMBO"):
        assert bw.find_param(iv, name) is None, name


# ---------------------------------------------------------------------------
# Structured `gating` blocks are now honored (previously ignored → the tiff-only
# metadata rows were wrongly active on nd2/zarr screens).
# ---------------------------------------------------------------------------


def test_structured_equals_gate_honored_on_nd2(bw, iv):
    bw.find_param(iv, "SBS_DATA_FORMAT")["value"] = "nd2"
    row = bw.find_param(iv, "SBS_METADATA_IMAGES_DIR_FP")
    assert isinstance(row.get("gating"), dict)  # uses a structured block
    assert bw.is_active(row, iv) is False


def test_structured_equals_gate_active_on_tiff(bw, iv):
    bw.find_param(iv, "SBS_DATA_FORMAT")["value"] = "tiff"
    row = bw.find_param(iv, "SBS_METADATA_IMAGES_DIR_FP")
    assert bw.is_active(row, iv) is True


# ---------------------------------------------------------------------------
# Structured gate operator semantics (synthetic rows — no template row uses
# not_equals / in yet, so exercise them directly).
# ---------------------------------------------------------------------------


def test_gate_op_not_equals(bw):
    iv = {"params": [{"param_name": "barcode_type", "notebook": "sbs", "value": "simple"}]}
    row = {"notebook": "sbs", "gating": {"param": "barcode_type", "op": "not_equals", "value": "simple"}}
    assert bw.is_active(row, iv) is False
    iv["params"][0]["value"] = "multi"
    assert bw.is_active(row, iv) is True


def test_gate_op_in(bw):
    iv = {"params": [{"param_name": "barcode_type", "notebook": "sbs", "value": "recomb"}]}
    row = {"notebook": "sbs", "gating": {"param": "barcode_type", "op": "in", "values": ["multi", "recomb"]}}
    assert bw.is_active(row, iv) is True
    iv["params"][0]["value"] = "simple"
    assert bw.is_active(row, iv) is False


def test_gate_unfilled_param_is_inactive(bw):
    iv = {"params": [{"param_name": "x", "notebook": "a", "value": None}]}
    row = {"notebook": "a", "gating": {"param": "x", "op": "equals", "value": "y"}}
    assert bw.is_active(row, iv) is False


def test_gate_unknown_op_raises(bw):
    iv = {"params": [{"param_name": "x", "notebook": "a", "value": "y"}]}
    row = {"notebook": "a", "gating": {"param": "x", "op": "bogus", "value": "y"}}
    with pytest.raises(ValueError):
        bw.is_active(row, iv)


# ---------------------------------------------------------------------------
# CHANNEL_CMAPS auto-sizing — must return exactly len(channel_names) entries so
# the viz cells never IndexError, regardless of channel count (multi-round).
# ---------------------------------------------------------------------------


def test_cmaps_match_channel_count_single_round(bw):
    out = bw.default_channel_cmaps(["DAPI", "COXIV", "CENPA", "WGA"])
    assert len(out) == 4
    assert all(c in bw.DEFAULT_CMAP_PALETTE for c in out)


def test_cmaps_no_indexerror_beyond_palette(bw):
    # 10 channels with a 6-color palette: old fixed default IndexErrored here.
    names = [f"ch{i}" for i in range(10)]
    out = bw.default_channel_cmaps(names)
    assert len(out) == 10
    assert all(c in bw.DEFAULT_CMAP_PALETTE for c in out)


def test_cmaps_multiround_aligns_colors_per_fluorophore(bw):
    # snowdon: 2 rounds x 5 fluorophores → round 2 repeats round 1's colors.
    names = [f"r{r}c{c}" for r in (1, 2) for c in range(5)]
    out = bw.default_channel_cmaps(names, n_rounds=2)
    assert len(out) == 10
    assert out[:5] == out[5:]


def test_cmaps_multiround_even_division_required(bw):
    # 9 channels / 2 rounds doesn't divide → falls back to flat cycling (no crash).
    out = bw.default_channel_cmaps([f"c{i}" for i in range(9)], n_rounds=2)
    assert len(out) == 9


def test_cmaps_base_palette_override(bw):
    out = bw.default_channel_cmaps(["a", "b", "c"], base_palette=["x", "y"])
    assert out == ["x", "y", "x"]


def test_cmaps_empty_palette_raises(bw):
    with pytest.raises(ValueError):
        bw.default_channel_cmaps(["a"], base_palette=[])


# ---------------------------------------------------------------------------
# Merge axis — fast/auto is standard. Orientation params gate on
# MERGE_APPROACH == "positions"; the fast-only rows on == "fast"; initial_sites /
# initial_sbs_tiles gate on the alignment approach.
# ---------------------------------------------------------------------------

_POSITIONS_ONLY = ("FLIPLR", "FLIPUD", "ROT90")
_FAST_ONLY = (
    "INITIAL_SITES_APPROACH", "DET_RANGE", "LOCAL_REFINEMENT", "WARP_DEGREE",
    "WARP_ITERATIONS", "WARP_SMOOTHING", "SEED_OPTIMIZE", "SEED_TOPK", "THRESHOLD_TRIANGLE",
)


@pytest.mark.parametrize("approach", ["fast", "positions"])
def test_merge_approach_gates_its_section(bw, iv, approach):
    bw.find_param(iv, "MERGE_APPROACH", notebook="merge")["value"] = approach
    for name in _POSITIONS_ONLY:
        row = bw.find_param(iv, name, notebook="merge")
        assert bw.is_active(row, iv) is (approach == "positions"), f"{name} ({approach})"
    for name in _FAST_ONLY:
        row = bw.find_param(iv, name, notebook="merge")
        assert bw.is_active(row, iv) is (approach == "fast"), f"{name} ({approach})"
    assert bw.is_active(bw.find_param(iv, "THRESHOLD", notebook="merge"), iv)


def test_merge_stitch_and_sbs_preview_rows_removed(bw, iv):
    names = {(r["notebook"], r["param_name"]) for r in iv["params"]}
    for gone in ("STITCH", "MASK_TYPE", "STITCHED_IMAGE", "NUM_TILES_PHENO", "NUM_TILES_SBS"):
        assert ("merge", gone) not in names
    for gone in ("VIZ_CYCLE", "DAPI_REFERENCE_CYCLE", "VIZ_CHANNELS"):
        assert ("sbs", gone) not in names


def test_merge_approach_is_derived_not_lab_default(bw, iv):
    assert ("merge", "merge_approach") not in bw.load_notebook_defaults()
    assert ("merge", "POSITIONS") not in {(r["notebook"], r["param_name"]) for r in iv["params"]}


def test_merge_auto_routes_initial_sbs_tiles_active(bw, iv):
    bw.find_param(iv, "INITIAL_SITES_APPROACH", notebook="merge")["value"] = "auto"
    assert bw.is_active(bw.find_param(iv, "INITIAL_SBS_TILES", notebook="merge"), iv) is True
    assert bw.is_active(bw.find_param(iv, "INITIAL_SITES", notebook="merge"), iv) is False


def test_merge_manual_routes_initial_sites_active(bw, iv):
    bw.find_param(iv, "INITIAL_SITES_APPROACH", notebook="merge")["value"] = "manual"
    assert bw.is_active(bw.find_param(iv, "INITIAL_SITES", notebook="merge"), iv) is True
    assert bw.is_active(bw.find_param(iv, "INITIAL_SBS_TILES", notebook="merge"), iv) is False


# ---------------------------------------------------------------------------
# Phenotype axis — channel-registration (align), segmentation method, cpsam.
# ---------------------------------------------------------------------------


def test_phenotype_registration_rows_inactive_when_no_align(bw, iv):
    bw.find_param(iv, "ALIGN", notebook="phenotype")["value"] = False
    for name in ("SOURCE", "RIDERS", "CUSTOM_CHANNEL_OFFSETS"):
        row = bw.find_param(iv, name, notebook="phenotype")
        assert bw.is_active(row, iv) is False, f"{name} should be inactive when align=False"


def test_phenotype_registration_rows_active_when_align(bw, iv):
    bw.find_param(iv, "ALIGN", notebook="phenotype")["value"] = True
    for name in ("SOURCE", "RIDERS", "CUSTOM_CHANNEL_OFFSETS"):
        row = bw.find_param(iv, name, notebook="phenotype")
        assert bw.is_active(row, iv) is True, f"{name} should be active when align=True"


def test_phenotype_cellpose_path_routes_models(bw, iv):
    bw.find_param(iv, "SEGMENTATION_METHOD", notebook="phenotype")["value"] = "cellpose"
    bw.find_param(iv, "CELLPOSE_MODEL", notebook="phenotype")["value"] = "cyto3"
    assert bw.is_active(bw.find_param(iv, "CELLPOSE_MODEL", notebook="phenotype"), iv) is True
    assert bw.is_active(bw.find_param(iv, "STARDIST_MODEL", notebook="phenotype"), iv) is False
    # HELPER_INDEX only for cpsam
    assert bw.is_active(bw.find_param(iv, "HELPER_INDEX", notebook="phenotype"), iv) is False


def test_phenotype_cpsam_routes_helper_index_active(bw, iv):
    bw.find_param(iv, "SEGMENTATION_METHOD", notebook="phenotype")["value"] = "cellpose"
    bw.find_param(iv, "CELLPOSE_MODEL", notebook="phenotype")["value"] = "cpsam"
    assert bw.is_active(bw.find_param(iv, "HELPER_INDEX", notebook="phenotype"), iv) is True


def test_phenotype_channel_cmaps_auto_derived(bw, tmp_path):
    import yaml as _y
    adir = tmp_path / "cmap-analysis"
    adir.mkdir()
    (adir / "screen.yaml").write_text(_y.dump({
        "collection": {"screen_id": "cmap"},
        "data": {"input_sbs_dir": "/s", "input_phenotype_dir": "/p", "storage_format": "nd2"},
        "iss": {"channels": [{"name": "DAPI"}], "background_channel": {"channel": "DAPI", "cycle": 1}},
        "phenotype": {"channels": [{"biological_name": "DAPI"},
                                   {"biological_name": "WGA"},
                                   {"biological_name": "CENPA"}],
                      "segment_cells": False, "background_channel_cell": None},
        "library": {"type": "simple", "negative_controls": ["nontargeting"]},
    }))
    bw.resolve_all_phases(adir)
    iv = bw.load_interview(adir / ".brieflow" / "interview.json")
    row = bw.find_param(iv, "CHANNEL_CMAPS", notebook="phenotype")
    assert row["value"] == ["pure_blue", "pure_green", "pure_red"]
    assert row["value_source"] == "screen-yaml"
