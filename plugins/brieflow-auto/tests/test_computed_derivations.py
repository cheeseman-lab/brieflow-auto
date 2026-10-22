"""Tests for the screen.yaml computed derivations added in the interview rebuild:
sbs_segment_cells (DNA/RNA rule) and channels_metadata (OME-Zarr annotation).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def bw():
    # Load via the facade (self-bootstraps sys.path so brieflow_interview's
    # sibling imports resolve); the new computes are re-exported there.
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPTS / "brieflow_wizard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


# --- sbs_segment_cells -------------------------------------------------------
def test_segment_cells_dna_is_false(bw):
    assert bw._compute_sbs_segment_cells({"library": {"detection_target": "DNA"}}) == {"segment_cells": False}


def test_segment_cells_rna_is_true(bw):
    assert bw._compute_sbs_segment_cells({"library": {"detection_target": "RNA"}}) == {"segment_cells": True}


def test_segment_cells_unset_is_none(bw):
    assert bw._compute_sbs_segment_cells({"library": {}}) is None
    assert bw._compute_sbs_segment_cells({}) is None


# --- call_reads_method (chemistry-conditional) -------------------------------
def test_call_reads_method_combinatorial_is_frac(bw):
    assert bw._compute_call_reads_method({"iss": {"chemistry": "combinatorial"}}) == {"call_reads_method": "frac"}


def test_call_reads_method_four_color_is_median(bw):
    # human-readable "four color" normalizes to four_color -> median
    assert bw._compute_call_reads_method({"iss": {"chemistry": "four color"}}) == {"call_reads_method": "median"}


def test_call_reads_method_unset_is_none(bw):
    assert bw._compute_call_reads_method({"iss": {}}) is None
    assert bw._compute_call_reads_method({}) is None


# --- channels_metadata -------------------------------------------------------
def test_channels_metadata_phenotype_pulls_annotation(bw):
    screen = {
        "phenotype": {"channels": [
            {"biological_name": "DAPI", "marker_of": "Nuclei", "marker": None, "marker_type": "dye"},
            {"biological_name": "GFP", "marker_of": "chaperones", "marker": "HSPA1B", "marker_type": "endogenous_tag"},
        ]}
    }
    out = bw._compute_channels_metadata(screen)
    ph = out["phenotype_channels_metadata"]
    assert len(ph) == 2
    assert ph[0]["name"] == "DAPI" and ph[0]["index"] == 0
    assert ph[1]["name"] == "GFP" and ph[1]["index"] == 1
    ann = ph[1]["biological_annotation"]
    assert ann["biological_target"] == "chaperones"
    assert ann["marker"] == "HSPA1B"
    assert ann["marker_type"] == "endogenous_tag"
    assert ann["full_label"] == "chaperones, HSPA1B"
    # color assigned per channel, channel_type defaulted
    assert ph[0]["channel_type"] == "fluorescent"
    assert ph[0]["color"] is not None


def test_channels_metadata_sbs_from_iss(bw):
    screen = {"iss": {"channels": [{"name": "DAPI"}, {"name": "G"}, {"name": "T"}]}}
    out = bw._compute_channels_metadata(screen)
    sbs = out["sbs_channels_metadata"]
    assert [e["name"] for e in sbs] == ["DAPI", "G", "T"]
    assert [e["index"] for e in sbs] == [0, 1, 2]


def test_channels_metadata_none_when_empty(bw):
    assert bw._compute_channels_metadata({}) is None


def test_channels_metadata_registered(bw):
    assert "channels_metadata" in bw._COMPUTED_DERIVATIONS
    assert "sbs_segment_cells" in bw._COMPUTED_DERIVATIONS


# --- barcode_cycle_positions + skip cycles -------------------------------------
def _positions(bw, **lib):
    import sys
    return sys.modules["brieflow_interview"]._compute_barcode_cycle_positions({"library": lib})


def test_cycle_positions_no_skips_unchanged(bw):
    r = _positions(bw, sequencing_order="map_recomb", barcode_length_map=7, barcode_length_recomb=5)
    assert (r["map_start"], r["map_end"], r["recomb_start"], r["recomb_end"]) == (1, 7, 8, 12)
    assert r["skip_cycles"] is None and r["skip_cycles_map"] is None and r["skip_cycles_recomb"] is None


def test_cycle_positions_skip_in_map_region_shrinks_everything_after(bw):
    """Cycle 6 misaligned. Skipping it drops one base from the aligned read,
    so MAP becomes 1-6 and RECOMB 7-11; the library must be standardized with
    SKIP_CYCLES_MAP=[6] (relative to the map prefix) and no recomb skip."""
    r = _positions(bw, sequencing_order="map_recomb", barcode_length_map=7, barcode_length_recomb=5, skip_cycles=[6])
    assert (r["map_start"], r["map_end"], r["recomb_start"], r["recomb_end"]) == (1, 6, 7, 11)
    assert r["skip_cycles"] == [6] and r["skip_cycles_map"] == [6] and r["skip_cycles_recomb"] is None


def test_cycle_positions_skip_in_recomb_region_is_relative(bw):
    r = _positions(bw, sequencing_order="map_recomb", barcode_length_map=7, barcode_length_recomb=5, skip_cycles=[9])
    assert (r["map_end"], r["recomb_start"], r["recomb_end"]) == (7, 8, 11)
    assert r["skip_cycles_map"] is None and r["skip_cycles_recomb"] == [2]  # cycle 9 = 2nd recomb position


def test_cycle_positions_recomb_map_order_with_skip(bw):
    r = _positions(bw, sequencing_order="recomb_map", barcode_length_map=7, barcode_length_recomb=5, skip_cycles=[2, 8])
    assert (r["recomb_start"], r["recomb_end"], r["map_start"], r["map_end"]) == (1, 4, 5, 10)
    assert r["skip_cycles_recomb"] == [2] and r["skip_cycles_map"] == [3]  # cycle 8 = 3rd map position


def test_cycle_positions_simple_mode_keeps_skip_list(bw):
    r = _positions(bw, skip_cycles=[6])
    assert r == {"skip_cycles": [6], "skip_cycles_map": [6]}
    assert _positions(bw) is None
