"""The screen.yaml contract: which keys the plugin reads, which facts the operator must supply,
staging an operator's screen.yaml, and keeping the interview in step when screen.yaml changes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import brieflow_interview as bi  # noqa: E402
import brieflow_preflight as pf  # noqa: E402
import brieflow_screen_yaml as sy  # noqa: E402

FILLED = {
    "library": {
        "type": "multi",
        "detection_target": "DNA",
        "raw_design_table_fp": "config/design.csv",
        "barcode_map_col": "iBAR_2",
        "barcode_recomb_col": "iBAR_1",
        "barcode_length_map": 7,
        "barcode_length_recomb": 5,
        "sequencing_order": "map_recomb",
        "gene_symbol_col": "target_symbol",
    },
    "iss": {
        "cycles": 12,
        "chemistry": "combinatorial",
        "combinatorial": {"code": {"G": [], "T": ["Alexa 568"]}},
        "channels": [{"name": "DAPI"}, {"name": "Alexa 568"}],
    },
    "phenotype": {"channels": [{"biological_name": "DAPI"}]},
    "pipeline": {"conda_env": "brieflow_x"},
    "data": {"input_sbs_dir": "/data/x/input_sbs", "input_phenotype_dir": "/data/x/input_ph"},
}


def _canonical_template() -> Path | None:
    d = os.environ.get("BRIEFLOW_CANONICAL_NB_DIR")
    p = Path(d) / "screen.yaml" if d else None
    return p if p and p.is_file() else None


def _leaf_keys(obj, prefix=""):
    """Dotted keys of a template; a list of channel dicts counts as one leaf."""
    keys = set()
    for k, v in (obj or {}).items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            keys |= _leaf_keys(v, path)
        else:
            keys.add(path)
    return keys


def test_filled_screen_has_nothing_missing():
    assert sy.missing_required(FILLED) == []


def test_blank_fields_are_named_and_conditional_on_library_and_chemistry():
    screen = json.loads(json.dumps(FILLED))
    screen["library"]["detection_target"] = None
    screen["library"]["barcode_length_map"] = None
    screen["iss"]["combinatorial"] = {}
    screen["phenotype"]["channels"] = [{"biological_name": None}]
    assert sy.missing_required(screen) == [
        "library.detection_target",
        "library.barcode_length_map",
        "iss.combinatorial.code",
        "phenotype.channels[].biological_name",
    ]


def test_template_derivation_paths_are_read_keys():
    params = json.loads((ROOT / "data" / "interview_template.json").read_text())["params"]
    paths = {
        p["derivation"]["path"]
        for p in params
        if isinstance(p.get("derivation"), dict) and p["derivation"].get("path")
        and p["derivation"].get("type", "").startswith("screen_yaml")
    }
    assert paths <= set(sy.READ_KEYS), sorted(paths - set(sy.READ_KEYS))


def test_required_keys_are_read_keys():
    required = set(sy.REQUIRED_KEYS) | set(sy.REQUIRED_SIMPLE) | set(sy.REQUIRED_MULTI)
    assert required <= set(sy.READ_KEYS) | {"iss.combinatorial.code"}


@pytest.mark.skipif(_canonical_template() is None, reason="BRIEFLOW_CANONICAL_NB_DIR not set")
def test_canonical_template_holds_exactly_the_contract_keys():
    template = _canonical_template()
    keys = _leaf_keys(yaml.safe_load(template.read_text()))
    contract = set(sy.READ_KEYS) | set(sy.DESCRIPTIVE_KEYS)
    assert not contract - keys, f"in the contract, missing from the template: {sorted(contract - keys)}"
    assert not keys - contract, f"in the template, not in the contract: {sorted(keys - contract)}"


@pytest.mark.skipif(_canonical_template() is None, reason="BRIEFLOW_CANONICAL_NB_DIR not set")
def test_canonical_template_is_blank_and_describes_every_read_key():
    template = _canonical_template()
    assert sy.missing_required(yaml.safe_load(template.read_text())), "template ships filled in"
    lines = template.read_text().splitlines()
    undescribed = []
    for key in sy.READ_KEYS:
        leaf, depth = key.split(".")[-1], key.count(".")
        line = next((ln for ln in lines if ln.startswith("  " * depth + f"{leaf}:")), "")
        if "#" not in line:
            undescribed.append(key)
    assert not undescribed, f"read keys with no comment in the template: {undescribed}"


def test_stage_keeps_the_template_and_find_skips_blank_files(tmp_path):
    repo = tmp_path / "x-analysis"
    analysis = repo / "analysis"
    analysis.mkdir(parents=True)
    (analysis / "screen.yaml").write_text("library:\n  type: null\n")
    assert sy.find_operator_screen_yaml(analysis) == []
    (repo / "screen.yaml").write_text(yaml.safe_dump(FILLED))
    (found,) = sy.find_operator_screen_yaml(analysis)
    assert found == (repo / "screen.yaml").resolve()
    staged, *_ = sy.stage(found, analysis)
    assert yaml.safe_load(staged.read_text()) == FILLED
    assert len(list(analysis.glob("screen.yaml.template-*"))) == 1
    assert sy.check(analysis)["missing"] == []


def test_check_reports_a_blank_template(tmp_path):
    (tmp_path / "screen.yaml").write_text("library:\n  type: null\n")
    report = sy.check(tmp_path)
    assert report["parses"] and "data.input_sbs_dir" in report["missing"]
    assert sy.main(["check", "--analysis-dir", str(tmp_path)]) == 3


def test_interview_follows_a_replaced_screen_yaml(tmp_path):
    first = json.loads(json.dumps(FILLED))
    first["data"]["input_sbs_dir"] = "/old/sbs"
    (tmp_path / "screen.yaml").write_text(yaml.safe_dump(first))
    iv_path = tmp_path / ".brieflow" / "interview.json"
    iv_path.parent.mkdir()
    bi.init_screen_interview(bi._DEFAULT_TEMPLATE, iv_path, "x")
    iv = bi.load_interview(iv_path)
    sbs_row = next(p for p in iv["params"] if (p.get("derivation") or {}).get("path") == "data.input_sbs_dir")
    assert sbs_row["value"] == "/old/sbs"
    gene = next(p for p in iv["params"] if (p.get("derivation") or {}).get("path") == "library.gene_symbol_col")
    gene["value"], gene["value_source"] = "operator_col", "operator"
    bi.save_interview(iv, iv_path)

    (tmp_path / "screen.yaml").write_text(yaml.safe_dump(FILLED))
    bi.apply_screen_yaml_derivations(iv, FILLED, only_unset_or_from_screen=True)
    rows = {(p.get("derivation") or {}).get("path"): p for p in iv["params"]}
    assert rows["data.input_sbs_dir"]["value"] == "/data/x/input_sbs"
    assert rows["library.gene_symbol_col"]["value"] == "operator_col"


def test_dna_screen_with_count_sort_is_high(tmp_path):
    (tmp_path / "screen.yaml").write_text(yaml.safe_dump(FILLED))
    cfg = tmp_path / "config.yml"
    cfg.write_text("sbs: {sort_calls: count}\n")
    (issue,) = pf.validate_sort_calls(tmp_path, cfg)
    assert issue.severity == "HIGH" and issue.code == "sort-calls-not-peak-for-dna"
    cfg.write_text("sbs: {sort_calls: peak}\n")
    assert pf.validate_sort_calls(tmp_path, cfg) == []


def test_stage_brings_the_tables_the_screen_yaml_names(tmp_path):
    src_dir, analysis = tmp_path / "given", tmp_path / "repo" / "analysis"
    (src_dir / "config").mkdir(parents=True)
    analysis.mkdir(parents=True)
    screen = json.loads(json.dumps(FILLED))
    screen["cellular"] = {"well_annotations": "well_annotations.tsv", "split_by": "experiment"}
    (src_dir / "screen.yaml").write_text(yaml.safe_dump(screen))
    (src_dir / "well_annotations.tsv").write_text("plate\twell\texperiment\n")
    (src_dir / "config" / "design.csv").write_text("x\n")
    written = sy.stage(src_dir / "screen.yaml", analysis)
    assert {p.relative_to(analysis).as_posix() for p in written} == {
        "screen.yaml", "well_annotations.tsv", "config/design.csv"}


@pytest.mark.skipif(_canonical_template() is None, reason="BRIEFLOW_CANONICAL_NB_DIR not set")
def test_canonical_parameter_cells_hold_only_literal_assignments():
    """Every params_* cell assigns literal values (branches allowed); no stray comment fences
    except in notebooks that still need one."""
    import ast

    sys.path.insert(0, str(ROOT / "scripts"))
    from brieflow_markers import PARAMS_CELL_PREFIX
    from brieflow_phases import PHASE_NOTEBOOKS

    nb_dir = Path(os.environ["BRIEFLOW_CANONICAL_NB_DIR"])
    bad, cells = [], 0
    for fname in PHASE_NOTEBOOKS.values():
        tree = ast.parse((nb_dir / fname).read_text())
        for fn in tree.body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith(PARAMS_CELL_PREFIX)):
                continue
            cells += 1
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign):
                    try:
                        ast.literal_eval(node.value)
                    except ValueError:
                        bad.append(f"{fname}:{node.lineno} {fn.name}")
                elif isinstance(node, (ast.Call, ast.Import, ast.ImportFrom)) and not (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("set", "frozenset", "tuple", "list", "dict")
                ):
                    bad.append(f"{fname}:{node.lineno} {fn.name} has a call or import")
    assert cells >= 8 and not bad, bad


def test_dna_screen_fills_the_sbs_background_without_asking():
    screen = {"library": {"detection_target": "DNA"},
              "iss": {"channels": [{"name": "DAPI"}, {"name": "G"}, {"name": "T"}],
                      "background_channel_nucleus": {"channel": "DAPI", "cycle": None},
                      "background_channel_cell": {"channel": None, "cycle": None}}}
    assert bi._compute_sbs_background(screen) == {"dapi_cycle": 1, "cyto_channel": "G", "cyto_cycle": 1}
    screen["library"]["detection_target"] = "RNA"
    assert bi._compute_sbs_background(screen) == {"dapi_cycle": 1}
    screen["iss"]["background_channel_cell"] = {"channel": "T", "cycle": 2}
    assert bi._compute_sbs_background(screen)["cyto_channel"] == "T"


def test_one_aggregation_of_all_phenotype_channels():
    screen = {"phenotype": {"channels": [{"biological_name": "DAPI"}, {"biological_name": "WGA"}]}}
    assert bi._compute_channel_combos(screen) == {"channel_combos": [["DAPI", "WGA"]]}


def test_alignment_comes_from_screen_yaml_or_several_rounds(tmp_path):
    assert bi._compute_phenotype_align({"phenotype": {"channels_imaged_together": True}}) == {"align": False}
    assert bi._compute_phenotype_align({"phenotype": {"channels_imaged_together": False}}) == {"align": True}
    assert bi._compute_phenotype_align({"phenotype": {}}) is None
    (tmp_path / "screen.yaml").write_text(yaml.safe_dump(FILLED))
    iv_path = tmp_path / ".brieflow" / "interview.json"
    iv_path.parent.mkdir()
    bi.init_screen_interview(bi._DEFAULT_TEMPLATE, iv_path, "x")
    iv = bi.load_interview(iv_path)
    bi.apply_preprocess_probe(iv, {"prefilled": {"PHENOTYPE_ROUND_ORDER": None}})
    assert bi.find_param(iv, "ALIGN", notebook="phenotype")["value_source"] is None
    bi.apply_preprocess_probe(iv, {"prefilled": {"PHENOTYPE_ROUND_ORDER": [1, 2]}})
    row = bi.find_param(iv, "ALIGN", notebook="phenotype")
    assert row["value"] is True and row["value_source"] == "auto-probe"


def test_interview_from_an_older_template_adopts_new_definitions(tmp_path):
    (tmp_path / "screen.yaml").write_text(yaml.safe_dump(FILLED))
    iv_path = tmp_path / ".brieflow" / "interview.json"
    iv_path.parent.mkdir()
    bi.init_screen_interview(bi._DEFAULT_TEMPLATE, iv_path, "x")
    iv = bi.load_interview(iv_path)
    seg = bi.find_param(iv, "SEGMENTATION_METHOD", notebook="phenotype")
    seg["bucket"] = "user"
    combos = bi.find_param(iv, "CHANNEL_COMBOS", notebook="aggregate")
    combos.pop("derivation", None)
    gene = bi.find_param(iv, "GENE_SYMBOL_COL", notebook="sbs")
    gene["value"], gene["value_source"] = "operator_col", "operator"
    iv["_csv_sha"] = "old"
    bi.save_interview(iv, iv_path)
    bi.resolve_all_phases(tmp_path)
    iv = bi.load_interview(iv_path)
    assert bi.find_param(iv, "SEGMENTATION_METHOD", notebook="phenotype")["value"] == "cellpose"
    assert bi.find_param(iv, "CHANNEL_COMBOS", notebook="aggregate")["value"] == [["DAPI"]]
    assert bi.find_param(iv, "GENE_SYMBOL_COL", notebook="sbs")["value"] == "operator_col"


def test_design_table_and_standardized_library_never_share_a_path(tmp_path):
    screen = json.loads(json.dumps(FILLED))
    screen["library"]["raw_design_table_fp"] = "config/barcode_library.tsv"
    (tmp_path / "screen.yaml").write_text(yaml.safe_dump(screen))
    bi.resolve_all_phases(tmp_path)
    iv = bi.load_interview(tmp_path / ".brieflow" / "interview.json")
    assert bi.find_param(iv, "DF_DESIGN_FP", notebook="sbs")["value"] == "config/barcode_library.tsv"
    assert bi.find_param(iv, "DF_BARCODE_LIBRARY_FP", notebook="sbs")["value"] == (
        "config/barcode_library_standardized.tsv")
