"""Sweeps ran on the head node (timeouts, ~5 h of head-node CPU on one screen). On a Slurm
backend they now run inside an allocation through srun."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import brieflow_sweep_alloc as alloc  # noqa: E402

RES = {"job_name": "brieflow_sweep_sbs", "cpus": 12, "mem_gb": 96, "time": "4:00:00",
       "partition": "20", "account": "lab"}


def _local_srun(calls):
    """Stand-in for srun: record the flags, then run the job in this interpreter."""

    def run(cmd, **kw):
        calls.append(cmd)
        job = cmd[cmd.index("-lc") + 1].split()[-1].strip("'")
        return subprocess.run([sys.executable, str(SCRIPTS / "brieflow_sweep_alloc.py"), job],
                              capture_output=True, text=True)

    return run


def test_allocation_needed_only_for_slurm_outside_a_job(monkeypatch):
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    assert alloc.allocation_needed("slurm") and not alloc.allocation_needed("local")
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    assert not alloc.allocation_needed("slurm")


FAKE = """
import os
import pandas as pd


async def fake_sweep(nb_path, param, values, tiles, work_dir, conda_env, **kw):
    if not values:
        raise ValueError("empty band")
    df = pd.DataFrame({"value": values, "mean_single_rate": [0.1 * v for v in values]})
    df.attrs["evidence"] = {"node": os.environ.get("FAKE_NODE", "head")}
    return df
"""


def _fake(tmp_path, monkeypatch):
    (tmp_path / "fake_sweeps.py").write_text(FAKE)
    monkeypatch.syspath_prepend(str(tmp_path))


def test_sweep_runs_through_srun_and_returns_its_value(tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch)
    calls = []
    fn = alloc.in_allocation("fake_sweep", RES, None, run=_local_srun(calls), module="fake_sweeps")
    out = asyncio.run(fn("2_sbs.py", "THRESHOLD_READS", [1, 2], [(1, "A1", 1)],
                         work_dir=tmp_path / "w", conda_env=None))
    assert list(out["mean_single_rate"]) == [0.1, 0.2] and out.attrs["evidence"]
    flags = calls[0]
    assert flags[0] == "srun" and "--partition=20" in flags and "--account=lab" in flags
    assert "--cpus-per-task=12" in flags and "--mem=96G" in flags


def test_errors_inside_the_allocation_keep_their_type(tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch)
    fn = alloc.in_allocation("fake_sweep", RES, None, run=_local_srun([]), module="fake_sweeps")
    with pytest.raises(ValueError, match="empty band"):
        asyncio.run(fn("2_sbs.py", "X", [], [], work_dir=tmp_path / "w", conda_env=None))


def test_a_failed_allocation_says_so(tmp_path):
    fn = alloc.in_allocation(
        "fake_sweep", RES, None,
        run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "srun: error: invalid partition"),
    )
    with pytest.raises(RuntimeError, match="invalid partition"):
        asyncio.run(fn("2_sbs.py", "X", [1], [], work_dir=tmp_path, conda_env=None))
