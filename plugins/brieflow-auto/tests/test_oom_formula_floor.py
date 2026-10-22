"""A rule OOM-killed at the 3000 MB default was bumped 4x to 12-13 GB when its true need was
~38 GB; the bump now never lands below the rule's formula estimate from geometry."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_bump_is_floored_at_the_formula_estimate():
    rec = _load("brieflow_recover")
    sacct = {"max_rss_mb": 2990.0, "req_mem_mb": 3000.0}
    assert rec.compute_oom_bump(sacct, None)[0] == 13000
    new_mem, reason = rec.compute_oom_bump(sacct, None, formula_floor=99_000)
    assert new_mem >= 99_000 and "formula estimate" in reason
    assert rec.compute_oom_bump(sacct, None, formula_floor=5_000)[0] == 13000


def test_recover_applies_the_floor_from_geometry(tmp_path, monkeypatch):
    rec = _load("brieflow_recover")
    a = tmp_path / "analysis"
    (a / ".snakemake" / "log").mkdir(parents=True)
    (a / ".snakemake" / "log" / "x.snakemake.log").write_text("sbatch call: sbatch -p u20\n")
    (a / "slurm").mkdir()
    (a / "slurm" / "mem_recommendations.json").write_text("{}")
    (a / ".brieflow").mkdir()
    (a / ".brieflow" / "geometry.json").write_text(
        json.dumps({"n_tiles_per_well_per_round_phenotype": 1460})
    )
    (a / "config").mkdir()
    (a / "config" / "config.yml").write_text("preprocess:\n  sample_fraction: 0.25\n")
    monkeypatch.setattr(sys, "argv", ["brieflow_recover.py", "--analysis-dir", str(a), "--modules",
                                      "preprocess", "--backend", "slurm",
                                      "--oom-rule", "calculate_ic_phenotype"])
    monkeypatch.setattr(rec, "emit", lambda d: None)
    rec.main()
    recs = json.loads((a / "slurm" / "mem_recommendations.json").read_text())
    assert recs["calculate_ic_phenotype"]["mem_mb_recommended"] >= 270 * 1460 * 0.5 + 2000
