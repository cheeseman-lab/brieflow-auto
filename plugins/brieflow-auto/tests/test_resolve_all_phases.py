"""resolve_all_phases: bootstrap screen.yaml derivations, then collect gaps.

Uses the real interview_template.json as the source of truth.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_wizard.py"
TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "interview_template.json"


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_screen_yaml(path: Path, extra: dict | None = None) -> None:
    """Write a minimal screen.yaml that fills SBS_IMAGES_DIR_FP and
    PHENOTYPE_IMAGES_DIR_FP (screen_yaml-derivable, bucket=user) but leaves
    most operator rows null."""
    data = {
        "collection": {"screen_id": "test-screen"},
        "data": {
            "input_sbs_dir": "/data/sbs",
            "input_phenotype_dir": "/data/phenotype",
            "storage_format": "nd2",
        },
        "iss": {
            "channels": [
                {"name": "DAPI"},
                {"name": "G"},
                {"name": "T"},
                {"name": "A"},
                {"name": "C"},
            ],
            "background_channel": {"channel": "DAPI", "cycle": 1},
        },
        "phenotype": {
            "channels": [{"biological_name": "DAPI"}, {"biological_name": "WGA"}],
            "segment_cells": False,
            "background_channel_cell": None,
        },
        "library": {
            "type": "simple",
            "negative_controls": ["nontargeting"],
        },
    }
    if extra:
        data.update(extra)
    with open(path, "w") as f:
        yaml.dump(data, f)


@pytest.fixture
def analysis_dir(tmp_path):
    """analysis_dir IS the screen repo root; screen.yaml lives inside it.

    Conventional layout:
        <analysis_dir>/screen.yaml
        <analysis_dir>/.brieflow/interview.json  ← init_screen_interview dest
    init_screen_interview resolves screen.yaml as dst.parent.parent / screen.yaml,
    where dst = <analysis_dir>/.brieflow/interview.json, so dst.parent.parent
    = <analysis_dir>.
    """
    adir = tmp_path / "myscreen-analysis"
    adir.mkdir()
    _write_screen_yaml(adir / "screen.yaml")
    return adir


# ---------------------------------------------------------------------------
# core contract
# ---------------------------------------------------------------------------


def test_returns_filled_and_gaps_keys(bw, analysis_dir):
    result = bw.resolve_all_phases(analysis_dir)
    assert set(result.keys()) == {"filled", "gaps", "note_overrides"}
    assert isinstance(result["filled"], list)
    assert isinstance(result["gaps"], list)
    assert isinstance(result["note_overrides"], list)  # candidate overrides from additional_notes


def test_screen_yaml_derivable_rows_in_filled(bw, analysis_dir):
    """SBS_IMAGES_DIR_FP derives from data.input_sbs_dir — should be filled."""
    result = bw.resolve_all_phases(analysis_dir)
    assert "SBS_IMAGES_DIR_FP" in result["filled"], (
        "SBS_IMAGES_DIR_FP (screen_yaml derivation) should appear in filled"
    )
    assert "PHENOTYPE_IMAGES_DIR_FP" in result["filled"], (
        "PHENOTYPE_IMAGES_DIR_FP (screen_yaml derivation) should appear in filled"
    )


def test_screen_yaml_derivable_rows_not_in_gaps(bw, analysis_dir):
    result = bw.resolve_all_phases(analysis_dir)
    gap_names = {g["param_name"] for g in result["gaps"]}
    assert "SBS_IMAGES_DIR_FP" not in gap_names
    assert "PHENOTYPE_IMAGES_DIR_FP" not in gap_names


def test_null_operator_row_in_gaps(bw, analysis_dir):
    """Active operator-bucket rows with no value should be gaps."""
    result = bw.resolve_all_phases(analysis_dir)
    assert len(result["gaps"]) > 0, "Expected at least one gap (null operator row)"


def test_gap_has_required_fields(bw, analysis_dir):
    result = bw.resolve_all_phases(analysis_dir)
    for gap in result["gaps"]:
        assert "param_name" in gap, f"gap missing param_name: {gap}"
        assert "notebook" in gap, f"gap missing notebook: {gap}"
        assert "notes" in gap, f"gap missing notes: {gap}"


def test_gap_notebook_is_valid_phase(bw, analysis_dir):
    valid_phases = {"preprocess", "sbs", "phenotype", "merge", "aggregate", "cluster"}
    result = bw.resolve_all_phases(analysis_dir)
    for gap in result["gaps"]:
        assert gap["notebook"] in valid_phases, (
            f"gap {gap['param_name']} has unknown notebook {gap['notebook']!r}"
        )


# ---------------------------------------------------------------------------
# lab_defaults.notebook_defaults fill
# ---------------------------------------------------------------------------


def test_lab_default_fills_invariant_param(bw, analysis_dir):
    """AGG_METHOD is in lab_defaults.notebook_defaults — it should be filled
    (value + value_source=lab-default), not left as a gap."""
    result = bw.resolve_all_phases(analysis_dir)
    gap_names = {g["param_name"] for g in result["gaps"]}
    assert "AGG_METHOD" not in gap_names
    assert "AGG_METHOD" in result["filled"]
    iv = bw.load_interview(analysis_dir / ".brieflow" / "interview.json")
    row = bw.find_param(iv, "AGG_METHOD", notebook="aggregate")
    assert row["value"] == "median"
    assert row["value_source"] == "lab-default"


def test_lab_default_does_not_override_operator(bw, analysis_dir, tmp_path):
    """An operator-set value wins over the lab default."""
    # First resolve creates the interview; then set an operator value and re-resolve.
    bw.resolve_all_phases(analysis_dir)
    iv_path = analysis_dir / ".brieflow" / "interview.json"
    iv = bw.load_interview(iv_path)
    bw.set_value(iv, "AGG_METHOD", "mean", "operator", notebook="aggregate")
    bw.save_interview(iv, iv_path)

    bw.resolve_all_phases(analysis_dir)
    iv2 = bw.load_interview(iv_path)
    row = bw.find_param(iv2, "AGG_METHOD", notebook="aggregate")
    assert row["value"] == "mean"
    assert row["value_source"] == "operator"


def test_lab_default_custom_path(bw, analysis_dir, tmp_path):
    """A custom lab_defaults path is honored."""
    import yaml as _y
    custom = tmp_path / "lab.yaml"
    custom.write_text(_y.dump({"notebook_defaults": {"aggregate": {"AGG_METHOD": "mean"}}}))
    bw.resolve_all_phases(analysis_dir, lab_defaults_path=str(custom))
    iv = bw.load_interview(analysis_dir / ".brieflow" / "interview.json")
    row = bw.find_param(iv, "AGG_METHOD", notebook="aggregate")
    assert row["value"] == "mean"
    assert row["value_source"] == "lab-default"


# ---------------------------------------------------------------------------
# aggregate derivations (control_key, filter_queries) + lab-default id cols
# ---------------------------------------------------------------------------


def test_control_key_derived_from_negative_controls(bw, analysis_dir):
    bw.resolve_all_phases(analysis_dir)
    iv = bw.load_interview(analysis_dir / ".brieflow" / "interview.json")
    row = bw.find_param(iv, "CONTROL_KEY", notebook="aggregate")
    assert row["value"] == "nontargeting"
    assert row["value_source"] == "screen-yaml"


def test_filter_queries_simple_base_only(bw, analysis_dir):
    bw.resolve_all_phases(analysis_dir)
    iv = bw.load_interview(analysis_dir / ".brieflow" / "interview.json")
    row = bw.find_param(iv, "FILTER_QUERIES", notebook="aggregate")
    assert row["value"] == ["mapped_single_gene == True"]


def test_filter_queries_multi_adds_recomb(bw, tmp_path):
    adir = tmp_path / "multi-analysis"
    adir.mkdir()
    _write_screen_yaml(
        adir / "screen.yaml",
        extra={"library": {"type": "multi", "negative_controls": ["nt_intergenic_"]}},
    )
    bw.resolve_all_phases(adir)
    iv = bw.load_interview(adir / ".brieflow" / "interview.json")
    fq = bw.find_param(iv, "FILTER_QUERIES", notebook="aggregate")["value"]
    assert fq == [
        "mapped_single_gene == True",
        "no_recomb_0 != False or no_recomb_0.isna()",
    ]
    ck = bw.find_param(iv, "CONTROL_KEY", notebook="aggregate")["value"]
    assert ck == "nt_intergenic_"


def test_perturbation_id_and_collapse_cols_lab_default(bw, analysis_dir):
    bw.resolve_all_phases(analysis_dir)
    iv = bw.load_interview(analysis_dir / ".brieflow" / "interview.json")
    pid = bw.find_param(iv, "PERTURBATION_ID_COL", notebook="aggregate")
    assert pid["value"] == "cell_barcode_0"
    assert pid["value_source"] == "lab-default"
    cc = bw.find_param(iv, "COLLAPSE_COLS", notebook="aggregate")
    assert cc["value"] == ["cell_barcode_0", "gene_symbol_0"]
    assert cc["value_source"] == "lab-default"


def test_aggregate_derived_not_in_gaps(bw, analysis_dir):
    result = bw.resolve_all_phases(analysis_dir)
    gap_names = {g["param_name"] for g in result["gaps"]}
    for nm in ("CONTROL_KEY", "FILTER_QUERIES", "PERTURBATION_ID_COL", "COLLAPSE_COLS"):
        assert nm not in gap_names, f"{nm} should be derived/lab-filled, not a gap"


def test_cluster_test_resolutions_lab_filled(bw, analysis_dir):
    """TEST_LEIDEN_RESOLUTIONS is the fixed coarse sweep (lab default)."""
    bw.resolve_all_phases(analysis_dir)
    iv = bw.load_interview(analysis_dir / ".brieflow" / "interview.json")
    row = bw.find_param(iv, "TEST_LEIDEN_RESOLUTIONS", notebook="cluster")
    assert row["value"] == [0.1, 1, 5, 7, 9, 11, 13, 15, 20, 100]
    assert row["value_source"] == "lab-default"


def test_cluster_test_resolution_singular_lab_filled(bw, analysis_dir):
    """TEST_LEIDEN_RESOLUTION (singular demo pick) is a fixed mid-sweep lab default."""
    bw.resolve_all_phases(analysis_dir)
    iv = bw.load_interview(analysis_dir / ".brieflow" / "interview.json")
    row = bw.find_param(iv, "TEST_LEIDEN_RESOLUTION", notebook="cluster")
    assert row["value"] == 5
    assert row["value_source"] == "lab-default"


def test_cluster_final_resolutions_not_asked_upfront(bw, analysis_dir):
    """FINAL_LEIDEN_RESOLUTIONS is resolved at run time (agent selects from the
    resolution table), so it is NOT an up-front operator question."""
    result = bw.resolve_all_phases(analysis_dir)
    gap_names = {g["param_name"] for g in result["gaps"]}
    assert "FINAL_LEIDEN_RESOLUTIONS" not in gap_names
    iv = bw.load_interview(analysis_dir / ".brieflow" / "interview.json")
    row = bw.find_param(iv, "FINAL_LEIDEN_RESOLUTIONS", notebook="cluster")
    assert row["value"] is None  # filled by the agent during the cluster phase
    assert row["bucket"] == "agent-resolved"


def test_swept_params_not_asked_upfront(bw, analysis_dir):
    """Sweep-band params (THRESHOLD_READS, PEAK_WIDTH) are resolved by the
    agent's sweep, not asked in the up-front interview."""
    result = bw.resolve_all_phases(analysis_dir)
    gap_names = {g["param_name"] for g in result["gaps"]}
    assert "THRESHOLD_READS" not in gap_names
    assert "PEAK_WIDTH" not in gap_names


def test_genuine_operator_params_still_asked(bw, analysis_dir):
    """Params that can't be derived, defaulted or swept are still up-front questions;
    the channel combos (from screen.yaml) and cell cutoffs (lab default) no longer are."""
    result = bw.resolve_all_phases(analysis_dir)
    gap_names = {g["param_name"] for g in result["gaps"]}
    assert {"ALIGN", "DF_DESIGN_FP"} <= gap_names
    assert not {"CHANNEL_COMBOS", "MIN_CELL_CUTOFFS", "SEGMENTATION_METHOD"} & gap_names


# ---------------------------------------------------------------------------
# inactive rows must NOT appear in gaps
# ---------------------------------------------------------------------------


def test_inactive_gated_row_not_in_gaps(bw, tmp_path):
    """SBS_METADATA_IMAGES_DIR_FP is gated on SBS_DATA_FORMAT==tiff. SBS_DATA_FORMAT
    is an auto-probe row (filled from the raw-data probe, not screen.yaml), so it is
    null in the up-front resolution — the dependent row is inactive and must NOT
    appear in gaps."""
    adir = tmp_path / "nd2screen-analysis"
    adir.mkdir()
    _write_screen_yaml(adir / "screen.yaml")  # sets storage_format: nd2

    result = bw.resolve_all_phases(adir)
    gap_names = {g["param_name"] for g in result["gaps"]}
    # Gated on SBS_DATA_FORMAT==tiff (auto-probe, null up front) — inactive
    assert "SBS_METADATA_IMAGES_DIR_FP" not in gap_names, (
        "SBS_METADATA_IMAGES_DIR_FP should be inactive (gate unset) and absent from gaps"
    )
    assert "SBS_METADATA_PATH_PATTERN" not in gap_names
    assert "SBS_METADATA_PATH_METADATA" not in gap_names


def test_metadata_rows_are_auto_probe_never_asked(bw, tmp_path):
    """The SBS metadata rows are auto-probe bucket: resolved by the raw-data probe
    during the run, never an up-front operator question. Even with storage_format=tiff
    in screen.yaml they stay out of gaps (the gate driver SBS_DATA_FORMAT is itself
    auto-probe, and auto-probe rows are not operator buckets)."""
    adir = tmp_path / "tiffscreen-analysis"
    adir.mkdir()
    _write_screen_yaml(adir / "screen.yaml", extra={"data": {
        "input_sbs_dir": "/data/sbs",
        "input_phenotype_dir": "/data/phenotype",
        "storage_format": "tiff",
    }})

    result = bw.resolve_all_phases(adir)
    gap_names = {g["param_name"] for g in result["gaps"]}
    assert "SBS_METADATA_IMAGES_DIR_FP" not in gap_names
    iv = bw.load_interview(adir / ".brieflow" / "interview.json")
    assert bw.find_param(iv, "SBS_METADATA_IMAGES_DIR_FP", notebook="preprocess")[
        "bucket"
    ] == "auto-probe"


# ---------------------------------------------------------------------------
# idempotency: calling twice with existing interview.json
# ---------------------------------------------------------------------------


def test_idempotent_second_call(bw, analysis_dir):
    """Second call reuses existing interview.json and returns the same result."""
    r1 = bw.resolve_all_phases(analysis_dir)
    r2 = bw.resolve_all_phases(analysis_dir)
    assert r1["filled"] == r2["filled"]
    gap_names_1 = {g["param_name"] for g in r1["gaps"]}
    gap_names_2 = {g["param_name"] for g in r2["gaps"]}
    assert gap_names_1 == gap_names_2


# ---------------------------------------------------------------------------
# explicit template_path argument
# ---------------------------------------------------------------------------


def test_explicit_template_path(bw, analysis_dir):
    """Passing template_path explicitly should work identically to the default."""
    r_default = bw.resolve_all_phases(analysis_dir)
    # delete the generated interview.json so init runs again with explicit path
    iv = analysis_dir / ".brieflow" / "interview.json"
    iv.unlink()

    r_explicit = bw.resolve_all_phases(analysis_dir, template_path=str(TEMPLATE))
    assert set(r_default["filled"]) == set(r_explicit["filled"])
    assert {g["param_name"] for g in r_default["gaps"]} == {
        g["param_name"] for g in r_explicit["gaps"]
    }


def test_phenotype_operator_surface_is_key_decisions(bw, analysis_dir):
    """The phenotype operator question is the genuine per-screen decision ALIGN (channel
    alignment on/off), asked when screen.yaml phenotype.channels_imaged_together is blank and
    the data has one round; SEGMENTATION_METHOD is the lab default cellpose.
    CYTO_CHANNEL is gated on SEGMENT_CELLS==True, so it is inactive (not asked)
    until segmentation is known. FOCI_CHANNEL is screen.yaml-derived (null_ok: an
    absent/null foci_channel is a valid 'no foci' answer, not a gap)."""
    result = bw.resolve_all_phases(analysis_dir)
    gaps = {g["param_name"] for g in result["gaps"] if g["notebook"] == "phenotype"}
    assert gaps == {"ALIGN"}
    iv = bw.load_interview(analysis_dir / ".brieflow" / "interview.json")
    # FOCI_CHANNEL: screen.yaml has no phenotype.foci_channel -> filled null, routed off
    fc = bw.find_param(iv, "FOCI_CHANNEL", notebook="phenotype")
    assert fc["value"] is None
    assert fc["value_source"] == "screen-yaml"
    # ALIGN-gated rows stay inactive while ALIGN is unanswered, so they are not gaps
    assert bw.is_active(bw.find_param(iv, "TARGET", notebook="phenotype"), iv) is False


def test_sbs_operator_surface_is_experimental_decisions(bw, analysis_dir):
    """SBS operator gaps are only genuine per-screen decisions: the channel/cycle
    layout that this minimal fixture doesn't supply (DAPI/CYTO cycles+channel,
    SEGMENT_CELLS), the segmentation method (key-decision), the design table +
    barcode column, and the gene columns. RULE: a screen.yaml value left blank is
    asked of the operator (never guessed). Lab-default rows (ERROR_CORRECT,
    SKIP_CYCLES, CALL_READS_METHOD, ...) and auto/auto-probe rows are filled or
    resolved later, never asked. SPECIES_ID + CHEMISTRY are also gaps here: this
    fixture's screen.yaml has no `experiment` block and no `iss.chemistry`, so
    both computed derivations return None and the rows fall through to asked
    (never guessed at human/four_color)."""
    result = bw.resolve_all_phases(analysis_dir)
    gaps = {g["param_name"] for g in result["gaps"] if g["notebook"] == "sbs"}
    assert gaps == {
        "BARCODE_COL", "CYTO_CHANNEL", "CYTO_CYCLE", "DF_DESIGN_FP",
        "GENE_ID_COL", "GENE_SYMBOL_COL", "PREFIX_LENGTH",
        "SEGMENT_CELLS", "SPECIES_ID", "CHEMISTRY",
    }
    iv = bw.load_interview(analysis_dir / ".brieflow" / "interview.json")
    # GENE_*_COL blank in this fixture's screen.yaml -> asked, NOT guessed
    assert bw.find_param(iv, "GENE_ID_COL", notebook="sbs")["value"] is None
    assert bw.find_param(iv, "GENE_SYMBOL_COL", notebook="sbs")["value"] is None
    # Lab-default rows are filled, not asked
    assert bw.find_param(iv, "ERROR_CORRECT", notebook="sbs")["value_source"] == "lab-default"
    assert bw.find_param(iv, "CALL_READS_METHOD", notebook="sbs")["value_source"] == "lab-default"
    # CMAPS auto-derived from channel colors, WINDOW lab-filled
    cc = bw.find_param(iv, "CHANNEL_CMAPS", notebook="sbs")
    assert cc["value"] == ["pure_blue", "pure_green", "pure_red", "pure_magenta", "pure_cyan"]
    assert bw.find_param(iv, "WINDOW", notebook="sbs")["value"] == 2
