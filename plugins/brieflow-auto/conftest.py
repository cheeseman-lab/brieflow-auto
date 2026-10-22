"""Shared pytest harness for the brieflow-auto plugin.

Loaded at the rootdir before collection, so the setup here applies to the whole
suite regardless of how pytest is invoked (bare `pytest`, `make test`, CI, or a
single-file run from an editor).

Provides three things:
  1. `scripts/` on sys.path — belt-and-suspenders with `[tool.pytest.ini_options]
     pythonpath` in pyproject.toml, so `import brieflow_qc` resolves in-tree
     without an editable install.
  2. Default GCS bucket env vars — a few modules read BRIEFLOW_BUCKET_* at import
     time; defaulting them here lets `pytest` run without the Makefile's env
     prefix. `setdefault` keeps any real values (e.g. from `make test`) intact.
  3. `load_script` fixture — the reusable module loader that replaces the
     per-file `importlib.util.spec_from_file_location(...)` boilerplate. New
     tests can instead just `import brieflow_qc` directly (see (1)).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

os.environ.setdefault("BRIEFLOW_BUCKET_OUTPUT", "gs://o")
os.environ.setdefault("BRIEFLOW_BUCKET_INPUT", "gs://i")

# Set before collection: subprocess watchdogs inherit this too. Never consult an
# operator's real Slack configuration during tests. Delivery tests explicitly opt
# into normal mode with a fake curl executable and an empty configuration path.
os.environ["BRIEFLOW_NOTIFY_MODE"] = "disabled"
os.environ["BRIEFLOW_NOTIFY_ENV_FILE"] = ""
os.environ.pop("BRIEFLOW_SLACK_WEBHOOK", None)


@pytest.fixture(scope="session")
def load_script():
    """Return `load(name)` -> the `scripts/<name>.py` module, loaded once.

    Registers the module in sys.modules under its bare name so its sibling
    imports (`import brieflow_recover`) resolve, and caches per session.
    """
    cache: dict[str, object] = {}

    def _load(name: str):
        if name in cache:
            return cache[name]
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        cache[name] = mod
        return mod

    return _load
