"""Tests for the --forcerun pass-through (HARDENING #7 layer 1).

Verifies the plugin-side wiring: runloop's build_run_cmd → brieflow_run.py's
run_flow both forward --forcerun. The flow.sh (bash) layer that consumes it
lives in the pipeline repo and is verified there, not in this plugin's suite.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


RUNLOOP_PY = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runloop.py"
RUN_PY = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_run.py"


@pytest.fixture(scope="module")
def runloop():
    spec = importlib.util.spec_from_file_location("brieflow_runloop", RUNLOOP_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runloop"] = mod
    spec.loader.exec_module(mod)
    return mod


def _args(**overrides) -> SimpleNamespace:
    """Build a SimpleNamespace with all the args build_run_cmd inspects."""
    base = dict(
        analysis_dir=Path("/tmp/fake"),
        backend="slurm",
        config="config/config.yml",
        cpus_per_task=1,
        array_limit=10,
        jobs=400,
        latency_wait=60,
        mem_recs=None,
        use_arrays=False,
        forcerun=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_runloop_build_run_cmd_no_forcerun(runloop):
    """No forcerun → no --forcerun flag in the assembled command."""
    args = _args()
    cmd = runloop.build_run_cmd(args, ["preprocess"], "tag1", "", 1)
    assert "--forcerun" not in cmd


def test_runloop_build_run_cmd_with_forcerun(runloop):
    """forcerun list → --forcerun is appended with each rule."""
    args = _args(forcerun=["format_merge", "eval_merge"])
    cmd = runloop.build_run_cmd(args, ["merge"], "tag1", "", 1)
    assert "--forcerun" in cmd
    i = cmd.index("--forcerun")
    assert cmd[i + 1] == "format_merge"
    assert cmd[i + 2] == "eval_merge"


def test_brieflow_run_help_lists_forcerun():
    """brieflow_run.py --help should mention --forcerun (smoke check)."""
    r = subprocess.run(
        [sys.executable, str(RUN_PY), "--help"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0
    assert "--forcerun" in r.stdout


def test_runloop_help_lists_forcerun():
    """brieflow_runloop.py --help should mention --forcerun."""
    r = subprocess.run(
        [sys.executable, str(RUNLOOP_PY), "--help"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0
    assert "--forcerun" in r.stdout
