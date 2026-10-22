"""Tests for brieflow_recover.classify_failure (HARDENING #13).

Tightened classification: only positive OOM signals trigger recoverable_oom.
A Python ValueError with MaxRSS far below cap must NOT be misclassified.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_recover.py"


@pytest.fixture(scope="module")
def recover():
    spec = importlib.util.spec_from_file_location("brieflow_recover", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_recover"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_oom_sacct_state(recover):
    sacct = {
        "state": "OUT_OF_MEMORY",
        "exit_code": "0:125",
        "max_rss_mb": 19000,
        "req_mem_mb": 19000,
        "comment": "",
    }
    cat, ev = recover.classify_failure("", sacct)
    assert cat == "recoverable_oom"
    assert "OUT_OF_MEMORY" in ev


def test_oom_exit_137(recover):
    sacct = {
        "state": "FAILED",
        "exit_code": "137:0",
        "max_rss_mb": 100,
        "req_mem_mb": 8000,
        "comment": "",
    }
    cat, ev = recover.classify_failure("", sacct)
    assert cat == "recoverable_oom"
    assert "137" in ev


def test_oom_comment_oom_killer(recover):
    """sacct.Comment containing 'oom-killer' is positive OOM evidence."""
    sacct = {
        "state": "FAILED",
        "exit_code": "1:0",
        "max_rss_mb": 500,
        "req_mem_mb": 8000,
        "comment": "oom-killer killed PID 12345",
    }
    cat, ev = recover.classify_failure("", sacct)
    assert cat == "recoverable_oom"
    assert "oom" in ev.lower()


def test_failed_near_cap_is_oom(recover):
    """FAILED + peak >= 80% cap → OOM in disguise."""
    sacct = {
        "state": "FAILED",
        "exit_code": "1:0",
        "max_rss_mb": 270_000,
        "req_mem_mb": 320_000,
        "comment": "",
    }
    cat, ev = recover.classify_failure("", sacct)
    assert cat == "recoverable_oom"
    assert "80%" in ev


def test_failed_far_below_cap_is_unknown(recover):
    """FAILED + peak nowhere near cap → unknown (HARDENING #13 fix).

    This was the eval_merge regression: MaxRSS 3.4 GB on 19 GB cap was being
    mis-classified somewhere, causing a wasted bump cycle. Confirm it now
    classifies as unknown so the bump path is skipped.
    """
    sacct = {
        "state": "FAILED",
        "exit_code": "1:0",
        "max_rss_mb": 3400,
        "req_mem_mb": 19000,
        "comment": "",
    }
    cat, ev = recover.classify_failure(
        "Traceback (most recent call last):\n"
        '  File "eval_merge.py", line 42\n'
        "ValueError: bad merge state\n",
        sacct,
    )
    assert cat == "unknown"


def test_log_match_oom_killer(recover):
    """Log line containing 'oom-killer' triggers OOM."""
    log = "[1234.567890] oom-killer: gfp_mask=0x0, order=0\n"
    cat, ev = recover.classify_failure(log, None)
    assert cat == "recoverable_oom"


def test_log_match_slurmstepd(recover):
    """Log line with slurmstepd Detected N oom_kill triggers OOM."""
    log = "slurmstepd: error: Detected 1 oom_kill event in StepId=1234.batch\n"
    cat, ev = recover.classify_failure(log, None)
    assert cat == "recoverable_oom"


def test_log_match_oomkilled_word(recover):
    """Strict \\bOOMKilled\\b matches but not substring noise."""
    cat, _ = recover.classify_failure("Status: OOMKilled\n", None)
    assert cat == "recoverable_oom"


def test_loose_oom_mention_no_longer_matches(recover):
    """A traceback that mentions 'Out Of Memory' as descriptive text only
    must NOT trigger recoverable_oom anymore (HARDENING #13 fix). Used to
    match via the broad re.IGNORECASE pattern."""
    log = (
        "Traceback (most recent call last):\n"
        '  File "foo.py", line 10, in bar\n'
        "  ValueError: User reported the system was Out Of Memory yesterday\n"
    )
    cat, _ = recover.classify_failure(log, None)
    assert cat == "unknown"


def test_bare_OUT_OF_MEMORY_token_no_longer_matches(recover):
    """A log line that mentions 'OUT_OF_MEMORY' in a status table without it
    being the actual job state should not classify as OOM. Now requires sacct
    or a structured slurmstepd/oom-killer log line."""
    log = (
        "Previous job statuses: jobid=1 state=COMPLETED, "
        "jobid=2 state=OUT_OF_MEMORY  # historical record\n"
        "Current job exited with ValueError.\n"
    )
    cat, _ = recover.classify_failure(log, None)
    assert cat == "unknown"


def test_config_mismatch_unaffected(recover):
    """ValueError: Unknown <key>: <value> still classifies as config_mismatch."""
    log = (
        "ValueError: Unknown cp_method: cp_multichannel. "
        "Choose cp_measure, cp_emulator\n"
    )
    cat, _ = recover.classify_failure(log, None)
    assert cat == "config_mismatch"


def test_nfs_latency_unaffected(recover):
    """MissingOutputException still classifies as nfs_latency."""
    log = (
        "MissingOutputException: Missing files after 60 seconds. This "
        "might be due to filesystem latency.\n"
    )
    cat, _ = recover.classify_failure(log, None)
    assert cat == "nfs_latency"
