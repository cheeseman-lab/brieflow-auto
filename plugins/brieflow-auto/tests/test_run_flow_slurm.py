"""Tests for brieflow_run.py's slurm wiring: the flow.sh arrays flag and the base-profile gate.

flow.sh has arrays off by default and takes `--arrays` to opt in (`--no-arrays` is still
accepted); the analysis template ships `slurm/config.yaml` with the partition commented out,
which must not count as an established base profile.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_run.py"


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("brieflow_run", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_run"] = mod
    spec.loader.exec_module(mod)
    return mod


def _flow_cmd(run, monkeypatch, tmp_path, use_arrays: bool) -> list[str]:
    captured = {}

    def fake_run(cmd, cwd=None, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    monkeypatch.setattr(run, "_raise_nofile_limit", lambda: None)
    run.run_flow(
        tmp_path, ["sbs"], "config/config.yml", "slurm", None,
        array_limit=10, use_arrays=use_arrays, latency_wait=5, cores="all", jobs=100,
    )
    return captured["cmd"]


def test_use_arrays_passes_arrays(run, monkeypatch, tmp_path):
    cmd = _flow_cmd(run, monkeypatch, tmp_path, use_arrays=True)
    assert "--arrays" in cmd
    assert "--no-arrays" not in cmd


def test_arrays_off_passes_no_arrays(run, monkeypatch, tmp_path):
    cmd = _flow_cmd(run, monkeypatch, tmp_path, use_arrays=False)
    assert "--no-arrays" in cmd
    assert "--arrays" not in cmd


def test_profile_missing_not_established(run, tmp_path):
    assert not run.base_profile_established(tmp_path / "slurm" / "config.yaml")


def test_template_profile_without_partition_not_established(run, tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "default-resources:\n"
        "    # slurm_partition: <partition>\n"
        "    # slurm_account: <account>\n"
        "    mem_mb: 3000\n"
        "jobs: 150\n"
    )
    assert not run.base_profile_established(p)


def test_profile_with_partition_established(run, tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("default-resources:\n    slurm_partition: u20\n    mem_mb: 3000\njobs: 100\n")
    assert run.base_profile_established(p)


def test_list_form_default_resources(run, tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("default-resources:\n  - slurm_partition=u20\n  - mem_mb=3000\n")
    assert run.base_profile_established(p)
    p.write_text("default-resources:\n  - mem_mb=3000\n")
    assert not run.base_profile_established(p)
