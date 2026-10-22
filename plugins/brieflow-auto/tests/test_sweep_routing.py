"""Sweeps on a Slurm screen run through srun; the headless fallback is the cached one."""

import asyncio
import importlib.util
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import brieflow_sweep as bs  # noqa: E402

spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPTS / "brieflow_runner.py")
runner = importlib.util.module_from_spec(spec)
sys.modules.setdefault("brieflow_runner", runner)
spec.loader.exec_module(runner)

LAB = {"sweep_tiles": {"run_in_allocation": True, "max_parallel_kernels": 3,
                       "alloc_cpus_per_kernel": 4, "alloc_mem_gb_per_kernel": 24,
                       "alloc_time": "8:00:00"}}


def test_slurm_sweeps_are_wrapped_with_the_profile_partition(tmp_path, monkeypatch):
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    (tmp_path / "slurm").mkdir()
    (tmp_path / "slurm" / "config.yaml").write_text(
        "default-resources:\n  slurm_partition: '20'\n  slurm_account: lab\n")
    seen = {}

    def fake_in_allocation(name, resources, conda_env):
        seen[name] = resources
        return name

    monkeypatch.setattr(runner, "in_allocation", fake_in_allocation)
    fns = runner.sweep_functions(Namespace(backend="slurm"), tmp_path, LAB, "env", "sbs")
    assert fns["sweep_headless"] == "cached_sweep_headless"
    assert fns["adaptive_sweep_headless"] == "adaptive_cached_sweep"
    res = seen["joint_marimo_sweep_multitile"]
    assert res["partition"] == "20" and res["cpus"] == 12 and res["mem_gb"] == 72
    assert res["job_name"] == "brieflow_sweep_sbs"


@pytest.mark.parametrize("backend,job,lab", [("local", None, LAB), ("slurm", "77", LAB),
                                             ("slurm", None, {})])
def test_local_when_not_slurm_inside_a_job_or_switched_off(tmp_path, monkeypatch, backend, job,
                                                           lab):
    if job:
        monkeypatch.setenv("SLURM_JOB_ID", job)
    else:
        monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    fns = runner.sweep_functions(Namespace(backend=backend), tmp_path, lab, "env", "sbs")
    assert fns["sweep_headless"] is runner.sweep_headless
    assert runner.sweep_headless is bs.cached_sweep_headless


def test_cached_sweep_runs_one_process_per_tile(tmp_path, monkeypatch):
    nb = tmp_path / "2_sbs.py"
    nb.write_text("# === OPERATOR PARAMETERS ===\nTEST_PLATE = 1\nTEST_WELL = 'A1'\n"
                  "TEST_TILE = 1\nTHRESHOLD_READS = 1\n# === END OPERATOR PARAMETERS ===\n")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = Path(cmd[-1].split("--out ")[1].strip('"'))
        out.write_text("THRESHOLD_READS\tsingle_rate\tany_rate\tqc_pass\telapsed_s\n"
                       "100\t0.4\t0.6\tTrue\t21.0\n200\t0.5\t0.7\tTrue\t20.5\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    df = asyncio.run(bs.cached_sweep_headless(nb, "THRESHOLD_READS", [100, 200],
                                              [(1, "A1", 1), (1, "A2", 2)],
                                              work_dir=tmp_path / "w", conda_env="env"))
    assert len(calls) == 2  # one primed process per tile, not one per (value, tile)
    assert list(df["value"]) == [100, 200] and list(df["n_tiles"]) == [2, 2]
    assert df.attrs["evidence"][200]["finite"] == 2


def test_cached_sweep_timeouts_raise_sweep_timeout(tmp_path, monkeypatch):
    nb = tmp_path / "2_sbs.py"
    nb.write_text("TEST_PLATE = 1\nTEST_WELL = 'A1'\nTEST_TILE = 1\n")
    monkeypatch.setattr(bs, "patch_marker_block_value", lambda *a: None)

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(bs.subprocess, "run", slow)
    with pytest.raises(bs.SweepTimeout):
        asyncio.run(bs.cached_sweep_headless(nb, "X", [1], [(1, "A1", 1)],
                                             work_dir=tmp_path / "w", conda_env="env"))
