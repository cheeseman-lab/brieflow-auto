"""The headless fallback re-ran imports, alignment and segmentation for every value of a
read-calling parameter (~14 min each on one screen). The cell runner pays them once per tile
and reruns only the cells downstream of the changed marker that the metric needs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_cell_runner as cr  # noqa: E402

CELLS = [
    "import math\nLOG = []",                                       # 0 imports
    "TEST_TILE = 1\nPEAK_WIDTH = 5",                               # 1 marker (upstream)
    "LOG.append('segment')\nnuclei = TEST_TILE * 10",              # 2 expensive upstream
    "def _helper(x):\n    return x\npeaks = nuclei + _helper(PEAK_WIDTH)",  # 3
    "THRESHOLD_READS = 100",                                       # 4 marker (downstream)
    "LOG.append('call')\nreads = peaks - THRESHOLD_READS / 100",   # 5
    "LOG.append('plot')\nprint('plot', reads)",                    # 6 display only
    "rate = max(0.0, reads / 20)\nprint(f'SINGLE_MAP_RATE: {rate}')",  # 7 metric
]


def test_names_follow_marimo_rules():
    defs, refs = cr.cell_names(CELLS[3])
    assert defs == {"peaks"} and refs == {"nuclei", "PEAK_WIDTH"}
    assert cr.cell_names("import os.path as p\nfor i in range(2):\n    k = i")[0] == {"p", "i", "k"}


def test_prime_once_then_rerun_only_what_the_metric_needs():
    runner = cr.CellRunner(CELLS)
    assert "SINGLE_MAP_RATE: 0.7" in runner.prime()
    assert runner.ns["LOG"] == ["segment", "call", "plot"]
    out = runner.trial({"THRESHOLD_READS": 200})
    assert "SINGLE_MAP_RATE: 0.65" in out
    assert runner.ns["LOG"] == ["segment", "call", "plot", "call"]  # no segment, no plot
    out = runner.trial({"PEAK_WIDTH": 7})
    assert "SINGLE_MAP_RATE: 0.75" in out and runner.ns["LOG"][-1] == "call"
    assert runner.ns["LOG"].count("segment") == 1


def test_a_cell_error_is_output_not_a_crash():
    runner = cr.CellRunner(CELLS[:4] + ["THRESHOLD_READS = 1", "reads = 1 / (THRESHOLD_READS - 1)",
                                        "print(f'SINGLE_MAP_RATE: {reads}')"])
    out = runner.prime()
    assert "ZeroDivisionError" in out


def test_set_assignment_rewrites_one_marker_line():
    code = "    THRESHOLD_READS = 400  # library default\nX = 1"
    assert cr.set_assignment(code, "THRESHOLD_READS", 50).splitlines()[0] == (
        "    THRESHOLD_READS = 50  # sweep trial"
    )
