"""Tests for scan_additional_notes — surfacing candidate lab-default overrides
from the operator's free-text screen.yaml additional_notes."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPTS / "brieflow_wizard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


def _params(cands):
    return {c["param"] for c in cands}


def test_empty_or_missing_notes(bw):
    assert bw.scan_additional_notes({}) == []
    assert bw.scan_additional_notes({"additional_notes": ""}) == []
    assert bw.scan_additional_notes({"additional_notes": "   \n  "}) == []


def test_skip_cycle_signal(bw):
    n = {"additional_notes": "The first sequencing cycle looked off so we left it out."}
    assert "SKIP_CYCLES" in _params(bw.scan_additional_notes(n))


def test_segmentation_difficulty_signal(bw):
    n = {"additional_notes": "These cells aren't round — irregular and hard to segment."}
    assert "SEGMENTATION_METHOD" in _params(bw.scan_additional_notes(n))


def test_channel_exclusion_signal(bw):
    n = {"additional_notes": "We tried phalloidin but the signal faded so we're ignoring it."}
    assert "REMOVE_CHANNEL" in _params(bw.scan_additional_notes(n))


def test_foci_signal(bw):
    n = {"additional_notes": "GlycoRNA shows up as distinct puncta we want to quantify."}
    assert "FOCI_CHANNEL" in _params(bw.scan_additional_notes(n))


def test_line_wrapped_phrase_still_matches(bw):
    # block-scalar wrap mid-phrase ("bright\nspots") must still be caught
    n = {"additional_notes": "Centromeres show up as bright\nspots and are the readout."}
    assert "FOCI_CHANNEL" in _params(bw.scan_additional_notes(n))


def test_benign_note_no_false_positive(bw):
    n = {"additional_notes": "Standard run, three plates, imaged at 78 hours."}
    assert bw.scan_additional_notes(n) == []


def test_candidate_shape(bw):
    cands = bw.scan_additional_notes({"additional_notes": "Dropped the first cycle — it was bad."})
    assert cands and set(cands[0]) == {"param", "suggestion", "excerpt"}


# --- verify_notes_against_data: reconcile prose vs the data census -----------


def _kinds(contras):
    return {c["kind"] for c in contras}


def _clean_census(**over):
    c = {
        "plates": [1],
        "cycles_per_plate": {1: 11},
        "rounds_per_plate": {1: 1},
        "max_cycles": 11,
        "uniform_cycles": True,
        "declared_cycles": 11,
        "surplus_cycles": {},
        "junk_dirs": [],
    }
    c.update(over)
    return c


def test_verify_damavand_reassuring_vs_anomaly(bw):
    """The motivating case: a 'nothing unusual' note over a tree with surplus +
    non-uniform cycles + a 'Dont use' junk dir."""
    screen = {
        "additional_notes": "Standard sequencing run, nothing unusual — "
        "it went cleanly start to finish.",
    }
    census = _clean_census(
        plates=[1, 2],
        cycles_per_plate={1: 13, 2: 12},
        max_cycles=13,
        uniform_cycles=False,
        surplus_cycles={1: 2, 2: 1},
        junk_dirs=["input_sbs/plate_2/Dont use"],
    )
    contras = bw.verify_notes_against_data(screen, census)
    kinds = _kinds(contras)
    assert "reassuring-claim-vs-anomaly" in kinds
    assert "junk-dirs-present" in kinds
    assert "non-uniform-cycles" in kinds
    # the reassuring contradiction is the serious one
    rc = next(c for c in contras if c["kind"] == "reassuring-claim-vs-anomaly")
    assert rc["severity"] == "action_needed"
    assert "Dont use" in rc["message"]
    assert rc["excerpt"]  # a snippet of the triggering phrase
    # shape
    for c in contras:
        assert set(c) == {"kind", "severity", "message", "excerpt"}


def test_verify_clean_note_clean_census(bw):
    screen = {"additional_notes": "Standard run, nothing unusual, went cleanly."}
    assert bw.verify_notes_against_data(screen, _clean_census()) == []


def test_verify_cycle_count_mismatch(bw):
    """Note says 11 cycles; data has 13 (and no declared count to compare)."""
    screen = {"additional_notes": "We collected 11 cycles for this screen."}
    census = _clean_census(
        cycles_per_plate={1: 13}, max_cycles=13, declared_cycles=None
    )
    contras = bw.verify_notes_against_data(screen, census)
    assert "cycle-count-mismatch" in _kinds(contras)
    cm = next(c for c in contras if c["kind"] == "cycle-count-mismatch")
    assert cm["severity"] == "warning"
    assert "11" in cm["message"] and "13" in cm["message"]


def test_verify_cycle_count_matches_no_contradiction(bw):
    screen = {"additional_notes": "We collected 11 cycles for this screen."}
    assert bw.verify_notes_against_data(screen, _clean_census()) == []


def test_verify_none_or_empty_note(bw):
    assert bw.verify_notes_against_data({}, _clean_census()) == []
    assert bw.verify_notes_against_data({"additional_notes": None}, _clean_census()) == []
    assert bw.verify_notes_against_data({"additional_notes": "  \n "}, _clean_census()) == []
    assert bw.verify_notes_against_data({"additional_notes": 123}, _clean_census()) == []


def test_verify_str_keys_json_roundtrip(bw):
    """A census round-tripped through json has str dict keys — must still work."""
    screen = {"additional_notes": "Nothing unusual here, a totally standard run."}
    census = _clean_census(
        plates=[1, 2],
        cycles_per_plate={"1": 13, "2": 12},
        max_cycles=13,
        uniform_cycles=False,
        surplus_cycles={"1": 2, "2": 1},
        junk_dirs=["input_sbs/plate_2/Dont use"],
    )
    contras = bw.verify_notes_against_data(screen, census)
    kinds = _kinds(contras)
    assert "reassuring-claim-vs-anomaly" in kinds
    assert "non-uniform-cycles" in kinds
    nu = next(c for c in contras if c["kind"] == "non-uniform-cycles")
    # int-coerced keys render as plate_1 / plate_2
    assert "plate_1" in nu["message"] and "plate_2" in nu["message"]


def test_verify_dedup_kinds(bw):
    """No kind appears twice even when multiple rules touch the same anomaly."""
    screen = {"additional_notes": "Standard run, nothing unusual."}
    census = _clean_census(
        plates=[1, 2],
        cycles_per_plate={1: 12, 2: 11},
        max_cycles=12,
        uniform_cycles=False,
        junk_dirs=["input_sbs/plate_1/Dont use", "input_sbs/plate_1/scratch"],
    )
    contras = bw.verify_notes_against_data(screen, census)
    kinds = [c["kind"] for c in contras]
    assert len(kinds) == len(set(kinds))


def test_verify_missing_census_keys(bw):
    """Defensive: a sparse census dict must not raise."""
    screen = {"additional_notes": "Nothing unusual."}
    assert bw.verify_notes_against_data(screen, {}) == []
    assert bw.verify_notes_against_data(screen, {"junk_dirs": ["x/Dont use"]})
