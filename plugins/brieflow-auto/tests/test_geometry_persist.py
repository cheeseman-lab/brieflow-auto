"""The runner probed geometry for the launch review but never wrote .brieflow/geometry.json, so
every formula-sized rule fell back to the 3000 MB default (24 illumination-correction OOMs)."""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runner.py"
spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPT)
runner = importlib.util.module_from_spec(spec)
sys.modules.setdefault("brieflow_runner", runner)
spec.loader.exec_module(runner)

FULL = {"n_tiles_per_well_per_round_phenotype": 1460, "n_tiles_per_well_per_cycle_sbs": 368}


def _saved(tmp_path):
    return json.loads((tmp_path / ".brieflow" / "geometry.json").read_text())


def test_probe_is_persisted_for_the_resolver(tmp_path):
    runner.refresh_geometry(tmp_path, tmp_path / "config.yml", dict(FULL))
    assert _saved(tmp_path) == FULL
    sys.path.insert(0, str(SCRIPT.parent))
    import brieflow_resolve_resources as rr

    assert rr.resolve(tmp_path)["calculate_ic_sbs"]["mem_mb"] == 100 * 368 + 2000


def test_partial_probe_never_replaces_a_complete_file(tmp_path):
    runner.refresh_geometry(tmp_path, tmp_path / "config.yml", dict(FULL))
    partial = {"n_tiles_per_well_per_round_phenotype": None, "n_tiles_per_well_per_cycle_sbs": 368}
    runner.refresh_geometry(tmp_path, tmp_path / "config.yml", partial)
    assert _saved(tmp_path) == FULL
    runner.refresh_geometry(tmp_path, tmp_path / "config.yml", {**FULL, "n_plates": 2})
    assert _saved(tmp_path)["n_plates"] == 2


def test_empty_probe_writes_nothing(tmp_path):
    runner.refresh_geometry(tmp_path, tmp_path / "config.yml", {"n_plates": None})
    assert not (tmp_path / ".brieflow" / "geometry.json").exists()
