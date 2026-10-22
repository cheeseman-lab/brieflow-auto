"""Tests for brieflow_smoketest.py (HARDENING #16)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_smoketest.py"


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("brieflow_smoketest", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_smoketest"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_probe_imports_pass_path(smoke):
    """At least one common package should be importable in the test env."""
    results = smoke.probe_imports()
    pkgs = {r.name for r in results}
    assert any(r.name.startswith("import ") for r in results)
    assert "import pandas" in pkgs or "import numpy" in pkgs


def test_probe_imports_missing_pkg(smoke, monkeypatch):
    """Adding a bogus package to the REQUIRED list should produce a FAIL row."""
    bogus = "this_pkg_does_not_exist_xyzzy"
    monkeypatch.setattr(smoke, "REQUIRED_PACKAGES", [bogus])
    results = smoke.probe_imports()
    assert len(results) == 1
    assert results[0].ok is False
    assert bogus in results[0].name


def test_probe_brieflow_present_yes(smoke, tmp_path: Path):
    bf = tmp_path / "brieflow"
    (bf / "workflow").mkdir(parents=True)
    (bf / "workflow" / "Snakefile").write_text("# fake")
    results = smoke.probe_brieflow_present(bf)
    assert len(results) == 1
    assert results[0].ok is True


def test_probe_brieflow_present_missing(smoke, tmp_path: Path):
    results = smoke.probe_brieflow_present(tmp_path / "nope")
    assert len(results) == 1
    assert results[0].ok is False


def test_probe_brieflow_present_no_snakefile(smoke, tmp_path: Path):
    bf = tmp_path / "brieflow"
    bf.mkdir()
    results = smoke.probe_brieflow_present(bf)
    assert results[0].ok is False


def test_probe_config_yaml_parses(smoke, tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text("all:\n  scope: test\npreprocess:\n  k: 1\n")
    results = smoke.probe_config(cfg)
    parses = [r for r in results if r.name == "config YAML parses"]
    assert parses and parses[0].ok is True
    req = [r for r in results if "required config top-level keys" in r.name]
    assert req and req[0].ok is True


def test_probe_config_missing_top_level_key(smoke, tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text("only_one:\n  k: 1\n")
    results = smoke.probe_config(cfg)
    req = [r for r in results if "required config top-level keys" in r.name]
    assert req and req[0].ok is False
    assert "missing" in req[0].detail


def test_probe_config_missing_file(smoke, tmp_path: Path):
    results = smoke.probe_config(tmp_path / "nope.yml")
    assert len(results) == 1
    assert results[0].ok is False


def test_probe_config_malformed_yaml(smoke, tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text("all:\n  : invalid\n  - mix list and map\n")
    results = smoke.probe_config(cfg)
    assert any(r.ok is False for r in results)


def test_probe_backend_local_skips(smoke):
    results = smoke.probe_backend("local")
    assert len(results) == 1
    assert results[0].ok is True
    assert "skipped" in results[0].detail


# ---------------------------------------------------------------------------
# End-to-end: invoke the script in --json mode
# ---------------------------------------------------------------------------


def test_e2e_json_output(tmp_path: Path):
    """Smoke the full pipeline end-to-end against a synthetic analysis dir."""
    analysis = tmp_path / "analysis"
    (analysis / "config").mkdir(parents=True)
    (analysis / "config" / "config.yml").write_text(
        "all:\n  x: 1\npreprocess:\n  y: 2\n"
    )
    bf = tmp_path / "brieflow"
    (bf / "workflow").mkdir(parents=True)
    (bf / "workflow" / "Snakefile").write_text("# fake")

    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--analysis-dir",
            str(analysis),
            "--brieflow-dir",
            str(bf),
            "--backend",
            "local",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # The bundled packages (polars/snakemake/etc.) should be importable in
    # test env. If they're not, we still want valid JSON.
    payload = json.loads(r.stdout)
    assert "results" in payload
    assert "elapsed_s" in payload
    assert payload["n_passed"] + payload["n_failed"] == len(payload["results"])
