"""Tests for _check_disk_space in brieflow_runloop (Phase 1.2).

Covers:
  - Below warn threshold → no signal
  - Between warn and abort → warn (used_pct returned, abort_signaled stays False)
  - At/above abort → abort_now=True, abort_signaled set
  - Below absolute free-GB floor → abort even when used-% is low
  - Throttle: within disk_check_interval_sec → no-op
  - Warn-repeat throttle: within disk_warn_repeat_sec → suppressed
  - OSError on disk_usage → graceful (False, 0.0)
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import time
from collections import namedtuple
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


FakeUsage = namedtuple("FakeUsage", ["total", "used", "free"])


def _patch_disk_usage(monkeypatch, runloop_mod, used_pct: float):
    """Patch shutil.disk_usage inside the runloop module's _check_disk_space."""
    total = 1_000_000_000
    used = int(total * used_pct)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda p: FakeUsage(total, used, total - used)
    )


def test_below_warn_no_signal(runloop, tmp_path, monkeypatch):
    _patch_disk_usage(monkeypatch, runloop, 0.50)
    ctx = runloop.MonitorCtx()
    ctx.disk_check_interval_sec = 0  # no throttle for test
    ctx.disk_abort_min_free_gb = None  # isolate the % bands from the abs free-GB floor
    abort, pct = runloop._check_disk_space(tmp_path, ctx, time.time())
    assert abort is False
    assert pct == 0.0  # below warn threshold returns 0 (caller doesn't act)
    assert ctx.disk_abort_signaled is False


def test_between_warn_and_abort(runloop, tmp_path, monkeypatch):
    _patch_disk_usage(monkeypatch, runloop, 0.992)  # warn (0.99) <= used < abort (0.995)
    ctx = runloop.MonitorCtx()
    ctx.disk_check_interval_sec = 0
    ctx.disk_warn_repeat_sec = 0  # no throttle for test
    ctx.disk_abort_min_free_gb = None  # isolate the % bands from the abs free-GB floor
    abort, pct = runloop._check_disk_space(tmp_path, ctx, time.time())
    assert abort is False
    assert pct == pytest.approx(0.992, abs=0.001)
    assert ctx.disk_abort_signaled is False


def test_at_abort_triggers(runloop, tmp_path, monkeypatch):
    _patch_disk_usage(monkeypatch, runloop, 0.996)  # used >= abort (0.995)
    ctx = runloop.MonitorCtx()
    ctx.disk_check_interval_sec = 0
    ctx.disk_abort_min_free_gb = None  # abort via the % path, not the abs floor
    abort, pct = runloop._check_disk_space(tmp_path, ctx, time.time())
    assert abort is True
    assert pct == pytest.approx(0.996, abs=0.001)
    assert ctx.disk_abort_signaled is True
    assert ctx.disk_abort_used_pct == pytest.approx(0.996, abs=0.001)


def test_abs_floor_triggers_abort(runloop, tmp_path, monkeypatch):
    """Below the absolute free-GB floor aborts even when the used-% is nowhere
    near the abort threshold (the fake disk is 1 GB total, so 50% used = 0.5 GB
    free, under the 10 GB floor)."""
    _patch_disk_usage(monkeypatch, runloop, 0.50)
    ctx = runloop.MonitorCtx()
    ctx.disk_check_interval_sec = 0
    ctx.disk_abort_min_free_gb = 10.0
    abort, _ = runloop._check_disk_space(tmp_path, ctx, time.time())
    assert abort is True
    assert ctx.disk_abort_signaled is True


def test_throttle_skips_recent_check(runloop, tmp_path, monkeypatch):
    """If last_disk_check_ts was recent, the function should no-op."""
    _patch_disk_usage(monkeypatch, runloop, 0.95)  # would normally abort
    ctx = runloop.MonitorCtx()
    ctx.disk_check_interval_sec = 60.0
    ctx.last_disk_check_ts = time.time()  # JUST checked
    abort, pct = runloop._check_disk_space(tmp_path, ctx, time.time() + 5)
    assert abort is False  # throttled out
    assert pct == 0.0
    assert ctx.disk_abort_signaled is False


def test_warn_repeat_throttled(runloop, tmp_path, monkeypatch):
    """Within disk_warn_repeat_sec, repeated warn-zone checks return pct=0
    so the caller doesn't re-notify the operator every cycle."""
    _patch_disk_usage(monkeypatch, runloop, 0.992)  # warn zone
    ctx = runloop.MonitorCtx()
    ctx.disk_check_interval_sec = 0
    ctx.disk_warn_repeat_sec = 600.0
    ctx.disk_abort_min_free_gb = None  # isolate the % bands from the abs free-GB floor
    # First check at warn threshold → fires
    _, pct1 = runloop._check_disk_space(tmp_path, ctx, time.time())
    assert pct1 == pytest.approx(0.992, abs=0.001)
    # Second check immediately after → throttled to 0
    _, pct2 = runloop._check_disk_space(tmp_path, ctx, time.time())
    assert pct2 == 0.0


def test_oserror_graceful(runloop, tmp_path, monkeypatch):
    """OSError from shutil.disk_usage → return (False, 0.0), don't crash."""

    def boom(p):
        raise OSError("disk gone")

    monkeypatch.setattr(shutil, "disk_usage", boom)
    ctx = runloop.MonitorCtx()
    ctx.disk_check_interval_sec = 0
    abort, pct = runloop._check_disk_space(tmp_path, ctx, time.time())
    assert abort is False
    assert pct == 0.0
    assert ctx.disk_abort_signaled is False


def test_ctx_default_thresholds(runloop):
    """MonitorCtx defaults: warn 0.99, abort 0.995, abs floor 10 GB free."""
    ctx = runloop.MonitorCtx()
    assert ctx.disk_warn_pct == 0.99
    assert ctx.disk_abort_pct == 0.995
    assert ctx.disk_abort_min_free_gb == 10.0
    assert ctx.disk_abort_signaled is False
