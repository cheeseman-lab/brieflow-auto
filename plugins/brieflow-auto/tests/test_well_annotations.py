"""Tests for per-well annotations: the screen.yaml table copied to config/ with only the wells to
analyze, the split column derivation, and the phase-time hooks in resolve_phase."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from brieflow_well_annotations import (  # noqa: E402
    build_well_annotations,
    complete_min_cell_cutoffs,
    set_annotated_test_wells,
)

TABLE = (
    "plate\twell\texperiment\tseeding_density\n"
    "1\tr02c02\texp01\t5000\n"
    "1\tr02c03\texp01\t5000\n"
    "1\tr03c02\t\t8000\n"
    "1\tr03c03\texp02\t8000\n"
)


def _screen(
    tmp_path,
    cellular="  well_annotations: well_annotations.tsv\n  split_by: experiment\n",
    merged=("r02c02", "r02c03"),
    table=TABLE,
    name="well_annotations.tsv",
    root="brieflow_output",
):
    (tmp_path / "screen.yaml").write_text("cellular:\n" + cellular)
    (tmp_path / name).write_text(table)
    parquets = tmp_path / root / "merge/parquets"
    parquets.mkdir(parents=True, exist_ok=True)
    for w in merged:
        plate, well = w if isinstance(w, tuple) else (1, w)
        (parquets / f"P-{plate}_W-{well}__merge_final.parquet").write_text("")
    return tmp_path


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPTS / "brieflow_wizard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def resolvers():
    spec = importlib.util.spec_from_file_location(
        "brieflow_resolvers", SCRIPTS / "brieflow_resolvers.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_resolvers"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_keeps_grouped_merged_wells_only(tmp_path):
    rep = build_well_annotations(_screen(tmp_path))
    assert rep["path"] == "config/well_annotations.tsv"
    assert rep["kept"] == 2 and rep["blank_group"] == 1 and rep["not_merged"] == [(1, "r03c03")]
    assert rep["wells"] == [(1, "r02c02"), (1, "r02c03")]
    assert rep["groups"] == ["exp01"]
    rows = (tmp_path / "config/well_annotations.tsv").read_text().splitlines()
    assert rows[0].split("\t") == ["plate", "well", "experiment", "seeding_density"]
    assert [r.split("\t")[1] for r in rows[1:]] == ["r02c02", "r02c03"]


def test_no_table_named_returns_none(tmp_path):
    assert build_well_annotations(_screen(tmp_path, cellular="  growth_conditions: null\n")) is None


def test_missing_split_column_raises(tmp_path):
    with pytest.raises(ValueError, match="treatment"):
        build_well_annotations(
            _screen(
                tmp_path,
                cellular="  well_annotations: well_annotations.tsv\n  split_by: treatment\n",
            )
        )


def test_cutoffs_filled_from_all():
    done, filled = complete_min_cell_cutoffs({"all": 5}, ["all", "exp01", "exp02"])
    assert done == {"all": 5, "exp01": 5, "exp02": 5} and filled == ["exp01", "exp02"]


def test_cutoffs_without_all_left_alone():
    assert complete_min_cell_cutoffs({"exp01": 3}, ["exp01", "exp02"]) == ({"exp01": 3}, [])


def test_split_col_from_screen_yaml(bw):
    screen = {"cellular": {"well_annotations": "w.tsv", "split_by": "experiment"}}
    assert bw._compute_aggregate_fields(screen)["split_col"] == "experiment"


def test_split_col_needs_a_table(bw):
    """Without a table screen.yaml names no split, so the lab default fills SPLIT_COL."""
    assert "split_col" not in bw._compute_aggregate_fields({"cellular": {"split_by": "experiment"}})
    assert "split_col" not in bw._compute_aggregate_fields({})


def _ctx(tmp_path, phase, params):
    nb = tmp_path / f"{phase}.py"
    nb.write_text("x = 1\n")
    notes: list = []
    patched: list = []
    return {
        "analysis_dir": tmp_path,
        "conda_env": "env",
        "lab": {"sweep_bands": {}},
        "iv": {"params": params},
        "iv_path": tmp_path / ".brieflow/interview.json",
        "nb_path": nb,
        "tag": "t",
        "notify": lambda *a, **k: notes.append((a, k)),
        "is_active": lambda row, iv: True,
        "patch_marker_block_value": lambda *a: patched.append(a[1:]),
        "_patched": patched,
        "list_wells_from_metadata": lambda *a, **k: ["r02c02"],
        "save_interview": lambda *a: None,
        "resolve_cluster_final": lambda *a: [5],
        "_notes": notes,
        "lab_sweep_spec": lambda *a: None,
        "lab_sweep_band": lambda *a: [],
    }


def test_aggregate_phase_writes_annotations(resolvers, tmp_path):
    _screen(tmp_path)
    row = {"param_name": "WELL_ANNOTATIONS_FP", "notebook": "aggregate", "value": None}
    ctx = _ctx(
        tmp_path,
        "aggregate",
        [
            row,
            {"param_name": "TEST_WELL_1", "notebook": "aggregate", "value": "r02c02"},
            {"param_name": "TEST_WELL_2", "notebook": "aggregate", "value": "r02c02"},
        ],
    )
    resolvers.resolve_phase("aggregate", ctx)
    assert row["value"] == "config/well_annotations.tsv" and row["value_source"] == "screen-yaml"
    assert (tmp_path / "config/well_annotations.tsv").exists()


def test_aggregate_phase_without_table_leaves_default(resolvers, tmp_path):
    _screen(tmp_path, cellular="  growth_conditions: null\n")
    row = {"param_name": "WELL_ANNOTATIONS_FP", "notebook": "aggregate", "value": None}
    resolvers.resolve_phase("aggregate", _ctx(tmp_path, "aggregate", [row]))
    assert row["value"] is None


def test_cluster_phase_completes_cutoffs(resolvers, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/aggregate_combo.tsv").write_text(
        "cell_class\tchannel_combo\tplate\twell\nall\tA\t1\tr02c02\nexp01\tA\t1\tr02c02\n"
    )
    row = {"param_name": "MIN_CELL_CUTOFFS", "notebook": "cluster", "value": {"all": 5}}
    resolvers.resolve_phase("cluster", _ctx(tmp_path, "cluster", [row]))
    assert row["value"] == {"all": 5, "exp01": 5}


def _row(name, value=None, source=None, notebook="aggregate"):
    return {"param_name": name, "notebook": notebook, "value": value, "value_source": source}


def test_trims_group_values(tmp_path):
    table = "plate\twell\texperiment\n1\tr02c02\texp01 \n1\tr02c03\t exp01\n"
    rep = build_well_annotations(_screen(tmp_path, table=table))
    assert rep["groups"] == ["exp01"]


def test_csv_table(tmp_path):
    table = "plate,well,experiment\n1,r02c02,exp01\n1,r02c03,exp02\n"
    cellular = "  well_annotations: well_annotations.csv\n  split_by: experiment\n"
    rep = build_well_annotations(
        _screen(tmp_path, cellular=cellular, table=table, name="well_annotations.csv")
    )
    assert rep["kept"] == 2 and rep["groups"] == ["exp01", "exp02"]
    assert (tmp_path / "config/well_annotations.tsv").read_text().startswith("plate\twell\t")


def test_multi_plate_table_keys_merge_output_on_plate(tmp_path):
    """The same well name on two plates is two wells; not_merged names the plate."""
    table = "plate\twell\texperiment\n1\tA1\texp01\n2\tA1\texp02\n"
    rep = build_well_annotations(_screen(tmp_path, table=table, merged=[(2, "A1")]))
    assert rep["wells"] == [(2, "A1")] and rep["not_merged"] == [(1, "A1")]


def test_results_root_from_config(tmp_path):
    """The merge output is looked up under config.yml all.root_fp."""
    _screen(tmp_path, root="results")
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/config.yml").write_text("all:\n  root_fp: results/\n")
    assert build_well_annotations(tmp_path)["kept"] == 2


def test_non_integer_plate_raises(tmp_path):
    table = "plate\twell\texperiment\nP1\tr02c02\texp01\n"
    with pytest.raises(ValueError, match="integer"):
        build_well_annotations(_screen(tmp_path, table=table))


def test_split_col_set_on_existing_interview(resolvers, tmp_path):
    """An interview created before split_by was added still gets the split."""
    _screen(tmp_path)
    split = _row("SPLIT_COL", "class", "lab-default")
    resolvers.resolve_phase(
        "aggregate", _ctx(tmp_path, "aggregate", [_row("WELL_ANNOTATIONS_FP"), split])
    )
    assert split["value"] == "experiment" and split["value_source"] == "screen-yaml"


def test_operator_rows_left_untouched(resolvers, tmp_path):
    _screen(tmp_path)
    split = _row("SPLIT_COL", "treatment", "operator")
    ann = _row("WELL_ANNOTATIONS_FP", "config/mine.tsv", "operator")
    resolvers.resolve_phase("aggregate", _ctx(tmp_path, "aggregate", [ann, split]))
    assert split["value"] == "treatment" and ann["value"] == "config/mine.tsv"
    assert not (tmp_path / "config/well_annotations.tsv").exists()


def test_test_wells_picked_from_kept_rows(resolvers, tmp_path):
    """The first phenotype well is ungrouped here; the test wells must be annotated ones."""
    _screen(tmp_path, merged=("r02c02", "r02c03", "r03c02"))
    rows = [
        _row("WELL_ANNOTATIONS_FP"),
        _row("TEST_PLATE", 1, "auto-probe"),
        _row("TEST_WELL_1"),
        _row("TEST_WELL_2", "r03c02", "auto-probe"),
    ]
    ctx = _ctx(tmp_path, "aggregate", rows)
    ctx["list_wells_from_metadata"] = lambda *a, **k: ["r03c02", "r02c02"]
    resolvers.resolve_phase("aggregate", ctx)
    assert [r["value"] for r in rows[2:]] == ["r02c02", "r02c03"]


def test_test_plate_moves_to_an_annotated_plate():
    rows = [_row("TEST_PLATE", 1, "auto-probe"), _row("TEST_WELL_1"), _row("TEST_WELL_2")]
    set_annotated_test_wells(rows, [(2, "A1")])
    assert [r["value"] for r in rows] == [2, "A1", "A1"]


def test_build_failure_pauses(resolvers, tmp_path):
    _screen(tmp_path, cellular="  well_annotations: well_annotations.tsv\n  split_by: nope\n")
    rp = resolvers.resolve_phase(
        "aggregate", _ctx(tmp_path, "aggregate", [_row("WELL_ANNOTATIONS_FP")])
    )
    assert rp["paused"] and "config error" in rp["reason"] and "nope" in rp["reason"]


def test_zero_wells_kept_pauses(resolvers, tmp_path):
    _screen(tmp_path, merged=())
    rp = resolvers.resolve_phase(
        "aggregate", _ctx(tmp_path, "aggregate", [_row("WELL_ANNOTATIONS_FP")])
    )
    assert rp["paused"] and "no well" in rp["reason"]


def test_table_removed_after_build_clears_path(resolvers, tmp_path):
    _screen(tmp_path, cellular="  well_annotations: null\n")
    ann = _row("WELL_ANNOTATIONS_FP", "config/well_annotations.tsv", "screen-yaml")
    split = _row("SPLIT_COL", "experiment", "screen-yaml")
    ctx = _ctx(tmp_path, "aggregate", [ann, split])
    resolvers.resolve_phase("aggregate", ctx)
    assert ann["value"] is None and ann["value_source"] == "lab-default"
    assert split["value"] == "class" and split["value_source"] == "lab-default"
    assert ("WELL_ANNOTATIONS_FP", "None") in ctx["_patched"]


PHENIX_LIKE = (
    "plate\twell\ttreatment\n"
    "1\tr02c02\tEthanol\n"
    "1\tr02c03\tEstradiol\n"
    "1\tr03c02\t\n"
)


def test_group_by_and_reference_give_the_vehicle_referenced_design(bw):
    screen = {"cellular": {"well_annotations": "w.tsv", "group_by": "treatment",
                           "reference_group": "Ethanol"}}
    out = bw._compute_aggregate_fields(screen)
    assert out["group_cols"] == ["treatment"] and "split_col" not in out
    assert out["bootstrap_control_scope"] == out["control_scope"] == "within_perturbation"
    assert out["bootstrap_reference_group"] == out["control_reference_group"] == "Ethanol"


def test_group_by_without_reference_keeps_the_pooled_default(bw):
    out = bw._compute_aggregate_fields(
        {"cellular": {"well_annotations": "w.tsv", "group_by": ["treatment", "dose"]}})
    assert out["group_cols"] == ["treatment", "dose"] and "control_scope" not in out
    assert "group_cols" not in bw._compute_aggregate_fields({"cellular": {"group_by": "treatment"}})


def test_group_by_drops_wells_without_a_value(tmp_path):
    rep = build_well_annotations(_screen(
        tmp_path, cellular="  well_annotations: well_annotations.tsv\n  group_by: treatment\n",
        merged=("r02c02", "r02c03", "r03c02"), table=PHENIX_LIKE))
    assert rep["wells"] == [(1, "r02c02"), (1, "r02c03")] and rep["blank_group"] == 1


def _preflight_issues(tmp_path, cellular):
    spec = importlib.util.spec_from_file_location("brieflow_preflight", SCRIPTS / "brieflow_preflight.py")
    pf = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_preflight"] = pf
    spec.loader.exec_module(pf)
    (tmp_path / "screen.yaml").write_text(
        "library:\n  raw_design_table_fp: d.tsv\ndata:\n  input_sbs_dir: s\n  input_phenotype_dir: p\n"
        "cellular:\n" + cellular)
    (tmp_path / "d.tsv").write_text("x\n")
    (tmp_path / "well_annotations.tsv").write_text(PHENIX_LIKE)
    return [i.code for i in pf.validate_screen_yaml_inputs(tmp_path)]


def test_preflight_checks_the_reference_against_the_table(tmp_path):
    base = "  well_annotations: well_annotations.tsv\n  group_by: treatment\n"
    assert _preflight_issues(tmp_path, base + "  reference_group: Ethanol\n") == []
    assert _preflight_issues(tmp_path, base + "  reference_group: DMSO\n") == [
        "reference-group-not-in-table"]
    assert _preflight_issues(
        tmp_path, "  well_annotations: well_annotations.tsv\n  reference_group: Ethanol\n"
    ) == ["reference-group-without-group-by"]
    assert _preflight_issues(
        tmp_path, "  well_annotations: well_annotations.tsv\n  group_by: dose\n"
    ) == ["well-annotations-columns"]
