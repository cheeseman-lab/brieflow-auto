"""Tests for brieflow_driver_watchdog.py (HARDENING #24).

Exercises the components separately and via short end-to-end subprocess
runs against a synthetic analysis dir + a doomed "runloop" stand-in.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "brieflow_driver_watchdog.py"
)


@pytest.fixture(scope="module")
def watchdog_module():
    spec = importlib.util.spec_from_file_location("brieflow_driver_watchdog", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_driver_watchdog"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_pid_alive(watchdog_module):
    assert watchdog_module.pid_alive(os.getpid()) is True
    assert watchdog_module.pid_alive(1 << 30) is False
    assert watchdog_module.pid_alive(0) is False
    assert watchdog_module.pid_alive(-1) is False


def test_terminal_marker_seen_positive(watchdog_module, tmp_path: Path):
    tag = "test_terminal"
    state_dir = tmp_path / ".brieflow_runloop" / tag
    state_dir.mkdir(parents=True)
    (state_dir / "notify.log").write_text(
        "[2026-05-14] phase start\n"
        "[2026-05-14] SUCCESS attempt 1: ['preprocess'] in 5.3 min\n"
    )
    assert watchdog_module.terminal_marker_seen(tmp_path, tag) is True


def test_terminal_marker_seen_negative(watchdog_module, tmp_path: Path):
    tag = "test_no_terminal"
    state_dir = tmp_path / ".brieflow_runloop" / tag
    state_dir.mkdir(parents=True)
    (state_dir / "notify.log").write_text(
        "[2026-05-14] phase start\n[2026-05-14] attempt 1 starting\n"
    )
    assert watchdog_module.terminal_marker_seen(tmp_path, tag) is False


def test_terminal_marker_seen_missing_log(watchdog_module, tmp_path: Path):
    """No notify.log → no terminal marker."""
    assert watchdog_module.terminal_marker_seen(tmp_path, "ghost") is False


def test_write_diag_bundle_contents(watchdog_module, tmp_path: Path):
    tag = "diag_test"
    state_dir = tmp_path / ".brieflow_runloop" / tag
    state_dir.mkdir(parents=True)
    (state_dir / "notify.log").write_text("[2026-05-14] phase start\n")

    snake_log_dir = tmp_path / ".snakemake" / "log"
    snake_log_dir.mkdir(parents=True)
    snake_log = snake_log_dir / "20260514_120000.snakemake.log"
    snake_log.write_text("Started 1234 jobs\nFinished jobid: 12\n")

    bundle = watchdog_module.write_diag_bundle(
        tmp_path,
        tag,
        "runloop pid gone before terminal marker",
        bundle_kind="driver_death",
        runloop_pid=99999,
    )
    assert bundle.exists()
    assert bundle.parent.name == "diag"
    text = bundle.read_text()
    assert "tag: diag_test" in text
    assert "runloop_pid: 99999" in text
    assert "df -h" in text
    assert "dmesg" in text
    assert "ps aux" in text
    assert "notify.log" in text
    assert "snakemake.log" in text


def test_write_watchdog_liveness(watchdog_module, tmp_path: Path):
    p = watchdog_module.write_watchdog_liveness(tmp_path, "wlive", 12345)
    assert p.exists()
    payload = json.loads(p.read_text())
    assert payload["pid"] == os.getpid()
    assert payload["tag"] == "wlive"
    assert payload["runloop_pid"] == 12345
    assert payload["host"] == socket.gethostname()
    p.unlink()  # cleanup (atexit only fires on real process exit)


# ---------------------------------------------------------------------------
# End-to-end: launch the watchdog against a doomed sleeper
# ---------------------------------------------------------------------------


def test_watchdog_detects_dead_runloop(tmp_path: Path):
    """Spawn a 'runloop' that exits quickly; the watchdog should detect the
    death, write a diag bundle, and exit non-zero (no terminal marker)."""
    # Doomed-runloop stand-in: sleep 1s then exit. Use subprocess.Popen with
    # start_new_session so the watchdog has to poll for it, not inherit.
    doomed = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        start_new_session=True,
    )
    # Watchdog polls every 1s for fast test.
    wd = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--analysis-dir",
            str(tmp_path),
            "--runloop-pid",
            str(doomed.pid),
            "--tag",
            "doomed_test",
            "--poll-seconds",
            "1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Wait for doomed sleeper to exit, then for watchdog to react + exit.
    doomed.wait(timeout=10)
    rc = wd.wait(timeout=20)
    out, err = wd.communicate()

    # Watchdog should exit 1 = driver death detected, no auto-restart.
    assert rc == 1, f"unexpected exit code {rc} (stdout={out}, stderr={err})"

    # Diag bundle should exist.
    diags = list((tmp_path / "diag").glob("doomed_test_driver_death_*.txt"))
    assert len(diags) == 1, f"expected 1 diag bundle, got {diags}"

    # Notify log should contain the watchdog's action_needed message.
    notify_log = tmp_path / ".brieflow_runloop" / "doomed_test" / "notify.log"
    assert notify_log.exists()
    text = notify_log.read_text()
    assert "DRIVER DEATH" in text
    assert "[watchdog/action_needed]" in text


def test_watchdog_no_op_when_terminal_marker_present(tmp_path: Path):
    """If notify.log already has a terminal marker when the runloop ends,
    the watchdog should exit 0 (clean completion) — no diag bundle."""
    tag = "clean_test"
    state_dir = tmp_path / ".brieflow_runloop" / tag
    state_dir.mkdir(parents=True)
    (state_dir / "notify.log").write_text(
        "[2026-05-14] phase start\n[2026-05-14] SUCCESS attempt 1: clean\n"
    )

    doomed = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        start_new_session=True,
    )
    wd = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--analysis-dir",
            str(tmp_path),
            "--runloop-pid",
            str(doomed.pid),
            "--tag",
            tag,
            "--poll-seconds",
            "1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    doomed.wait(timeout=10)
    rc = wd.wait(timeout=20)
    assert rc == 0
    # No diag bundle.
    diag_dir = tmp_path / "diag"
    if diag_dir.exists():
        assert list(diag_dir.glob(f"{tag}*.txt")) == []
