"""OOM-cascade → recover hand-off. The runloop's cascade monitor terminates
Snakemake as soon as N jobs of a rule go OUT_OF_MEMORY — before any `Error in
rule` block lands in the master log — so recover.py used to find "no rule
errors" and ESCALATE instead of bumping (an SBS run, combine_sbs_info @ 1600 MB,
2026-08-27). Now the runloop passes `--oom-rule` and recover treats it as a
peak-at-cap OOM at the rule's current recommendation.
"""

from __future__ import annotations

import argparse
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


def _analysis_dir(tmp_path: Path) -> Path:
    a = tmp_path / "analysis"
    (a / ".snakemake" / "log").mkdir(parents=True)
    (a / ".snakemake" / "log" / "2026-08-26T213049.snakemake.log").write_text(
        "Job 16497 has been submitted with SLURM jobid 7978782\nsbatch call: sbatch --parsable\n"
    )
    (a / "slurm").mkdir()
    (a / "slurm" / "mem_recommendations.json").write_text(json.dumps(
        {"combine_sbs_info": {"mem_mb_recommended": 1600, "tier": "well", "obs_source": "efficiency_aggregated"}}))
    return a


def test_recover_bumps_hinted_cascade_rule(tmp_path, monkeypatch, capsys):
    rec = _load("brieflow_recover")
    a = _analysis_dir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["brieflow_recover.py", "--analysis-dir", str(a), "--modules", "sbs",
                                      "--attempt", "1", "--max-attempts", "3", "--backend", "slurm",
                                      "--oom-rule", "combine_sbs_info"])
    captured = []
    monkeypatch.setattr(rec, "emit", lambda d: captured.append(d))
    rec.main()
    action = captured[-1]
    assert action["action"] == "resume", action
    recs = json.loads((a / "slurm" / "mem_recommendations.json").read_text())
    assert recs["combine_sbs_info"]["mem_mb_recommended"] >= 1600 * rec.OOM_REPEAT_BUMP_X
    assert "manual_override" in recs["combine_sbs_info"]["obs_source"]


def test_recover_still_escalates_without_hint(tmp_path, monkeypatch, capsys):
    rec = _load("brieflow_recover")
    a = _analysis_dir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["brieflow_recover.py", "--analysis-dir", str(a), "--modules", "sbs",
                                      "--attempt", "1", "--backend", "slurm"])
    captured = []
    monkeypatch.setattr(rec, "emit", lambda d: captured.append(d))
    rec.main()
    assert captured[-1]["action"] == "escalate" and "no rule errors parsed" in captured[-1]["reason"]


def test_runloop_passes_cascade_rule_to_recover():
    rl = _load("brieflow_runloop")
    args = argparse.Namespace(analysis_dir=Path("/x"), max_attempts=3, mem_recs=None, backend="slurm")
    cmd = rl.build_recover_cmd(args, ["sbs"], 1, oom_rule="combine_sbs_info")
    assert cmd[-2:] == ["--oom-rule", "combine_sbs_info"]
    assert "--oom-rule" not in rl.build_recover_cmd(args, ["sbs"], 1)
