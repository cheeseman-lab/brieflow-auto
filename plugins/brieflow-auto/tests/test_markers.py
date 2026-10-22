import ast
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from brieflow_markers import patch_marker_block_value  # noqa: E402

MULTILINE_NB = '''import marimo as mo
@app.cell
def _():
    # === OPERATOR PARAMETERS ===
    THRESHOLD_READS = 50
    COMBINATORIAL_CODE = {
        "A": ["Red"],
        "C": ["Green"],
        "G": ["Red", "Green"],
        "T": [],
    }
    # === END OPERATOR PARAMETERS ===
    return COMBINATORIAL_CODE, THRESHOLD_READS
'''

def test_patch_multiline_dict_literal_stays_valid_python(tmp_path):
    nb = tmp_path / "2_sbs.py"
    nb.write_text(MULTILINE_NB)
    new = '{"A": ["Green"], "C": ["Red"], "G": [], "T": ["Red", "Green"]}'
    assert patch_marker_block_value(str(nb), "COMBINATORIAL_CODE", new) is True
    ast.parse(nb.read_text())  # must not raise
    assert '"A": ["Green"]' in nb.read_text()
    assert '"G": ["Red", "Green"]' not in nb.read_text()  # old value gone
    # neighbor var untouched, on its own line
    assert "THRESHOLD_READS = 50" in nb.read_text()

def test_patch_single_line_value_unchanged(tmp_path):
    nb = tmp_path / "2_sbs.py"
    nb.write_text(MULTILINE_NB)
    assert patch_marker_block_value(str(nb), "THRESHOLD_READS", "75") is True
    assert "THRESHOLD_READS = 75" in nb.read_text()
    ast.parse(nb.read_text())


PARAMS_CELL_NB = '''import marimo

app = marimo.App()


@app.cell
def params_reads():
    THRESHOLD_READS = 50  # e.g. 50-400
    COMBINATORIAL_CODE = {
        "A": ["Red"],
        "T": [],
    }
    return COMBINATORIAL_CODE, THRESHOLD_READS


@app.cell
def _(THRESHOLD_READS):
    THRESHOLD_SCALED = THRESHOLD_READS * 2
    return (THRESHOLD_SCALED,)
'''


def test_params_cell_counts_as_a_marker_block(tmp_path):
    from brieflow_markers import find_marker_cell, scan_marker_blocks, verify_marker_coverage

    nb = tmp_path / "1_sbs.py"
    nb.write_text(PARAMS_CELL_NB)
    assert scan_marker_blocks(nb) == [(8, 12)]
    report = verify_marker_coverage(nb, ["THRESHOLD_READS", "COMBINATORIAL_CODE"])
    assert sorted(report["covered"]) == ["COMBINATORIAL_CODE", "THRESHOLD_READS"]
    assert find_marker_cell(nb, "THRESHOLD_READS") == "params_reads"
    assert find_marker_cell(nb, "THRESHOLD_SCALED") is None


def test_patch_in_params_cell_keeps_comment_and_python(tmp_path):
    nb = tmp_path / "1_sbs.py"
    nb.write_text(PARAMS_CELL_NB)
    assert patch_marker_block_value(nb, "THRESHOLD_READS", "120") is True
    assert patch_marker_block_value(nb, "COMBINATORIAL_CODE", '{"A": [], "T": ["Red"]}') is True
    text = nb.read_text()
    ast.parse(text)
    assert "THRESHOLD_READS = 120  # e.g. 50-400" in text
    assert 'COMBINATORIAL_CODE = {"A": [], "T": ["Red"]}' in text
    assert "THRESHOLD_SCALED = THRESHOLD_READS * 2" in text


def test_derived_assignment_outside_params_cells_is_not_patched(tmp_path):
    import pytest

    nb = tmp_path / "1_sbs.py"
    nb.write_text(PARAMS_CELL_NB)
    with pytest.raises(ValueError):
        patch_marker_block_value(nb, "THRESHOLD_SCALED", "1")


def test_live_context_finds_params_cell_by_name():
    from brieflow_markers import find_marker_cell

    class Cell:
        def __init__(self, cid, name, code):
            self.id, self.name, self.code = cid, name, code

    ctx = type("Ctx", (), {"cells": {"a": Cell("a", "_", "X = THRESHOLD_READS"),
                                     "b": Cell("b", "params_reads", "THRESHOLD_READS = 50")}})()
    assert find_marker_cell(ctx, "THRESHOLD_READS") == "b"


def test_tuned_values_read_back_from_config(tmp_path):
    from brieflow_interview import pull_tuned_values

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yml").write_text(
        "sbs: {nuclei_diameter: 11.5, cell_diameter: null, threshold_peaks: 300}\n")
    assert pull_tuned_values(tmp_path, "sbs") == {
        "NUCLEI_DIAMETER": {"derived": 11.5, "src": "config.yml sbs.nuclei_diameter"},
        "THRESHOLD_READS": {"derived": 300, "src": "config.yml sbs.threshold_peaks"},
    }
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / ".brieflow" / "tuned_sbs.json").write_text('{"PEAK_WIDTH": {"derived": 5}}')
    assert pull_tuned_values(tmp_path, "sbs") == {"PEAK_WIDTH": {"derived": 5}}


def test_phase_notebook_prefers_current_names_and_finds_old_ones(tmp_path):
    from brieflow_phases import notebook_phase, phase_notebook

    (tmp_path / "2_sbs.py").write_text("")
    assert phase_notebook(tmp_path, "sbs").name == "2_sbs.py"
    (tmp_path / "1_sbs.py").write_text("")
    assert phase_notebook(tmp_path, "sbs").name == "1_sbs.py"
    assert phase_notebook(tmp_path, "cluster").name == "6_cluster.py"
    assert notebook_phase("10_cluster.py") == notebook_phase("6_cluster.py") == "cluster"
    assert notebook_phase("screen.py") is None
