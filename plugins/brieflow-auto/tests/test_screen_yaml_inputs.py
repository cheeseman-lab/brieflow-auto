"""Tests for validate_screen_yaml_inputs in brieflow_preflight.py.

Gates the operator-supplied screen.yaml inputs the pipeline can't derive:
the barcode/design TSV (library.raw_design_table_fp) and the operator-locked
raw-data dirs (data.input_sbs_dir / input_phenotype_dir).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_preflight.py"


@pytest.fixture(scope="module")
def preflight():
    spec = importlib.util.spec_from_file_location("brieflow_preflight", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_preflight"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_screen_yaml(
    analysis_dir: Path, *, design_fp, sbs_dir, ph_dir, make_design=True
):
    analysis_dir.mkdir(parents=True, exist_ok=True)
    lib = "" if design_fp is None else f'  raw_design_table_fp: "{design_fp}"\n'
    sbs = "null" if sbs_dir is None else sbs_dir
    ph = "null" if ph_dir is None else ph_dir
    (analysis_dir / "screen.yaml").write_text(
        "library:\n"
        + lib
        + f"data:\n  input_sbs_dir: {sbs}\n  input_phenotype_dir: {ph}\n"
    )
    if design_fp and make_design:
        fp = analysis_dir / design_fp
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("sgRNA\tgene\n")


def _codes(issues):
    return {i.code for i in issues}


def test_all_present_no_issues(preflight, tmp_path):
    a = tmp_path / "screen-analysis"
    _write_screen_yaml(
        a, design_fp="config/pool.tsv", sbs_dir="/data/s/sbs", ph_dir="/data/s/ph"
    )
    assert preflight.validate_screen_yaml_inputs(a) == []


def test_missing_screen_yaml_is_noop(preflight, tmp_path):
    a = tmp_path / "no-yaml"
    a.mkdir()
    assert preflight.validate_screen_yaml_inputs(a) == []


def test_blank_barcode_is_high(preflight, tmp_path):
    a = tmp_path / "s"
    _write_screen_yaml(a, design_fp=None, sbs_dir="/data/s/sbs", ph_dir="/data/s/ph")
    issues = preflight.validate_screen_yaml_inputs(a)
    assert "barcode-table-missing" in _codes(issues)
    assert all(i.severity == "HIGH" for i in issues)


def test_barcode_path_set_but_file_absent_is_high(preflight, tmp_path):
    a = tmp_path / "s"
    _write_screen_yaml(
        a,
        design_fp="config/pool.tsv",
        sbs_dir="/data/s/sbs",
        ph_dir="/data/s/ph",
        make_design=False,
    )
    issues = preflight.validate_screen_yaml_inputs(a)
    assert "barcode-table-missing" in _codes(issues)


def test_null_input_dirs_are_high(preflight, tmp_path):
    a = tmp_path / "s"
    _write_screen_yaml(a, design_fp="config/pool.tsv", sbs_dir=None, ph_dir=None)
    issues = preflight.validate_screen_yaml_inputs(a)
    assert "input-dir-missing" in _codes(issues)
    # one per missing dir
    assert sum(i.code == "input-dir-missing" for i in issues) == 2


def test_parse_error_is_high(preflight, tmp_path):
    a = tmp_path / "s"
    a.mkdir()
    (a / "screen.yaml").write_text("library: [unbalanced\n")
    issues = preflight.validate_screen_yaml_inputs(a)
    assert _codes(issues) == {"screen-yaml-parse"}


def test_well_annotations_named_but_absent_is_high(preflight, tmp_path):
    _write_screen_yaml(tmp_path, design_fp="config/lib.tsv", sbs_dir="/d/s", ph_dir="/d/p")
    with open(tmp_path / "screen.yaml", "a") as fh:
        fh.write("cellular:\n  well_annotations: well_annotations.tsv\n  split_by: experiment\n")
    assert "well-annotations-missing" in _codes(preflight.validate_screen_yaml_inputs(tmp_path))
    (tmp_path / "well_annotations.tsv").write_text("plate\twell\texperiment\n")
    assert preflight.validate_screen_yaml_inputs(tmp_path) == []


def test_split_by_without_table_is_high(preflight, tmp_path):
    _write_screen_yaml(tmp_path, design_fp="config/lib.tsv", sbs_dir="/d/s", ph_dir="/d/p")
    with open(tmp_path / "screen.yaml", "a") as fh:
        fh.write("cellular:\n  split_by: experiment\n")
    codes = _codes(preflight.validate_screen_yaml_inputs(tmp_path))
    assert codes == {"split-by-without-annotations"}


def test_well_annotations_header_checked(preflight, tmp_path):
    _write_screen_yaml(tmp_path, design_fp="config/lib.tsv", sbs_dir="/d/s", ph_dir="/d/p")
    with open(tmp_path / "screen.yaml", "a") as fh:
        fh.write("cellular:\n  well_annotations: wells.csv\n  split_by: experiment\n")
    (tmp_path / "wells.csv").write_text("plate,well,treatment\n1,A1,x\n")
    issues = preflight.validate_screen_yaml_inputs(tmp_path)
    assert _codes(issues) == {"well-annotations-columns"} and "experiment" in issues[0].message
    (tmp_path / "wells.csv").write_text("plate,well,experiment\n1,A1,x\n")
    assert preflight.validate_screen_yaml_inputs(tmp_path) == []
