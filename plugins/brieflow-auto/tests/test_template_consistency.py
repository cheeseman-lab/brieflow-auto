"""Consistency locks for the interview template — the maintained source of truth.

The spec→template generators were bootstrap-only and are not committed;
data/interview_template.json is now THE source of truth. These tests lock its
integrity so the surfaces that must agree (template ↔ RESOLVER_MAP ↔
probe_preprocess ↔ the spec CSV index) can't silently drift — catching exactly
the case-mismatch / orphan-resolver / spec-drift classes a generator would have
prevented.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]          # plugins/brieflow-auto
SCRIPTS = ROOT / "scripts"
TEMPLATE = ROOT / "data" / "interview_template.json"
SPEC = Path(__file__).resolve().parents[3] / "docs" / "superpowers" / "plans" / "2026-06-07-interview-spec.csv"


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPTS / "brieflow_wizard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def params():
    return json.loads(TEMPLATE.read_text())["params"]


def _names(params):
    return {(p["notebook"], p["param_name"]) for p in params}


def test_validators_pass(bw, params):
    assert bw.validate_resolver_coverage(params, bw)["ok"]
    assert bw.validate_interview_buckets(params, compute_keys=bw._COMPUTED_DERIVATIONS.keys())["ok"]


def test_resolver_map_keys_are_real_template_params(bw, params):
    """Every RESOLVER_MAP key must name a real template param (exact case) — a
    leftover lowercase key would silently never match (find_param is case-sensitive)."""
    param_names = {p["param_name"] for p in params}
    orphan_keys = [k for k in bw.RESOLVER_MAP if k not in param_names]
    assert orphan_keys == [], f"RESOLVER_MAP keys with no template param: {orphan_keys}"


def test_probe_prefill_keys_are_real_template_params(bw, params, tmp_path):
    """Everything probe_preprocess prefills must be a real template param_name, so
    apply_preprocess_probe's case-sensitive set_value always lands."""
    sbs = tmp_path / "sbs"
    sbs.mkdir()
    (sbs / "plate_1_WellA1_Points-0001_c1_ChannelDAPI.tiff").write_bytes(b"")
    ph = tmp_path / "ph"
    ph.mkdir()
    (ph / "plate_1_WellA1_Points-0001_ChannelDAPI.tiff").write_bytes(b"")
    screen = {"data": {"input_sbs_dir": str(sbs), "input_phenotype_dir": str(ph)},
              "iss": {"channels": []}, "phenotype": {"channels": []}}
    rep = bw.probe_preprocess(tmp_path, screen)
    param_names = {p["param_name"] for p in params if p["notebook"] == "preprocess"}
    bad = [k for k in (list(rep["prefilled"]) + list(rep["operator_needed"])) if k not in param_names]
    assert bad == [], f"probe emits keys absent from the preprocess template: {bad}"


def test_template_param_set_matches_spec_csv(params):
    """The spec CSV stays a faithful index of the template (same (module, var)
    set) even though parameter VALUES are now maintained in the template."""
    if not SPEC.exists():
        pytest.skip(f"spec CSV not found at {SPEC}")
    spec_rows = {(r["module"], r["notebook_var"]) for r in csv.DictReader(open(SPEC))}
    tmpl_rows = _names(params)
    assert spec_rows == tmpl_rows, (
        f"spec↔template drift — only in spec: {sorted(spec_rows - tmpl_rows)}; "
        f"only in template: {sorted(tmpl_rows - spec_rows)}"
    )
