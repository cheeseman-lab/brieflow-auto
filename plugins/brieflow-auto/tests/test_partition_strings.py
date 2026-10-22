"""A numeric slurm_partition is summed across a Snakemake job group (20 x 3 -> `-p 60`), so the
profile the runner hands to flow.sh must carry partition and account as strings."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("brieflow_run", SCRIPTS / "brieflow_run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_run"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_generated_profile_quotes_numeric_partition_and_account(run, tmp_path):
    base = tmp_path / "config.yaml"
    base.write_text("default-resources:\n    slurm_partition: 20\n    slurm_account: 7\n"
                    "    mem_mb: 3000\n")
    out = run.write_slurm_profile(base, tmp_path / "profile")
    res = yaml.safe_load((out / "config.yaml").read_text())["default-resources"]
    assert res["slurm_partition"] == "20" and res["slurm_account"] == "7"
    assert res["mem_mb"] == 3000


def test_compute_writes_quoted_partition(tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    import brieflow_compute

    out = brieflow_compute.write_base_slurm_profile(
        tmp_path, backend="slurm", partition="20", account="42"
    )
    res = yaml.safe_load(out.read_text())["default-resources"]
    assert res["slurm_partition"] == "20" and res["slurm_account"] == "42"


def test_flow_gets_the_running_plugin_dir(run, monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, cwd=None, env=None, **kw):
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.delenv("BRIEFLOW_OPS_PLUGIN_DIR", raising=False)
    monkeypatch.setattr(run.subprocess, "run", fake_run)
    monkeypatch.setattr(run, "_raise_nofile_limit", lambda: None)
    run.run_flow(tmp_path, ["sbs"], "config/config.yml", "slurm", None, array_limit=10,
                 use_arrays=False, latency_wait=5, cores="all", jobs=100)
    plugin = Path(captured["env"]["BRIEFLOW_OPS_PLUGIN_DIR"])
    assert (plugin / "scripts" / "brieflow_resolve_resources.py").is_file()
