"""Tests for FD-exhaustion ([Errno 24] Too many open files) handling.

Two layers: brieflow_recover.py classifies it as recoverable_fd_limit (→ resume,
not escalate), and brieflow_run.py raises the nofile soft limit before launch so
the resume actually succeeds. Regression for jannu preprocess dying at 60% on
zarr FD exhaustion (2026-06-22).
"""

from __future__ import annotations

import importlib.util
import resource
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / fname)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def rec():
    return _load("brieflow_recover", "brieflow_recover.py")


@pytest.fixture(scope="module")
def run():
    return _load("brieflow_run", "brieflow_run.py")


_FD_LOG = (
    "Traceback (most recent call last):\n"
    "OSError: [Errno 24] Too many open files: "
    "'/x/.snakemake/metadata/...image_1.zarr/A/3/865/zarr.json'\n"
)


def test_fd_exhaustion_classified_recoverable(rec):
    cat, ev = rec.classify_failure(_FD_LOG, None)
    assert cat == "recoverable_fd_limit"
    assert "Too many open files" in ev


def test_emfile_classified_recoverable(rec):
    cat, _ = rec.classify_failure("OSError: EMFILE: too many open files", None)
    assert cat == "recoverable_fd_limit"


def test_oom_not_misclassified_as_fd(rec):
    """An OOM kill must still classify as recoverable_oom, not fd_limit."""
    cat, _ = rec.classify_failure(
        "slurmstepd: error: Detected 1 oom_kill event", None
    )
    assert cat == "recoverable_oom"


def test_unrelated_error_still_unknown(rec):
    cat, _ = rec.classify_failure("ValueError: something totally else", None)
    assert cat == "unknown"


def test_raise_nofile_idempotent_when_at_cap(run, monkeypatch):
    """When soft already == hard, the helper is a no-op (returns None)."""
    monkeypatch.setattr(resource, "getrlimit", lambda which: (4096, 4096))
    assert run._raise_nofile_limit() is None


def test_raise_nofile_raises_to_hard(run, monkeypatch):
    captured = {}
    monkeypatch.setattr(resource, "getrlimit", lambda which: (1024, 1048576))
    monkeypatch.setattr(resource, "setrlimit", lambda which, lim: captured.update(lim=lim))
    res = run._raise_nofile_limit()
    assert res == (1024, 1048576)
    assert captured["lim"] == (1048576, 1048576)
