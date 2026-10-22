"""Tests for the adaptive monitor cadence + hang detection (HARDENING #4 + #8).

Unit-tests the decision functions directly, without spinning up a full
runloop subprocess. We patch psutil-based probes with synthetic signals so
the assertions are deterministic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runloop.py"


@pytest.fixture(scope="module")
def runloop():
    spec = importlib.util.spec_from_file_location("brieflow_runloop", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runloop"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_burst_during_phase_start(runloop):
    """In the first BURST_GRACE_SEC after phase start → burst interval."""
    ctx = runloop.MonitorCtx()
    now = 1000.0
    ctx.phase_started_ts = now - 5  # 5s ago — within burst window
    sig = {"pid_alive": True, "proc_running": True, "cpu_busy": True, "log_grew": True}
    assert runloop.decide_interval(ctx, sig, now) == runloop.MonitorCtx.BURST_SEC


def test_burst_during_attempt_boundary(runloop):
    """Within BURST_GRACE_SEC after a new attempt → burst regardless of signals."""
    ctx = runloop.MonitorCtx()
    now = 1000.0
    ctx.phase_started_ts = now - 1000  # phase started long ago
    ctx.attempt_started_ts = now - 10  # but attempt just incremented
    sig = {
        "pid_alive": True,
        "proc_running": False,
        "cpu_busy": False,
        "log_grew": False,
    }
    assert runloop.decide_interval(ctx, sig, now) == runloop.MonitorCtx.BURST_SEC


def test_steady_when_active_mid_phase(runloop):
    """Past burst windows + any-signal active → steady interval."""
    ctx = runloop.MonitorCtx()
    now = 5000.0
    ctx.phase_started_ts = now - 600
    ctx.attempt_started_ts = now - 600
    sig = {
        "pid_alive": True,
        "proc_running": False,
        "cpu_busy": True,
        "log_grew": False,
    }
    assert runloop.decide_interval(ctx, sig, now) == runloop.MonitorCtx.STEADY_SEC


def test_idle_after_two_quiet_cycles(runloop):
    """≥2 quiet cycles + no current signal → idle interval."""
    ctx = runloop.MonitorCtx()
    ctx.quiet_cycles = 2
    now = 5000.0
    ctx.phase_started_ts = now - 600
    ctx.attempt_started_ts = now - 600
    sig = {
        "pid_alive": True,
        "proc_running": False,
        "cpu_busy": False,
        "log_grew": False,
    }
    assert runloop.decide_interval(ctx, sig, now) == runloop.MonitorCtx.IDLE_SEC


def test_steady_when_only_one_quiet_cycle(runloop):
    """1 quiet cycle is not enough to drop to idle — stay steady."""
    ctx = runloop.MonitorCtx()
    ctx.quiet_cycles = 1
    now = 5000.0
    ctx.phase_started_ts = now - 600
    ctx.attempt_started_ts = now - 600
    sig = {
        "pid_alive": True,
        "proc_running": False,
        "cpu_busy": False,
        "log_grew": False,
    }
    assert runloop.decide_interval(ctx, sig, now) == runloop.MonitorCtx.STEADY_SEC


def test_process_signals_for_self(runloop):
    """psutil probe against this test's own PID should return pid_alive."""
    import os

    sig = runloop._process_signals(os.getpid())
    assert sig["pid_alive"] is True
    assert sig["tree_size"] >= 1


def test_process_signals_for_none(runloop):
    """None PID → all signals False."""
    sig = runloop._process_signals(None)
    assert sig == {
        "pid_alive": False,
        "proc_running": False,
        "cpu_busy": False,
        "tree_size": 0,
        "plate": None,
    }


def test_process_signals_for_dead(runloop):
    """Probing a high (dead) PID returns all-False."""
    sig = runloop._process_signals(1 << 30)
    assert sig["pid_alive"] is False


def test_log_grew_returns_false_first_call(runloop, tmp_path: Path):
    """First call seeds last_log_size; can't know if it grew, so False."""
    snake_dir = tmp_path / ".snakemake" / "log"
    snake_dir.mkdir(parents=True)
    log = snake_dir / "test.snakemake.log"
    log.write_text("first content\n")
    ctx = runloop.MonitorCtx()
    assert runloop._log_grew(tmp_path, ctx) is False
    assert ctx.last_log_size > 0


def test_log_grew_detects_growth(runloop, tmp_path: Path):
    """Second call after writing more content should report True."""
    snake_dir = tmp_path / ".snakemake" / "log"
    snake_dir.mkdir(parents=True)
    log = snake_dir / "test.snakemake.log"
    log.write_text("first content\n")
    ctx = runloop.MonitorCtx()
    runloop._log_grew(tmp_path, ctx)  # prime
    with open(log, "a") as f:
        f.write("more content here\n")
    assert runloop._log_grew(tmp_path, ctx) is True


def test_log_grew_no_growth(runloop, tmp_path: Path):
    """Second call with no growth → False."""
    snake_dir = tmp_path / ".snakemake" / "log"
    snake_dir.mkdir(parents=True)
    log = snake_dir / "test.snakemake.log"
    log.write_text("first content\n")
    ctx = runloop.MonitorCtx()
    runloop._log_grew(tmp_path, ctx)  # prime
    assert runloop._log_grew(tmp_path, ctx) is False
