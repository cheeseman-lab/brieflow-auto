#!/usr/bin/env python3
"""brieflow_sweep_alloc.py — run a parameter sweep inside a Slurm allocation.

On a Slurm screen the runner orchestrates from the head node, and sweeps (live marimo
kernels: alignment, segmentation, read calling per tile) used to run there too, where they
were slow, timed out and competed with every other user. `in_allocation` wraps a sweep
function from `brieflow_sweep` so the whole call runs on a compute node through `srun`: the
arguments and the result travel as pickles in the sweep's work dir, and the kernels bind to
localhost on that node exactly as they do in a local run.

`python brieflow_sweep_alloc.py <job.pkl>` is the in-allocation entry point.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import pickle
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def allocation_needed(backend: str | None) -> bool:
    """A Slurm screen whose runner is not already inside an allocation."""
    return backend == "slurm" and not os.environ.get("SLURM_JOB_ID")


def srun_command(job_file: Path, resources: dict, conda_env: str | None) -> list[str]:
    """The srun call that runs `job_file` on a compute node."""
    flags = [
        "srun",
        f"--job-name={resources.get('job_name', 'brieflow_sweep')}",
        f"--cpus-per-task={int(resources['cpus'])}",
        f"--mem={int(resources['mem_gb'])}G",
        f"--time={resources['time']}",
    ]
    if resources.get("partition"):
        flags.append(f"--partition={resources['partition']}")
    if resources.get("account"):
        flags.append(f"--account={resources['account']}")
    inner = " ".join(shlex.quote(str(p)) for p in (sys.executable, Path(__file__), job_file))
    if conda_env:
        inner = (
            f'eval "$(conda shell.bash hook)" && conda activate {shlex.quote(conda_env)} && '
            f"exec python {shlex.quote(str(Path(__file__)))} {shlex.quote(str(job_file))}"
        )
    return flags + ["bash", "-lc", inner]


def in_allocation(
    fn_name: str,
    resources: dict,
    conda_env: str | None,
    run=subprocess.run,
    module: str = "brieflow_sweep",
):
    """Wrap `brieflow_sweep.<fn_name>` so each call runs through srun.

    The wrapper keeps the sweep's signature and async-ness; its `work_dir` keyword holds
    the job and result pickles. A failed allocation raises RuntimeError with srun's tail,
    and an exception inside the sweep is re-raised with its original type."""

    async def wrapped(*args, **kwargs):
        work_dir = Path(kwargs.get("work_dir") or ".").resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        job_file = work_dir / f"alloc_{fn_name}.job.pkl"
        result_file = job_file.with_suffix(".result.pkl")
        result_file.unlink(missing_ok=True)
        with open(job_file, "wb") as f:
            job = {"module": module, "fn": fn_name, "args": args, "kwargs": kwargs,
                   "result": str(result_file), "path": [str(SCRIPT_DIR), *sys.path]}
            pickle.dump(job, f)
        proc = run(srun_command(job_file, resources, conda_env), capture_output=True, text=True)
        if not result_file.exists():
            raise RuntimeError(
                f"sweep allocation failed (srun rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[-800:]}"
            )
        with open(result_file, "rb") as f:
            outcome = pickle.load(f)
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    wrapped.__name__ = f"{fn_name}_in_allocation"
    return wrapped


def main(job_file: str) -> int:
    import importlib

    with open(job_file, "rb") as f:
        job = pickle.load(f)
    sys.path[:0] = [p for p in job.get("path", [str(SCRIPT_DIR)]) if p not in sys.path]
    fn = getattr(importlib.import_module(job.get("module", "brieflow_sweep")), job["fn"])
    try:
        value = fn(*job["args"], **job["kwargs"])
        if inspect.isawaitable(value):
            value = asyncio.run(value)
        outcome = {"value": value}
    except Exception as e:  # handed back to the caller, which re-raises it
        outcome = {"error": e}
    with open(job["result"], "wb") as f:
        pickle.dump(outcome, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
