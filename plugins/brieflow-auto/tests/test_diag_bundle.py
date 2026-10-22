"""Tests for brieflow_diag shared bundle helpers (HARDENING #14)."""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_diag.py"


@pytest.fixture(scope="module")
def diag():
    spec = importlib.util.spec_from_file_location("brieflow_diag", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_diag"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# capture_cmd / tail_file / latest_snakemake_log
# ---------------------------------------------------------------------------


def test_capture_cmd_normal(diag):
    out = diag.capture_cmd(["echo", "hello world"])
    assert "hello world" in out


def test_capture_cmd_nonexistent(diag):
    """Missing command returns error-prefixed string, doesn't raise."""
    out = diag.capture_cmd(["this_cmd_does_not_exist_xyzzy"])
    assert "error running" in out


def test_capture_cmd_timeout(diag):
    """Timeout returns error-prefixed string, doesn't raise."""
    out = diag.capture_cmd(["sleep", "30"], timeout=0.2)
    assert "error running" in out


def test_tail_file(diag, tmp_path: Path):
    p = tmp_path / "a.log"
    p.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    out = diag.tail_file(p, n=5)
    lines = out.splitlines()
    assert lines == [f"line {i}" for i in range(95, 100)]


def test_tail_file_missing(diag, tmp_path: Path):
    out = diag.tail_file(tmp_path / "nope.log", n=5)
    assert "error reading" in out


def test_latest_snakemake_log_none(diag, tmp_path: Path):
    assert diag.latest_snakemake_log(tmp_path) is None


def test_latest_snakemake_log_picks_newest(diag, tmp_path: Path):
    import time

    d = tmp_path / ".snakemake" / "log"
    d.mkdir(parents=True)
    old = d / "20260101_000000.snakemake.log"
    new = d / "20260514_120000.snakemake.log"
    old.write_text("old")
    time.sleep(0.01)
    new.write_text("new")
    assert diag.latest_snakemake_log(tmp_path) == new


# ---------------------------------------------------------------------------
# write_diag_bundle
# ---------------------------------------------------------------------------


def test_write_diag_bundle_basic(diag, tmp_path: Path):
    bundle = diag.write_diag_bundle(tmp_path, "test_tag", "some reason")
    assert bundle.exists()
    assert bundle.parent.name == "diag"
    text = bundle.read_text()
    assert "tag: test_tag" in text
    assert "reason: some reason" in text
    assert "df -h" in text
    assert "dmesg" in text
    assert "ps aux" in text
    assert socket.gethostname() in text


def test_write_diag_bundle_with_kind(diag, tmp_path: Path):
    bundle = diag.write_diag_bundle(
        tmp_path,
        "t",
        "r",
        bundle_kind="escalate",
    )
    assert "_escalate_" in bundle.name


def test_write_diag_bundle_with_pid(diag, tmp_path: Path):
    bundle = diag.write_diag_bundle(
        tmp_path,
        "t",
        "r",
        runloop_pid=42,
    )
    assert "runloop_pid: 42" in bundle.read_text()


def test_write_diag_bundle_includes_notify_log(diag, tmp_path: Path):
    tag = "with_notify"
    sd = tmp_path / ".brieflow_runloop" / tag
    sd.mkdir(parents=True)
    (sd / "notify.log").write_text("[2026-05-14] phase start\nLINE 2\n")
    bundle = diag.write_diag_bundle(tmp_path, tag, "r")
    text = bundle.read_text()
    assert "phase start" in text
    assert "LINE 2" in text


def test_write_diag_bundle_includes_snakemake_log(diag, tmp_path: Path):
    d = tmp_path / ".snakemake" / "log"
    d.mkdir(parents=True)
    (d / "20260514_120000.snakemake.log").write_text(
        "Started 1234 jobs\nFinished jobid: 12\n"
    )
    bundle = diag.write_diag_bundle(tmp_path, "t", "r")
    text = bundle.read_text()
    assert "snakemake log tail" in text
    assert "Finished jobid: 12" in text


def test_write_diag_bundle_extra_sections(diag, tmp_path: Path):
    extra = [
        ("recover.py action JSON", '{"action": "escalate"}'),
        ("sacct excerpt", "JobID|State\n123|FAILED"),
    ]
    bundle = diag.write_diag_bundle(tmp_path, "t", "r", extra_sections=extra)
    text = bundle.read_text()
    assert "recover.py action JSON" in text
    assert '"action": "escalate"' in text
    assert "sacct excerpt" in text
    assert "123|FAILED" in text


def test_write_diag_bundle_filename_format(diag, tmp_path: Path):
    """Filename pattern: <tag>_<kind>_<YYYYMMDD>_<HHMMSS>.txt"""
    bundle = diag.write_diag_bundle(tmp_path, "mytag", "r", bundle_kind="diag")
    assert bundle.name.startswith("mytag_diag_")
    assert bundle.name.endswith(".txt")


def test_write_diag_bundle_does_not_crash_when_no_logs(diag, tmp_path: Path):
    """Bundle should still succeed when there's no notify.log or snakemake log."""
    bundle = diag.write_diag_bundle(tmp_path, "lonely_tag", "no context")
    assert bundle.exists()
    text = bundle.read_text()
    # Always-present sections:
    assert "HEADER" in text
    assert "df -h" in text
