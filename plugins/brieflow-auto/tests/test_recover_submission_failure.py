"""Every sbatch rejected (a numeric partition summed across a job group to `-p 60`): recover
escalated only after hours of per-error log searches. It now names the submission failure."""

from __future__ import annotations

import importlib.util
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


def _error_block(rule, jobid, tile):
    return (
        f"    Error in rule {rule}:\n        message: None\n        jobid: {jobid}\n"
        f"        input: in/{tile}.zarr\n        output: out/{rule}/{tile}.tsv\n"
    )


def _analysis(tmp_path, submitted=False, n_tiles=60):
    a = tmp_path / "analysis"
    (a / ".snakemake" / "log").mkdir(parents=True)
    (a / "slurm").mkdir()
    (a / "slurm" / "config.yaml").write_text("default-resources:\n    slurm_partition: 20\n")
    calls = "sbatch call: sbatch --parsable --job-name brieflow_x -A acct  -p 60 -t 10080\n" * 3
    done = "Job 7 has been submitted with SLURM jobid 123 (log: x.log).\n" if submitted else ""
    blocks = "".join(_error_block("call_reads", i, i) for i in range(n_tiles))
    log = calls + done + "Error in group sbs_tile_group\n" + blocks
    (a / ".snakemake" / "log" / "2026-09-28T185634.snakemake.log").write_text(log)
    return a


def _run(rec, a, monkeypatch, searches):
    monkeypatch.setattr(sys, "argv", ["brieflow_recover.py", "--analysis-dir", str(a),
                                      "--modules", "sbs", "--backend", "slurm"])
    captured = []
    monkeypatch.setattr(rec, "emit", lambda d: captured.append(d))
    backend = rec.get_backend("slurm")
    monkeypatch.setattr(rec, "get_backend", lambda name: backend)
    monkeypatch.setattr(backend, "collect_rule_logs",
                        lambda *args: searches.append(args) or [])
    monkeypatch.setattr(backend, "query_failed_jobs", lambda *args, **kw: {})
    rec.main()
    return captured[-1]


def test_rejected_submissions_escalate_at_once_with_partition_hint(tmp_path, monkeypatch):
    rec = _load("brieflow_recover")
    searches = []
    action = _run(rec, _analysis(tmp_path), monkeypatch, searches)
    assert action["action"] == "escalate"
    assert action["classification"] == "submission_failure"
    assert action["submission"] == {"sbatch_calls": 3, "submitted": 0, "partitions": ["60"]}
    assert "'20'" in action["reason"] and "quote slurm_partition" in action["reason"]
    assert searches == []


def test_mass_failure_classifies_a_bounded_sample_per_rule(tmp_path, monkeypatch):
    rec = _load("brieflow_recover")
    searches = []
    action = _run(rec, _analysis(tmp_path, submitted=True), monkeypatch, searches)
    assert len(searches) == rec.MAX_DIAGNOSED_PER_RULE
    assert action["action"] == "escalate"
