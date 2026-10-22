"""The phenotype retention floor (0.75, set on HeLa: ~75 px cells on 2400 px tiles) failed a
screen of ~94 px cells on 1480 px tiles, where edge removal alone drops ~12% of cells.
The floor now scales with the screen's expected edge keep."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_qc as qc  # noqa: E402

GATE = {"min": 0.75, "warn": 0.85}
REF = {"cell_diameter_px": 75, "tile_px": 2400, "metrics": ["cell_retention", "nuclei_retention"]}


def _config(diameter, tile):
    return {"phenotype": {"cell_diameter": diameter},
            "merge": {"phenotype_dimensions": [tile, tile]}}


def _phenotype(root, init_n, init_c, final_c, final_n):
    seg = root / "phenotype" / "eval" / "segmentation" / "P-1__segmentation_overview.tsv"
    seg.parent.mkdir(parents=True)
    seg.write_text("well\tinitial_nuclei\tinitial_cells\tfinal_cells\tfinal_nuclei\n"
                   f"A1\t{init_n}\t{init_c}\t{final_c}\t{final_n}\n")


def test_large_cells_on_small_tiles_lower_the_floor():
    g = qc.edge_scaled_gate(GATE, 94.47, 1480, REF)
    assert g["min"] == pytest.approx(0.75 * (1385.53 / 1480) ** 2 / (2325 / 2400) ** 2, abs=1e-3)
    assert g["edge_scaled"]["unscaled"] == {"min": 0.75, "warn": 0.85}


def test_reference_geometry_and_small_cells_keep_the_floor():
    assert qc.edge_scaled_gate(GATE, 75, 2400, REF)["min"] == 0.75
    assert qc.edge_scaled_gate(GATE, 20, 2400, REF)["min"] == 0.75  # never stricter
    assert qc.edge_scaled_gate(GATE, None, 1480, REF) is GATE  # unknown geometry: unchanged


def test_phase_gate_uses_the_screen_config(tmp_path):
    _phenotype(tmp_path, 311_600, 286_100, 202_850, 201_900)
    gates = {"phenotype": {"edge_reference": REF,
                           "phase_qc": {"cell_retention": GATE, "nuclei_retention": GATE}}}
    summary = qc.build_phase_qc(tmp_path, "phenotype", generated_at="t", gates=gates,
                                config=_config(94.47, 1480))
    by = {m.name: m for m in summary.metrics}
    assert by["cell_retention"].status == "warn"  # 0.709 >= scaled min 0.700
    assert by["nuclei_retention"].status == "fail"  # 0.648: nuclei loss is not edge loss alone
    assert qc.phase_gates(qc.load_qc_gates(), "phenotype")["cell_retention"] == GATE
    assert qc.load_qc_gates()["phenotype"]["edge_reference"] == REF
    assert by["cell_retention"].threshold["edge_scaled"]["tile_px"] == 1480
