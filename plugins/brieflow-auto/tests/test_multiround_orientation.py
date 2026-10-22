"""Tests for multi-round phenotype channel-flip orientation.

The probe reads ONE round's ND2 channels but screen.yaml declares the full
multi-round stack (DAPI listed per round → DAPI, DAPI_2). _detect_channel_flip
must orient against the per-round slice. Regression for jannu phenotype being
converted with flip=False because the probe couldn't orient 4-vs-7.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_probe.py"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("brieflow_probe", PROBE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_probe"] = m
    spec.loader.exec_module(m)
    return m


# jannu phenotype: each round's ND2 has 4 channels, DAPI last (reversed vs the
# DAPI-first declared order). 8 declared (DAPI + DAPI_2).
_EMBEDDED_ROUND = ["AF750", "Cy3", "GFP", "DAPI"]
_DECLARED_8 = ["DAPI", "VIMENTIN", "COXIV", "TUBULIN",
               "DAPI_2", "PHALLOIDIN", "ACA", "WGA"]


def test_multiround_detects_flip_true(probe):
    """4 embedded (DAPI last) vs 8 declared (DAPI first, per round) ⇒ flip."""
    flip, why = probe._detect_channel_flip(_EMBEDDED_ROUND, _DECLARED_8)
    assert flip is True, why
    assert "per-round" in why


def test_multiround_no_flip_when_already_dapi_first(probe):
    """If the embedded round is already DAPI-first, no flip."""
    flip, _ = probe._detect_channel_flip(
        ["DAPI", "GFP", "Cy3", "AF750"], _DECLARED_8
    )
    assert flip is False


def test_seven_declared_stays_undecidable(probe):
    """4 doesn't divide 7 (no per-round DAPI duplicate) → can't orient → operator."""
    declared_7 = ["DAPI", "VIMENTIN", "COXIV", "TUBULIN", "PHALLOIDIN", "ACA", "WGA"]
    flip, why = probe._detect_channel_flip(_EMBEDDED_ROUND, declared_7)
    assert flip is None
    assert "mismatch" in why


def test_singleround_matched_counts_unchanged(probe):
    """Equal counts still use the plain DAPI-anchored path (SBS / single-round)."""
    # SBS: container DAPI last vs declared DAPI first ⇒ flip
    flip, _ = probe._detect_channel_flip(
        ["Cy7", "Cy5", "AF594", "Cy3_SBS", "GFP", "DAPI_SBS"],
        ["DAPI", "VIMENTIN", "G", "T", "A", "C"],
    )
    assert flip is True


def test_non_divisible_mismatch_undecidable(probe):
    flip, _ = probe._detect_channel_flip(["A", "B", "DAPI"], ["DAPI", "B", "C", "D", "E"])
    assert flip is None
