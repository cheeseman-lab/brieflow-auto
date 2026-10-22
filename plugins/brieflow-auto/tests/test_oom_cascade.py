"""Tests for the OOM-cascade watcher in brieflow_monitor.py (HARDENING #1).

Direct-tests _check_oom_cascade with a mocked _oom_count_since so we don't
need a live slurm cluster. Also tests _terminate_subprocess_tree against a
real child process.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_monitor.py"


@pytest.fixture(scope="module")
def monitor():
    spec = importlib.util.spec_from_file_location("brieflow_monitor", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_monitor"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_check_oom_cascade_fires_above_threshold(monitor, monkeypatch):
    monkeypatch.setattr(
        monitor,
        "_oom_count_since",
        lambda start, user: {"align_phenotype": 150, "convert_sbs": 3},
    )
    ctx = monitor.MonitorCtx()
    ctx.attempt_started_ts = time.time() - 60
    ctx.oom_cascade_threshold = 100
    ctx.last_oom_poll_ts = 0
    ctx.oom_poll_interval_sec = 0  # poll every cycle
    monitor._check_oom_cascade(ctx, time.time())
    assert ctx.oom_cascade_detected is True
    assert ctx.oom_cascade_rule == "align_phenotype"
    assert ctx.oom_cascade_count == 150


def test_check_oom_cascade_below_threshold(monitor, monkeypatch):
    monkeypatch.setattr(
        monitor, "_oom_count_since", lambda start, user: {"align_phenotype": 50}
    )
    ctx = monitor.MonitorCtx()
    ctx.attempt_started_ts = time.time() - 60
    ctx.oom_cascade_threshold = 100
    ctx.last_oom_poll_ts = 0
    ctx.oom_poll_interval_sec = 0
    monitor._check_oom_cascade(ctx, time.time())
    assert ctx.oom_cascade_detected is False
    assert ctx.oom_cascade_rule is None


def test_check_oom_cascade_throttles_polls(monitor, monkeypatch):
    """If last_oom_poll_ts is recent, don't re-poll. Counter shouldn't change."""
    calls = []
    monkeypatch.setattr(
        monitor,
        "_oom_count_since",
        lambda start, user: (calls.append(1), {"r": 200})[1],
    )
    ctx = monitor.MonitorCtx()
    ctx.attempt_started_ts = time.time() - 60
    ctx.oom_cascade_threshold = 100
    ctx.last_oom_poll_ts = time.time()  # JUST polled
    ctx.oom_poll_interval_sec = 60.0
    monitor._check_oom_cascade(ctx, time.time())  # not due
    assert len(calls) == 0
    assert ctx.oom_cascade_detected is False


def test_check_oom_cascade_no_attempt_no_poll(monitor, monkeypatch):
    """attempt_started_ts == 0 → no poll (no attempt window yet)."""
    monkeypatch.setattr(monitor, "_oom_count_since", lambda *a, **k: {"r": 200})
    ctx = monitor.MonitorCtx()
    ctx.attempt_started_ts = 0
    ctx.oom_cascade_threshold = 100
    ctx.last_oom_poll_ts = 0
    ctx.oom_poll_interval_sec = 0
    monitor._check_oom_cascade(ctx, time.time())
    assert ctx.oom_cascade_detected is False


def test_terminate_subprocess_tree(monitor):
    """Spawn a sleeper child, terminate it via the helper, confirm it dies
    quickly (the point of the helper). Don't assert specific rc — Python's
    SIGTERM handling can produce different codes across platforms."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        assert proc.poll() is None
        t0 = time.time()
        monitor._terminate_subprocess_tree(proc.pid)
        proc.wait(timeout=10)  # raises TimeoutExpired on failure
        elapsed = time.time() - t0
        # Should die well within the 60s natural-exit window.
        assert elapsed < 10, f"took {elapsed}s"
        assert proc.poll() is not None  # confirmed dead
    finally:
        if proc.poll() is None:
            proc.kill()


def test_terminate_subprocess_tree_dead_pid_noop(monitor):
    """Terminating an already-dead PID should be a clean no-op."""
    monitor._terminate_subprocess_tree(1 << 30)  # nothing to crash on


def test_oom_count_since_filters_state(monitor, monkeypatch):
    """_oom_count_since should only count OUT_OF_MEMORY rows + parse rule from Comment."""
    fake_sacct_stdout = (
        "rule_align_phenotype_wildcards_plate=1,well=A1,tile=1|OUT_OF_MEMORY\n"
        "rule_align_phenotype_wildcards_plate=1,well=A1,tile=2|OUT_OF_MEMORY\n"
        "rule_convert_sbs_wildcards_plate=1,well=A1,tile=1|COMPLETED\n"
        "not_a_rule_comment|OUT_OF_MEMORY\n"
        "rule_merge_phenotype_wildcards_plate=1|FAILED\n"
    )

    class FakeProc:
        returncode = 0
        stdout = fake_sacct_stdout

    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *a, **kw: FakeProc(),
    )
    out = monitor._oom_count_since(time.time() - 60, "tester")
    assert out == {"align_phenotype": 2}


def test_oom_count_since_no_user_returns_empty(monitor):
    """An empty user → no probe (avoids `sacct -u ` which lists all users)."""
    assert monitor._oom_count_since(time.time(), "") == {}


def test_cascade_short_circuits_when_backend_unsupported(monitor, monkeypatch):
    """LocalBackend (sequential execution) → _check_oom_cascade is a no-op
    regardless of what _oom_count_since would return. The guard fires before
    the sacct/log poll, so even high counts in the mock don't trigger."""
    import importlib.util

    backend_script = (
        Path(__file__).resolve().parent.parent / "scripts" / "brieflow_backend.py"
    )
    spec = importlib.util.spec_from_file_location("brieflow_backend", backend_script)
    backend_mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_backend"] = backend_mod
    spec.loader.exec_module(backend_mod)

    # Even if the legacy _oom_count_since would return a cascading count,
    # the LocalBackend's supports_cascade_detection=False short-circuits.
    monkeypatch.setattr(
        monitor, "_oom_count_since", lambda start, user: {"any_rule": 9999}
    )
    ctx = monitor.MonitorCtx()
    ctx.adapter = backend_mod.LocalBackend()
    ctx.attempt_started_ts = time.time() - 60
    ctx.oom_cascade_threshold = 100
    ctx.last_oom_poll_ts = 0
    ctx.oom_poll_interval_sec = 0
    monitor._check_oom_cascade(ctx, time.time())
    assert ctx.oom_cascade_detected is False
    assert ctx.oom_cascade_rule is None
