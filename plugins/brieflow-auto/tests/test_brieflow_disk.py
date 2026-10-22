import importlib.util
import sys
from pathlib import Path
import pytest
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_disk.py"

@pytest.fixture(scope="module")
def dm():
    spec = importlib.util.spec_from_file_location("brieflow_disk", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_disk"] = m
    spec.loader.exec_module(m)
    return m

def _geom():
    return {"n_plates": 2, "n_wells": 6, "n_cycles": 12,
            "n_channels_per_cycle_sbs": 5, "n_tiles_per_well_sbs": 350,
            "n_rounds_phenotype": 2, "n_channels_per_round_phenotype": 5,
            "n_tiles_per_well_phenotype": 1500, "n_channel_combos": 19}

def test_disk_requirements_phase_breakdown(dm):
    out = dm.compute_disk_requirements(_geom())
    assert set(out["phase_breakdown"]) == {"preprocess","sbs","phenotype","merge","aggregate","cluster"}
    assert out["required_gb"] == pytest.approx(sum(out["phase_breakdown"].values()))
    assert out["phase_breakdown"]["preprocess"] > 0

def test_disk_requirements_missing_geometry_is_safe(dm):
    out = dm.compute_disk_requirements({})  # nothing known
    assert out["required_gb"] >= 0  # no crash; unknown dims contribute 0

def test_check_disk_available(dm, tmp_path):
    v = dm.check_disk_available(required_gb=1, path=tmp_path, headroom=1.2)
    assert set(v) >= {"ok", "required_gb", "available_gb", "headroom"}
    assert v["ok"] in (True, False)

def test_disk_requirements_snowdon_band(dm):
    out = dm.compute_disk_requirements(_geom())
    # ~9.6 TB for this geometry since raw input is freed after preprocess (abef5d1).
    assert 8000 <= out["required_gb"] <= 13000, f"required_gb={out['required_gb']} not in 8-13 TB band"
    assert out["phase_breakdown"]["preprocess"] > out["phase_breakdown"]["aggregate"]

def test_check_disk_available_missing_path(dm):
    v = dm.check_disk_available(1, "/nonexistent/xyz", 1.2)
    assert v["ok"] is False
