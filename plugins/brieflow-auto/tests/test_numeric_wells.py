"""Filenames `Well{N}_Point{N}_{tile}` lost the well: the pattern search read the point as the
tile. Numeric wells are now reported and routed to the operator."""

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("brieflow_probe", SCRIPTS / "brieflow_probe.py")
probe = importlib.util.module_from_spec(spec)
sys.modules["brieflow_probe"] = probe
spec.loader.exec_module(probe)

NAMES = [f"c1/Well{w}_Point{w}_{t:04d}_Seq{t:04d}.nd2" for w in (1, 2, 3) for t in (0, 1)]


def test_numeric_wells_detected():
    assert probe.numeric_well_tokens(NAMES) == ["1", "2", "3"]
    assert probe.numeric_well_tokens(["c1/WellA1_Point1_0000_Seq0000.nd2"]) == []


def test_probe_routes_numeric_wells_to_operator(tmp_path, monkeypatch):
    sbs = tmp_path / "sbs"
    for name in NAMES:
        (sbs / name).parent.mkdir(parents=True, exist_ok=True)
        (sbs / name).write_bytes(b"")
    monkeypatch.setattr(probe, "_read_nd2_meta", lambda p: {
        "channels": ["DAPI", "G", "T", "A", "C"], "n_z": None, "n_points": 1, "voxel_um": None})
    monkeypatch.setattr(probe, "detect_organization", lambda *a, **k: "tile")
    screen = {"data": {"input_sbs_dir": str(sbs), "input_phenotype_dir": None},
              "iss": {"channels": [{"name": n} for n in ["DAPI", "G", "T", "A", "C"]]},
              "phenotype": {"channels": []}}
    rep = probe.probe_preprocess(tmp_path, screen)
    assert "SBS_PATH_PATTERN" in rep["operator_needed"]
    assert any("numeric wells (Well1.." in n for n in rep["notes"])
