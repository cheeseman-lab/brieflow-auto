"""Tests for LocalBackend — snakemake-log parsing + exit-code OOM detection.

Synthetic snakemake log fixtures cover:
  - Plain rule failure → FailedJob with state=FAILED
  - Exit code 137 → FailedJob with state=OUT_OF_MEMORY
  - Exit code 9 → state=OUT_OF_MEMORY (SIGKILL)
  - Multiple rules failing in same log
  - No log present → empty result
  - count_oom_jobs_since reflects log-derived OOM counts
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_backend.py"


@pytest.fixture(scope="module")
def backend_mod():
    spec = importlib.util.spec_from_file_location("brieflow_backend", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_backend"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def analysis_dir(tmp_path: Path) -> Path:
    """Empty analysis dir with .snakemake/log/ ready to receive synthetic logs."""
    (tmp_path / ".snakemake" / "log").mkdir(parents=True)
    return tmp_path


def write_snakemake_log(
    analysis_dir: Path, text: str, ts_suffix: str = "20260516_120000"
) -> Path:
    p = analysis_dir / ".snakemake" / "log" / f"{ts_suffix}.snakemake.log"
    p.write_text(text)
    return p


def test_no_log_returns_empty(backend_mod, analysis_dir):
    """No master log → empty failed_jobs dict."""
    b = backend_mod.LocalBackend()
    assert b.query_failed_jobs(analysis_dir) == {}
    assert b.count_oom_jobs_since(analysis_dir, time.time() - 3600) == {}
    assert b.collect_rule_logs(analysis_dir, "x", None) == []


def test_plain_failure_classified(backend_mod, analysis_dir):
    """Rule with non-OOM exit code → state=FAILED, not OUT_OF_MEMORY."""
    write_snakemake_log(
        analysis_dir,
        """Building DAG of jobs...
Using shell: /bin/bash
Provided cores: 4

Error in rule align_sbs:
    jobid: 17
    output: align_sbs/aligned.zarr
    shell:
        python -m brieflow.align_sbs ...
        (exited with non-zero exit code 1)

Exiting because a job execution failed.
""",
    )
    b = backend_mod.LocalBackend()
    failed = b.query_failed_jobs(analysis_dir)
    assert "align_sbs" in failed
    assert failed["align_sbs"][0].state == "FAILED"
    assert failed["align_sbs"][0].exit_code == "1"


def test_exit_137_classified_as_oom(backend_mod, analysis_dir):
    """Exit code 137 → state=OUT_OF_MEMORY (kernel SIGKILL'd the process)."""
    write_snakemake_log(
        analysis_dir,
        """Error in rule extract_phenotype_cp:
    jobid: 42
    output: phenotype/cp.tsv
    shell:
        python -m brieflow.extract ...
        (exited with non-zero exit code 137)

Exiting because a job execution failed.
""",
    )
    b = backend_mod.LocalBackend()
    failed = b.query_failed_jobs(analysis_dir)
    assert failed["extract_phenotype_cp"][0].state == "OUT_OF_MEMORY"
    assert failed["extract_phenotype_cp"][0].exit_code == "137"


def test_exit_9_classified_as_oom(backend_mod, analysis_dir):
    """Exit code 9 → SIGKILL → OOM."""
    write_snakemake_log(
        analysis_dir,
        "Error in rule merge_phenotype:\n    Returncode: 9\n",
    )
    b = backend_mod.LocalBackend()
    failed = b.query_failed_jobs(analysis_dir)
    assert failed["merge_phenotype"][0].state == "OUT_OF_MEMORY"
    assert failed["merge_phenotype"][0].exit_code == "9"


def test_multiple_rules_in_one_log(backend_mod, analysis_dir):
    """Cascade-like log with multiple Error in rule blocks."""
    write_snakemake_log(
        analysis_dir,
        """Error in rule align_sbs:
    (exited with non-zero exit code 137)

Some other output here.

Error in rule extract_phenotype_cp:
    (exited with non-zero exit code 1)

Error in rule align_sbs:
    Returncode: 137
""",
    )
    b = backend_mod.LocalBackend()
    failed = b.query_failed_jobs(analysis_dir)
    assert set(failed.keys()) == {"align_sbs", "extract_phenotype_cp"}
    assert len(failed["align_sbs"]) == 2  # both 137s
    assert all(j.state == "OUT_OF_MEMORY" for j in failed["align_sbs"])
    assert failed["extract_phenotype_cp"][0].state == "FAILED"


def test_count_oom_since_reflects_log(backend_mod, analysis_dir):
    """count_oom_jobs_since returns {rule: count} for OOM-classified failures."""
    write_snakemake_log(
        analysis_dir,
        """Error in rule align_sbs:
    (exited with non-zero exit code 137)

Error in rule align_sbs:
    Returncode: 9

Error in rule extract_phenotype_cp:
    (exited with non-zero exit code 1)
""",
    )
    b = backend_mod.LocalBackend()
    counts = b.count_oom_jobs_since(analysis_dir, time.time() - 3600)
    assert counts == {"align_sbs": 2}  # extract_phenotype_cp was FAILED, not OOM


def test_collect_rule_logs_returns_master(backend_mod, analysis_dir):
    """Local has no per-rule logs; return the master log for grep-context."""
    log_p = write_snakemake_log(analysis_dir, "Error in rule x:\n   exit 1\n")
    b = backend_mod.LocalBackend()
    logs = b.collect_rule_logs(analysis_dir, "x", None)
    assert logs == [log_p]


def test_latest_log_picked_when_multiple(backend_mod, analysis_dir):
    """When .snakemake/log/ has multiple logs, take the most recent."""
    old = write_snakemake_log(analysis_dir, "old log\n", ts_suffix="20260101_000000")
    # Ensure the new log has a later mtime even on fast filesystems.
    time.sleep(0.05)
    new = write_snakemake_log(
        analysis_dir, "Error in rule x:\n  exit 1\n", ts_suffix="20260516_120000"
    )
    b = backend_mod.LocalBackend()
    logs = b.collect_rule_logs(analysis_dir, "x", None)
    assert logs == [new]
    assert old not in logs


def test_failed_job_legacy_dict_compatible(backend_mod, analysis_dir):
    """FailedJob.as_legacy_dict() works for classify_failure() consumption."""
    write_snakemake_log(
        analysis_dir,
        "Error in rule x:\n    (exited with non-zero exit code 137)\n",
    )
    b = backend_mod.LocalBackend()
    failed = b.query_failed_jobs(analysis_dir)
    legacy = failed["x"][0].as_legacy_dict()
    assert legacy["state"] == "OUT_OF_MEMORY"
    assert legacy["exit_code"] == "137"
    assert legacy["max_rss_mb"] == 0.0  # local can't sample RSS yet
    assert legacy["req_mem_mb"] == 0.0
