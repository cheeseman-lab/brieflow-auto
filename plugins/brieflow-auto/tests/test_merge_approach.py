"""MERGE_APPROACH is derived: measured phenotype cells per tile, else the phenotype
objective from screen.yaml, else "fast"; an operator value always wins."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "interview_template.json"


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPTS / "brieflow_wizard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ap(bw):
    spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPTS / "brieflow_runner.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


def _screen(objective=None, binning=None):
    return {"phenotype": {"objective": objective, "binning": binning}}


def test_objective_magnification_parses_common_forms(bw):
    assert bw.objective_magnification("40X 0.95 NA") == 40
    assert bw.objective_magnification("20x") == 20
    assert bw.objective_magnification(60) == 60
    assert bw.objective_magnification(None) is None
    assert bw.objective_magnification("Plan Apo") is None


def test_policy_reads_lab_defaults(bw):
    assert bw.merge_approach_policy() == {"max_cells_per_tile": 60.0, "min_objective_x": 40.0}


def test_40x_objective_is_positions(bw):
    d = bw.derive_merge_approach(_screen("40X 0.95 NA", 2))
    assert d["approach"] == "positions" and d["value_source"] == "screen-yaml"
    assert "40x" in d["reason"] and "binning 2" in d["reason"]


def test_20x_objective_is_fast(bw):
    d = bw.derive_merge_approach(_screen("20X 0.75 NA"))
    assert d["approach"] == "fast" and d["value_source"] == "screen-yaml"


def test_sparse_measured_density_wins_over_20x(bw):
    d = bw.derive_merge_approach(_screen("20X 0.75 NA"), median_cells_per_tile=25)
    assert d["approach"] == "positions" and d["value_source"] == "auto-probe"
    assert "25" in d["reason"]


def test_dense_measured_density_is_fast_even_at_40x(bw):
    d = bw.derive_merge_approach(_screen("40X"), median_cells_per_tile=130)
    assert d["approach"] == "fast" and d["value_source"] == "auto-probe"


def test_no_objective_no_data_is_fast_with_note(bw):
    d = bw.derive_merge_approach(_screen())
    assert d["approach"] == "fast" and d["value_source"] == "default"
    assert "no phenotype.objective" in d["reason"]


def test_operator_value_is_never_replaced(bw, tmp_path):
    dst = tmp_path / ".brieflow" / "interview.json"
    bw.init_screen_interview(TEMPLATE, dst, "s")
    iv = bw.load_interview(dst)
    bw.set_value(iv, "MERGE_APPROACH", "fast", "operator", notebook="merge")
    d = bw.apply_merge_approach(iv, _screen("60X"), median_cells_per_tile=10)
    row = bw.find_param(iv, "MERGE_APPROACH", notebook="merge")
    assert d["value_source"] == "operator" and row["value"] == "fast"
    assert row["value_source"] == "operator"


def _write_info(root: Path, well: str, cells_per_tile: dict[int, int]):
    import pandas as pd

    d = root / "brieflow_output" / "phenotype" / "parquets"
    d.mkdir(parents=True, exist_ok=True)
    tiles = [t for t, n in cells_per_tile.items() for _ in range(n)]
    pd.DataFrame({"tile": tiles, "cell": range(len(tiles))}).to_parquet(
        d / f"P-1_W-{well}__phenotype_info.parquet"
    )


def test_probe_median_cells_per_tile(bw, tmp_path):
    assert bw.probe_phenotype_cells_per_tile(tmp_path)["median_cells_per_tile"] is None
    _write_info(tmp_path, "A1", {0: 20, 1: 25, 2: 30})
    _write_info(tmp_path, "A2", {0: 24, 1: 26})
    out = bw.probe_phenotype_cells_per_tile(tmp_path)
    assert out["median_cells_per_tile"] == 25 and out["n_tiles"] == 5 and out["n_wells"] == 2


def _analysis(tmp_path, objective):
    (tmp_path / "screen.yaml").write_text(
        json.dumps({"phenotype": {"objective": objective}})
    )
    return tmp_path


def test_launch_resolves_positions_from_objective_and_gates_merge_rows(bw, tmp_path):
    a = _analysis(tmp_path, "40X 0.95 NA")
    bw.resolve_all_phases(a)
    iv = bw.load_interview(a / ".brieflow" / "interview.json")
    row = bw.find_param(iv, "MERGE_APPROACH", notebook="merge")
    assert row["value"] == "positions" and row["value_source"] == "screen-yaml" and row["reason"]
    assert bw.find_param(iv, "FLIPUD", notebook="merge")["value"] is False  # lab-filled
    assert not bw.is_active(bw.find_param(iv, "INITIAL_SITES_APPROACH", notebook="merge"), iv)
    review = bw.interview_review(iv)
    [dec] = [
        d for d in review["attention"]["derived_decisions"] if d["param"] == "merge.MERGE_APPROACH"
    ]
    assert dec["value"] == "positions" and "40x" in dec["reason"]
    assert "merge.MERGE_APPROACH" in bw.render_interview_review_html(review)


def test_launch_without_objective_is_fast_with_note(bw, tmp_path):
    a = _analysis(tmp_path, None)
    r = bw.resolve_all_phases(a)
    iv = bw.load_interview(a / ".brieflow" / "interview.json")
    row = bw.find_param(iv, "MERGE_APPROACH", notebook="merge")
    assert row["value"] == "fast" and row["value_source"] == "default"
    assert "no phenotype.objective" in row["reason"]
    assert "MERGE_APPROACH" not in {g["param_name"] for g in r["gaps"]}


def test_merge_phase_flips_to_positions_on_sparse_tiles(ap, bw, tmp_path, monkeypatch):
    a = _analysis(tmp_path, "20X 0.75 NA")
    bw.resolve_all_phases(a)
    iv_path = a / ".brieflow" / "interview.json"
    iv = bw.load_interview(iv_path)
    assert bw.find_param(iv, "MERGE_APPROACH", notebook="merge")["value"] == "fast"
    _write_info(a, "A1", {t: 25 for t in range(9)})
    journal = []
    monkeypatch.setattr(ap, "record_decision", lambda *x, **k: journal.append(k))
    d = ap.resolve_merge_approach(a, ap.load_lab_defaults(), iv, iv_path)
    assert d["changed"] and d["approach"] == "positions" and d["value_source"] == "auto-probe"
    assert bw.find_param(iv, "FLIPUD", notebook="merge")["value"] is False  # newly active, filled
    assert journal and journal[0]["kind"] == "merge-approach"
    assert journal[0]["choice"] == "MERGE_APPROACH=positions"
    saved = bw.load_interview(iv_path)
    assert bw.find_param(saved, "MERGE_APPROACH", notebook="merge")["value"] == "positions"
    assert ap._merge_positions_on(saved)


def test_merge_phase_keeps_fast_on_dense_tiles(ap, bw, tmp_path, monkeypatch):
    a = _analysis(tmp_path, "40X")
    bw.resolve_all_phases(a)
    iv_path = a / ".brieflow" / "interview.json"
    iv = bw.load_interview(iv_path)
    _write_info(a, "A1", {t: 130 for t in range(9)})
    monkeypatch.setattr(ap, "record_decision", lambda *x, **k: None)
    d = ap.resolve_merge_approach(a, ap.load_lab_defaults(), iv, iv_path)
    assert d["approach"] == "fast" and d["changed"]
    assert bw.find_param(iv, "INITIAL_SITES_APPROACH", notebook="merge")["value"] == "auto"
