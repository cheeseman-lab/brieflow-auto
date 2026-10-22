"""Tests for brieflow_resolvers.py — the resolution registry.

The coverage validator runs against the real interview_template.json so no
auto-probe / agent-resolved param is an orphan promise (flagged but with
nothing behind it) and no named resolver is dead (points to a missing helper).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "interview_template.json"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def resolvers():
    return _load("brieflow_resolvers")


@pytest.fixture(scope="module")
def bw():
    return _load("brieflow_wizard")


@pytest.fixture(scope="module")
def template_rows():
    return json.loads(TEMPLATE.read_text())["params"]


# ---------------------------------------------------------------------------
# Real template: full coverage, no orphans, no dead resolvers
# ---------------------------------------------------------------------------
def test_template_resolver_coverage_clean(resolvers, bw, template_rows):
    report = resolvers.validate_resolver_coverage(template_rows, bw)
    assert report["ok"], {
        "orphans": report["orphans"],
        "dead_resolvers": report["dead_resolvers"],
    }


def test_every_named_resolver_is_live(resolvers, bw):
    # Each non-sentinel resolver name must resolve to a callable on the facade.
    sentinels = {resolvers.DERIVATION, resolvers.AGENT_INTERVIEW}
    for param, resolver in resolvers.RESOLVER_MAP.items():
        if resolver in sentinels:
            continue
        fn = getattr(bw, resolver, None)
        assert callable(fn), f"{param} -> {resolver} is not a live callable"


def test_no_known_unimplemented_gaps(resolvers):
    # DET_RANGE is now built (probe_det_range), so there are no
    # tracked-unimplemented probes. Any param in
    # KNOWN_UNIMPLEMENTED_PROBES must never also be in RESOLVER_MAP (a gap is not
    # a promise) — vacuously true while the gap set is empty.
    assert resolvers.KNOWN_UNIMPLEMENTED_PROBES == {}
    assert not (set(resolvers.KNOWN_UNIMPLEMENTED_PROBES) & set(resolvers.RESOLVER_MAP))
    assert resolvers.RESOLVER_MAP.get("DET_RANGE") == "probe_det_range"


# ---------------------------------------------------------------------------
# Validator catches drift
# ---------------------------------------------------------------------------
def test_validator_flags_orphan(resolvers, bw):
    rows = [{"param_name": "brand_new_param", "bucket": "auto-probe"}]
    report = resolvers.validate_resolver_coverage(rows, bw)
    assert not report["ok"]
    assert "brand_new_param" in report["orphans"]


def test_validator_flags_dead_resolver(resolvers):
    class NS:  # namespace with no helpers
        pass

    rows = [{"param_name": "SBS_PATH_PATTERN", "bucket": "auto-probe"}]
    report = resolvers.validate_resolver_coverage(rows, NS())
    assert "SBS_PATH_PATTERN->infer_path_pattern" in report["dead_resolvers"]


def test_validator_ignores_operator_rows(resolvers, bw):
    rows = [{"param_name": "whatever", "bucket": "user"}]
    report = resolvers.validate_resolver_coverage(rows, bw)
    assert report["ok"]


# ---------------------------------------------------------------------------
# Facade re-export
# ---------------------------------------------------------------------------
def test_facade_reexports_registry(bw, resolvers):
    assert bw.RESOLVER_MAP == resolvers.RESOLVER_MAP
    assert bw.validate_resolver_coverage is resolvers.validate_resolver_coverage
    assert bw.KNOWN_UNIMPLEMENTED_PROBES == resolvers.KNOWN_UNIMPLEMENTED_PROBES
