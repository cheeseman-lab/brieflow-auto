"""Tests for the same-fingerprint short-circuit in runloop (HARDENING #15).

Unit-tests the helper functions directly. End-to-end behavior (the runloop
actually escalating on second-attempt match) requires a full subprocess
harness; the helpers are the load-bearing decision logic so direct testing
suffices.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runloop.py"


@pytest.fixture(scope="module")
def runloop():
    spec = importlib.util.spec_from_file_location("brieflow_runloop", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runloop"] = mod
    spec.loader.exec_module(mod)
    return mod


def _action(diagnostics):
    return {"action": "resume", "diagnostics": diagnostics}


def test_fingerprint_stable_for_identical_input(runloop):
    """Same (rule, category, evidence) → same fingerprint."""
    a = _action(
        [
            {
                "rule": "eval_merge",
                "category": "unknown",
                "evidence": "ValueError: bad merge state\n  File foo.py line 42\n",
            },
        ]
    )
    b = _action(
        [
            {
                "rule": "eval_merge",
                "category": "unknown",
                "evidence": "ValueError: bad merge state\n  File foo.py line 42\n",
            },
        ]
    )
    assert runloop._diagnostics_fingerprint(a) == runloop._diagnostics_fingerprint(b)


def test_fingerprint_differs_for_different_rule(runloop):
    a = _action(
        [
            {"rule": "eval_merge", "category": "unknown", "evidence": "ValueError"},
        ]
    )
    b = _action(
        [
            {
                "rule": "merge_phenotype",
                "category": "unknown",
                "evidence": "ValueError",
            },
        ]
    )
    assert runloop._diagnostics_fingerprint(a) != runloop._diagnostics_fingerprint(b)


def test_fingerprint_truncates_evidence(runloop):
    """Evidence beyond 120 chars shouldn't affect fingerprint — different
    MaxRSS readings on different attempts shouldn't perturb the hash."""
    # First 120 chars identical, tail differs.
    common = "x" * 120
    a = _action(
        [
            {
                "rule": "merge_phenotype",
                "category": "recoverable_oom",
                "evidence": common + " peak=130000",
            },
        ]
    )
    b = _action(
        [
            {
                "rule": "merge_phenotype",
                "category": "recoverable_oom",
                "evidence": common + " peak=140000",
            },
        ]
    )
    assert runloop._diagnostics_fingerprint(a) == runloop._diagnostics_fingerprint(b)


def test_detect_repeat(runloop):
    """Same fingerprint for same rule in prev + cur → return rule name."""
    prev = {"eval_merge": "abc123def4567890"}
    cur = {"eval_merge": "abc123def4567890"}
    assert runloop._detect_same_fingerprint(prev, cur) == "eval_merge"


def test_detect_no_repeat_when_fingerprint_changed(runloop):
    """Same rule but different fingerprint → no repeat."""
    prev = {"eval_merge": "abc123def4567890"}
    cur = {"eval_merge": "11112222ffff0000"}
    assert runloop._detect_same_fingerprint(prev, cur) is None


def test_detect_no_repeat_when_different_rule(runloop):
    """Different rule names → no repeat."""
    prev = {"eval_merge": "abc123def4567890"}
    cur = {"merge_phenotype": "abc123def4567890"}
    assert runloop._detect_same_fingerprint(prev, cur) is None


def test_detect_handles_empty_prev(runloop):
    """First attempt — no prior fingerprints → no repeat."""
    prev: dict[str, str] = {}
    cur = {"eval_merge": "abc123"}
    assert runloop._detect_same_fingerprint(prev, cur) is None


def test_detect_handles_empty_cur(runloop):
    """Recover returned no diagnostics → no repeat."""
    prev = {"eval_merge": "abc123"}
    cur: dict[str, str] = {}
    assert runloop._detect_same_fingerprint(prev, cur) is None
