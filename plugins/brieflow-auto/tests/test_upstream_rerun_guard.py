"""Upstream-rerun guard: abort ONLY on an actual `upstream_rerun` classification.
2026-08-27: the dry-run preview crashed (no `snakemake` on the runner's
PATH → FileNotFoundError, rc=1, no [JSON] line) and the guard read the exit
code as "upstream rerun", refusing to submit a healthy fresh SBS phase. Now a
missing classification is lenient-with-reason, `full_restart` (a fresh 60-well
SBS is 14k jobs) does not abort, and the dry-run runs inside the screen env.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
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


def _fake(rc, stdout="", stderr=""):
    def _run(cmd, **kw):
        _run.cmd = cmd
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)
    return _run


def test_guard_no_classification_is_lenient_with_reason(monkeypatch):
    ap = _load("brieflow_runner")
    monkeypatch.setattr(ap.subprocess, "run", _fake(1, stderr="FileNotFoundError: [Errno 2] No such file or directory: 'snakemake'"))
    abort, detail = ap.run_upstream_rerun_guard("/x", "sbs", conda_env="brieflow_examplescreen")
    assert abort is False and "no classification" in detail and "snakemake" in detail
    assert "--conda-env" in ap.subprocess.run.cmd and "brieflow_examplescreen" in ap.subprocess.run.cmd


def test_guard_aborts_only_on_upstream_rerun(monkeypatch):
    ap = _load("brieflow_runner")
    up = "[JSON] " + json.dumps({"classification": "upstream_rerun", "detail": "1 pending rule(s) belong to phases BEFORE sbs: ['convert_sbs']"})
    monkeypatch.setattr(ap.subprocess, "run", _fake(1, stdout=up))
    assert ap.run_upstream_rerun_guard("/x", "sbs") == (True, "1 pending rule(s) belong to phases BEFORE sbs: ['convert_sbs']")
    full = "[JSON] " + json.dumps({"classification": "full_restart", "detail": "total 14043 jobs ≥ threshold 500"})
    monkeypatch.setattr(ap.subprocess, "run", _fake(0, stdout=full))
    abort, _ = ap.run_upstream_rerun_guard("/x", "sbs")
    assert abort is False


def test_dryrun_wraps_snakemake_in_conda_env(monkeypatch, tmp_path):
    dr = _load("brieflow_dryrun_preview")
    monkeypatch.setattr(dr.subprocess, "run", _fake(0, stdout="Nothing to be done"))
    dr.run_snakemake_dryrun(tmp_path, tmp_path / "c.yml", tmp_path / "bf", "sbs", conda_env="brieflow_x")
    cmd = dr.subprocess.run.cmd
    assert cmd[:2] == ["bash", "-lc"] and "conda activate brieflow_x" in cmd[2] and "snakemake --dry-run" in cmd[2]
    dr.run_snakemake_dryrun(tmp_path, tmp_path / "c.yml", tmp_path / "bf", "sbs")
    assert dr.subprocess.run.cmd[0] == "snakemake"
