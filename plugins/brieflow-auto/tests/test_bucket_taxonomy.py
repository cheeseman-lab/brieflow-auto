"""Tests for brieflow_buckets.py — the canonical resolution taxonomy.

The validator runs against the real interview_template.json so the bucket
vocabulary and derivation-clause shapes can't drift silently. Also locks the
facade contract: every public symbol is importable from brieflow_wizard.
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
def buckets():
    return _load("brieflow_buckets")


@pytest.fixture(scope="module")
def bw():
    return _load("brieflow_wizard")


@pytest.fixture(scope="module")
def template_rows():
    return json.loads(TEMPLATE.read_text())["params"]


# ---------------------------------------------------------------------------
# Real template passes the validator
# ---------------------------------------------------------------------------
def test_template_buckets_all_canonical(buckets, template_rows):
    used = {r["bucket"] for r in template_rows}
    extra = used - set(buckets.ALL_BUCKETS)
    assert not extra, f"template uses non-canonical bucket(s): {extra}"


def test_template_validates_clean(buckets, bw, template_rows):
    report = buckets.validate_interview_buckets(
        template_rows, compute_keys=bw._COMPUTED_DERIVATIONS.keys()
    )
    assert report["ok"], {k: v for k, v in report.items() if k != "ok" and v}


def test_no_legacy_tuned_bucket(template_rows):
    # det_range was reclassified `tuned` -> `auto-probe`; the singular `tuned`
    # bucket must not reappear.
    assert all(r["bucket"] != "tuned" for r in template_rows)


# ---------------------------------------------------------------------------
# Bucket families are disjoint and cover ALL_BUCKETS
# ---------------------------------------------------------------------------
def test_families_partition_all_buckets(buckets):
    fams = [
        buckets.OPERATOR_BUCKETS,
        buckets.SYSTEM_DERIVED_BUCKETS,
        buckets.AGENT_RESOLVED_BUCKETS,
        buckets.ARCHIVAL_BUCKETS,
    ]
    union = set().union(*fams)
    assert union == set(buckets.ALL_BUCKETS)
    # disjoint
    seen: set[str] = set()
    for fam in fams:
        assert not (seen & set(fam)), "bucket families overlap"
        seen |= set(fam)


# ---------------------------------------------------------------------------
# Validator catches drift
# ---------------------------------------------------------------------------
def test_validator_flags_unknown_bucket(buckets):
    rows = [{"param_name": "x", "bucket": "made-up"}]
    report = buckets.validate_interview_buckets(rows)
    assert not report["ok"]
    assert "x='made-up'" in report["invalid_bucket"]


def test_validator_flags_missing_screen_yaml_path(buckets):
    rows = [{"param_name": "x", "bucket": "auto", "derivation": {"type": "screen_yaml"}}]
    report = buckets.validate_interview_buckets(rows)
    assert "x" in report["missing_path"]


def test_validator_flags_unknown_compute(buckets):
    rows = [
        {
            "param_name": "x",
            "bucket": "auto",
            "derivation": {"type": "screen_yaml_computed", "compute": "nope"},
        }
    ]
    report = buckets.validate_interview_buckets(rows, compute_keys={"aggregate_fields"})
    assert "x='nope'" in report["unknown_compute"]


def test_validator_flags_library_default_without_value(buckets):
    rows = [{"param_name": "x", "bucket": "auto", "derivation": {"type": "library_default"}}]
    report = buckets.validate_interview_buckets(rows)
    assert "x" in report["missing_value"]


# ---------------------------------------------------------------------------
# Facade contract: symbols re-exported from brieflow_wizard
# ---------------------------------------------------------------------------
def test_facade_reexports_taxonomy(bw, buckets):
    assert bw.VALID_VALUE_SOURCES == buckets.VALID_VALUE_SOURCES
    assert bw._OPERATOR_BUCKETS == set(buckets.OPERATOR_BUCKETS)
    assert bw._NO_MARKER_BUCKETS == buckets._NO_MARKER_BUCKETS
    assert bw.validate_interview_buckets is buckets.validate_interview_buckets
    assert bw.ALL_BUCKETS == buckets.ALL_BUCKETS
