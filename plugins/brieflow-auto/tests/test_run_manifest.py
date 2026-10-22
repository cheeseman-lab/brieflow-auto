"""run_manifest.json — version stamp for a run (spec §4.1). Never raises."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_manifest.py"
PLUGIN_JSON = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"


@pytest.fixture(scope="module")
def mf():
    spec = importlib.util.spec_from_file_location("brieflow_manifest", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_manifest"] = m
    spec.loader.exec_module(m)
    return m


def _args(**k):
    base = dict(
        backend="local",
        lifecycle_hooks="noop",
        screen_id=None,
        from_phase=None,
        through=None,
        only=None,
    )
    base.update(k)
    return argparse.Namespace(**base)


def test_writes_schema_and_plugin_version(mf, tmp_path):
    (tmp_path / "screen.yaml").write_text("name: t\n")
    p = mf.write_run_manifest(tmp_path, _args(), "run_x")
    d = json.loads(p.read_text())
    assert p == tmp_path / ".brieflow" / "run_manifest.json"
    assert d["schema"] == "brieflow-run-manifest/v1" and d["tag"] == "run_x"
    assert d["plugin"]["version"] == json.loads(PLUGIN_JSON.read_text())["version"]
    assert d["screen"]["screen_yaml_sha256"] and d["run"]["backend"] == "local"


def test_missing_git_and_versions_are_null_not_errors(mf, tmp_path, monkeypatch):
    monkeypatch.setattr(mf, "_git", lambda *a, **k: None)
    monkeypatch.setattr(mf, "_dist_version", lambda name: None)
    d = json.loads(mf.write_run_manifest(tmp_path, _args(), "t").read_text())
    assert d["screen"]["repo_sha"] is None and d["env"]["snakemake"] is None
    assert d["screen"]["screen_yaml_sha256"] is None


def test_read_roundtrip_and_absent(mf, tmp_path):
    assert mf.read_run_manifest(tmp_path) is None
    mf.write_run_manifest(tmp_path, _args(only="sbs"), "t")
    assert mf.read_run_manifest(tmp_path)["run"]["only"] == "sbs"


def test_env_overrides_recorded(mf, tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEFLOW_STOP_AFTER", "merge")
    d = json.loads(mf.write_run_manifest(tmp_path, _args(), "t").read_text())
    assert d["run"]["env_overrides"]["BRIEFLOW_STOP_AFTER"] == "merge"


def test_write_survives_unwritable_brieflow_dir(mf, tmp_path):
    brieflow_dir = tmp_path / ".brieflow"
    brieflow_dir.mkdir()
    mode = brieflow_dir.stat().st_mode
    brieflow_dir.chmod(0o500)
    try:
        p = mf.write_run_manifest(tmp_path, _args(), "t")
        assert p == brieflow_dir / "run_manifest.json"
    finally:
        brieflow_dir.chmod(mode)


def test_plugin_version_non_dict_json_returns_none(mf, tmp_path, monkeypatch):
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("[]")
    monkeypatch.setattr(mf, "SCRIPT_DIR", tmp_path / "scripts")
    assert mf.plugin_version() is None


def test_git_on_non_repo_dir_returns_none(mf, tmp_path):
    assert mf._git(tmp_path, "rev-parse", "HEAD") is None


def test_git_missing_binary_returns_none(mf, tmp_path, monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError("no git")

    monkeypatch.setattr(mf.subprocess, "run", _raise)
    assert mf._git(tmp_path, "rev-parse", "HEAD") is None
