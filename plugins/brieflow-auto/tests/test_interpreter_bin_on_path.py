"""brieflow_run / brieflow_dryrun_preview must find `snakemake` even when the
screen env was never `conda activate`d — the VM orchestrator launches the
runner by the env's absolute python (PATH lacks env/bin) and brieflow_run
died with FileNotFoundError: 'snakemake' (2026-08-27)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", ["brieflow_run", "brieflow_dryrun_preview"])
def test_interpreter_bin_prepended_once(name, monkeypatch):
    mod = _load(name)
    monkeypatch.setenv("PATH", "/usr/bin")
    mod._prepend_interpreter_bin_to_path()
    bindir = str(Path(sys.executable).resolve().parent)
    assert os.environ["PATH"].split(os.pathsep)[0] == bindir
    mod._prepend_interpreter_bin_to_path()
    assert os.environ["PATH"].split(os.pathsep).count(bindir) == 1
