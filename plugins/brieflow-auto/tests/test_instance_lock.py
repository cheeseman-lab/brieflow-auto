"""Tests for the single-instance lock in brieflow_runloop.py (HARDENING #17).

Direct unit tests against `acquire_instance_lock` — avoids spinning up a
full runloop subprocess (which would require a real analysis dir + snakemake).
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runloop.py"


@pytest.fixture(scope="module")
def runloop_module():
    """Load brieflow_runloop.py as a module without invoking main()."""
    spec = importlib.util.spec_from_file_location("brieflow_runloop", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runloop"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_first_acquire_writes_payload(tmp_path: Path, runloop_module):
    """First call writes pid/host/tag/started into the lock file."""
    lock = runloop_module.acquire_instance_lock(tmp_path, "test_tag_1")
    assert lock == tmp_path / ".brieflow_runloop" / "instance.lock"
    assert lock.exists()

    with open(lock) as f:
        payload = json.load(f)
    assert payload["pid"] == os.getpid()
    assert payload["host"] == socket.gethostname()
    assert payload["tag"] == "test_tag_1"
    assert "started" in payload

    lock.unlink()  # cleanup; atexit only fires on real process exit


def test_live_pid_blocks_relaunch(tmp_path: Path, runloop_module):
    """A lock naming a live PID on this host refuses a second acquire."""
    lock = tmp_path / ".brieflow_runloop" / "instance.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:
        json.dump(
            {
                "pid": os.getpid(),  # self — guaranteed live
                "host": socket.gethostname(),
                "tag": "alive_other",
                "started": "2026-05-14T00:00:00",
            },
            f,
        )

    with pytest.raises(SystemExit) as exc:
        runloop_module.acquire_instance_lock(tmp_path, "second_attempt")
    assert exc.value.code == 1

    # Lock unchanged.
    with open(lock) as f:
        payload = json.load(f)
    assert payload["tag"] == "alive_other"
    lock.unlink()


def test_stale_lock_reclaimed(tmp_path: Path, runloop_module, capsys):
    """A lock naming a dead PID on this host is reclaimed with a warning."""
    lock = tmp_path / ".brieflow_runloop" / "instance.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    # PID 2^30 is high enough to be ~certain dead on any sane Linux system.
    dead_pid = 1 << 30
    with open(lock, "w") as f:
        json.dump(
            {
                "pid": dead_pid,
                "host": socket.gethostname(),
                "tag": "stale_run",
                "started": "2026-05-13T00:00:00",
            },
            f,
        )

    new_lock = runloop_module.acquire_instance_lock(tmp_path, "fresh_run")
    captured = capsys.readouterr()
    assert "stale brieflow_runloop lock" in captured.err
    assert "reclaiming" in captured.err

    with open(new_lock) as f:
        payload = json.load(f)
    assert payload["tag"] == "fresh_run"
    assert payload["pid"] == os.getpid()
    new_lock.unlink()


def test_foreign_host_lock_reclaimed(tmp_path: Path, runloop_module, capsys):
    """A lock with a foreign host name is reclaimed (we can't probe it)."""
    lock = tmp_path / ".brieflow_runloop" / "instance.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:
        json.dump(
            {
                "pid": 12345,
                "host": "not-this-machine-12345",
                "tag": "old_host_run",
                "started": "2026-05-13T00:00:00",
            },
            f,
        )

    new_lock = runloop_module.acquire_instance_lock(tmp_path, "this_host_run")
    captured = capsys.readouterr()
    assert "foreign host" in captured.err

    with open(new_lock) as f:
        payload = json.load(f)
    assert payload["tag"] == "this_host_run"
    new_lock.unlink()


def test_pid_alive_helper(runloop_module):
    """_pid_alive returns True for self, False for clearly-dead PID, False for 0."""
    assert runloop_module._pid_alive(os.getpid()) is True
    assert runloop_module._pid_alive(1 << 30) is False
    assert runloop_module._pid_alive(0) is False
    assert runloop_module._pid_alive(-1) is False
