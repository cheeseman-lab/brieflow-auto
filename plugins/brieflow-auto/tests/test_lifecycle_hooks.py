"""Tests for brieflow_hooks — RunLifecycleHooks for data movement.

Covers:
  - NoopHooks all methods are no-ops
  - GcsHooks uses default + env-overridden bucket names
  - gsutil rsync command structure (mocked subprocess)
  - on_pre_phase_1 raises on gsutil failure (must abort run)
  - on_post_terminal_phase skips when brieflow_output absent
  - on_failure swallows errors (must not raise)
  - get_hooks factory + unknown rejection
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_hooks.py"


@pytest.fixture(scope="module")
def hooks_mod():
    spec = importlib.util.spec_from_file_location("brieflow_hooks", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_hooks"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_returns_correct_hooks(hooks_mod):
    assert hooks_mod.get_hooks("noop").name == "noop"
    assert hooks_mod.get_hooks("gcs").name == "gcs"


def test_factory_rejects_unknown(hooks_mod):
    with pytest.raises(ValueError, match="unknown lifecycle hooks"):
        hooks_mod.get_hooks("s3")


# ---------------------------------------------------------------------------
# NoopHooks
# ---------------------------------------------------------------------------


def test_noop_all_methods_none(hooks_mod, tmp_path):
    n = hooks_mod.NoopHooks()
    assert n.on_pre_phase_1(tmp_path, "screen-x") is None
    assert n.on_post_terminal_phase(tmp_path, "screen-x") is None
    assert n.on_failure(tmp_path, "screen-x", None) is None
    assert n.on_teardown(tmp_path, "screen-x") is None


# ---------------------------------------------------------------------------
# GcsHooks — bucket names
# ---------------------------------------------------------------------------


def test_gcs_requires_buckets(hooks_mod, monkeypatch):
    """No defaults — operator must set both env vars."""
    monkeypatch.delenv("BRIEFLOW_BUCKET_INPUT", raising=False)
    monkeypatch.delenv("BRIEFLOW_BUCKET_OUTPUT", raising=False)
    import pytest
    with pytest.raises(RuntimeError, match="BRIEFLOW_BUCKET_INPUT"):
        hooks_mod.GcsHooks()


def test_gcs_env_buckets(hooks_mod, monkeypatch):
    """Both env vars resolve to the bucket names operator set."""
    monkeypatch.setenv("BRIEFLOW_BUCKET_INPUT", "my-input")
    monkeypatch.setenv("BRIEFLOW_BUCKET_OUTPUT", "my-output")
    g = hooks_mod.GcsHooks()
    assert g.input_bucket == "my-input"
    assert g.output_bucket == "my-output"


# ---------------------------------------------------------------------------
# GcsHooks — gsutil command structure
# ---------------------------------------------------------------------------


def test_gcs_pre_phase_1_runs_rsync(hooks_mod, tmp_path, monkeypatch):
    """on_pre_phase_1 invokes gsutil -m rsync -r <src> <dst> with correct paths."""
    captured = {}

    class FakeProc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return FakeProc()

    monkeypatch.setattr(hooks_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("BRIEFLOW_BUCKET_INPUT", "test-input")
    g = hooks_mod.GcsHooks()
    g.on_pre_phase_1(tmp_path, "screen-x")
    assert captured["cmd"][:3] == ["gsutil", "-m", "rsync"]
    assert "-r" in captured["cmd"]
    assert "gs://test-input/screen-x/" in captured["cmd"]
    assert (tmp_path / "raw").exists()


def test_gcs_pre_phase_1_raises_on_failure(hooks_mod, tmp_path, monkeypatch):
    """If gsutil errors, on_pre_phase_1 must raise so the runloop aborts."""

    def fake_run(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(hooks_mod.subprocess, "run", fake_run)
    g = hooks_mod.GcsHooks()
    with pytest.raises(subprocess.CalledProcessError):
        g.on_pre_phase_1(tmp_path, "screen-x")


def test_gcs_post_terminal_skips_if_no_output(hooks_mod, tmp_path, monkeypatch):
    """No brieflow_output/ to push → skip, don't error."""
    called = []
    monkeypatch.setattr(
        hooks_mod.subprocess,
        "run",
        lambda *a, **k: called.append(a)
        or (_ for _ in ()).throw(AssertionError("should not run")),
    )
    g = hooks_mod.GcsHooks()
    g.on_post_terminal_phase(tmp_path, "screen-x")  # no exception
    assert called == []


def test_gcs_post_terminal_pushes_output(hooks_mod, tmp_path, monkeypatch):
    """brieflow_output/ present → gsutil rsync to gs://output/<screen>/brieflow_output/."""
    (tmp_path / "brieflow_output").mkdir()
    captured = {}

    class FakeProc:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        hooks_mod.subprocess,
        "run",
        lambda cmd, **kw: (captured.setdefault("cmd", cmd), FakeProc())[1],
    )
    monkeypatch.setenv("BRIEFLOW_BUCKET_OUTPUT", "test-output")
    g = hooks_mod.GcsHooks()
    g.on_post_terminal_phase(tmp_path, "screen-x")
    assert "gs://test-output/screen-x/brieflow_output/" in captured["cmd"]


def test_gcs_on_failure_swallows_errors(hooks_mod, tmp_path, monkeypatch):
    """on_failure must NEVER raise — failure hook is called from runloop's
    exception handler, can't itself cause another failure."""

    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(hooks_mod.subprocess, "run", boom)
    # No diag bundle, no subdirs — exercise empty path
    g = hooks_mod.GcsHooks()
    g.on_failure(tmp_path, "screen-x", None)  # no raise

    # With a diag bundle present
    diag = tmp_path / "diag.tar.gz"
    diag.write_text("fake")
    (tmp_path / "logs").mkdir()
    g.on_failure(tmp_path, "screen-x", diag)  # still no raise


def test_gcs_on_teardown_noop(hooks_mod, tmp_path):
    """Teardown is no-op in sprint (VM teardown handles disk reclaim)."""
    g = hooks_mod.GcsHooks()
    assert g.on_teardown(tmp_path, "screen-x") is None


def test_gcs_handles_missing_gsutil(hooks_mod, tmp_path, monkeypatch):
    """If gsutil binary not installed, check=True path raises; check=False returns 127."""

    def fake_run(cmd, **kw):
        raise FileNotFoundError("gsutil")

    monkeypatch.setattr(hooks_mod.subprocess, "run", fake_run)
    g = hooks_mod.GcsHooks()
    # check=True (pre/post-phase) should raise
    with pytest.raises(FileNotFoundError):
        g._gsutil_rsync("gs://x/", str(tmp_path) + "/", recursive=True, check=True)
    # check=False (failure/teardown) should return 127
    assert (
        g._gsutil_rsync("gs://x/", str(tmp_path) + "/", recursive=True, check=False)
        == 127
    )
