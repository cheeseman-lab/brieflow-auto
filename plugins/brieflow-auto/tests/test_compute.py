"""Tests for brieflow_compute.py — backend/slurm-identity probe + profile writer."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_compute.py"


@pytest.fixture(scope="module")
def compute():
    spec = importlib.util.spec_from_file_location("brieflow_compute", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_compute"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_probe_local_when_no_sbatch(compute, monkeypatch):
    monkeypatch.setattr(compute, "_sbatch_available", lambda: False)
    assert compute.probe_compute() == {"backend": "local"}


def test_probe_slurm_surfaces_candidates(compute, monkeypatch):
    monkeypatch.setattr(compute, "_sbatch_available", lambda: True)
    monkeypatch.setattr(compute, "_probe_partitions", lambda: (["20", "u20"], "20"))
    monkeypatch.setattr(compute, "_probe_accounts", lambda: ["wibrusers"])
    rep = compute.probe_compute()
    assert rep["backend"] == "slurm"
    assert rep["partitions"] == ["20", "u20"]
    assert rep["partition_default"] == "20"
    assert rep["accounts"] == ["wibrusers"]


def test_probe_partitions_parses_default_marker(compute, monkeypatch):
    monkeypatch.setattr(compute, "_run", lambda cmd, timeout=10: "20*\nu20\nqgpu\n")
    partitions, default = compute._probe_partitions()
    assert partitions == ["20", "u20", "qgpu"]
    assert default == "20"


def test_probe_partitions_empty_when_sinfo_fails(compute, monkeypatch):
    monkeypatch.setattr(compute, "_run", lambda cmd, timeout=10: None)
    assert compute._probe_partitions() == ([], None)


def test_write_slurm_profile(compute, tmp_path):
    out = compute.write_base_slurm_profile(
        tmp_path, backend="slurm", partition="u20", account="wibrusers", jobs=100
    )
    assert out == tmp_path / "slurm" / "config.yaml"
    cfg = yaml.safe_load(out.read_text())
    assert cfg["default-resources"]["slurm_partition"] == "u20"
    assert cfg["default-resources"]["slurm_account"] == "wibrusers"
    assert cfg["default-resources"]["runtime"] == "1d"  # a full day, not a minute count
    assert cfg["jobs"] == 100
    assert "set-resources" not in cfg  # per-rule mem comes from resolve_resources


def test_write_local_writes_nothing(compute, tmp_path):
    out = compute.write_base_slurm_profile(tmp_path, backend="local")
    assert out is None
    assert not (tmp_path / "slurm" / "config.yaml").exists()


def test_write_slurm_without_partition_raises(compute, tmp_path):
    with pytest.raises(ValueError, match="partition"):
        compute.write_base_slurm_profile(tmp_path, backend="slurm", partition=None)


def test_write_unknown_backend_raises(compute, tmp_path):
    with pytest.raises(ValueError, match="unknown backend"):
        compute.write_base_slurm_profile(tmp_path, backend="pbs", partition="x")


def test_write_slurm_omits_account_when_absent(compute, tmp_path):
    out = compute.write_base_slurm_profile(tmp_path, backend="slurm", partition="u20")
    cfg = yaml.safe_load(out.read_text())
    assert "slurm_account" not in cfg["default-resources"]


def test_write_slurm_profile_no_output_in_slurm_extra(compute, tmp_path):
    """Regression guard: plugin-slurm >= 2.6.0 forbids --output in slurm_extra
    (the executor manages job logs). A generated profile must never emit it —
    doing so silently deadlocks submission (job dies in an un-awaited Future)."""
    out = compute.write_base_slurm_profile(
        tmp_path, backend="slurm", partition="u20", account="wibrusers"
    )
    text = out.read_text()
    assert "--output" not in text
    assert "slurm_extra" not in text
