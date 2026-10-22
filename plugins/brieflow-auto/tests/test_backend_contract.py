"""Tests for the BackendAdapter contract.

Both SlurmBackend and LocalBackend must satisfy the Protocol; this file
covers method-signature equivalence + return-shape sanity. Backend-specific
behavior tests live in test_local_backend.py and test_classify_failure.py.
"""

from __future__ import annotations

import importlib.util
import sys
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


@pytest.fixture(scope="module")
def both_backends(backend_mod):
    return [backend_mod.SlurmBackend(), backend_mod.LocalBackend()]


def test_factory_returns_correct_backend(backend_mod):
    assert backend_mod.get_backend("slurm").name == "slurm"
    assert backend_mod.get_backend("local").name == "local"


def test_factory_rejects_unknown(backend_mod):
    with pytest.raises(ValueError, match="unknown backend"):
        backend_mod.get_backend("k8s")


def test_both_backends_have_required_methods(both_backends):
    for b in both_backends:
        for method in (
            "collect_rule_logs",
            "extract_jobid",
            "query_failed_jobs",
            "query_job_state",
            "count_oom_jobs_since",
            "queue_summary",
            "supports_cascade_detection",
        ):
            assert hasattr(b, method), f"{b.name} missing {method}"
            assert callable(getattr(b, method))


def test_cascade_detection_support_differs(both_backends):
    by_name = {b.name: b for b in both_backends}
    assert by_name["slurm"].supports_cascade_detection() is True
    assert by_name["local"].supports_cascade_detection() is False


def test_local_queue_summary_empty(backend_mod):
    """Local has no queue concept — always empty dict."""
    assert backend_mod.LocalBackend().queue_summary() == {}


def test_local_query_job_state_none(backend_mod):
    """Local has no persistent job records — query_job_state always None."""
    assert backend_mod.LocalBackend().query_job_state("anything") is None


def test_local_extract_jobid_none(backend_mod, tmp_path):
    """Local has no jobid concept."""
    fake_log = tmp_path / "fake.log"
    fake_log.write_text("snakemake: rule x failed")
    assert backend_mod.LocalBackend().extract_jobid(fake_log) is None


def test_failed_job_legacy_dict_shape(backend_mod):
    """FailedJob.as_legacy_dict() must match the sacct-dict shape that
    classify_failure() expects."""
    fj = backend_mod.FailedJob(
        rule="x",
        state="OUT_OF_MEMORY",
        exit_code="0:125",
        max_rss_mb=1000.0,
        req_mem_mb=2000.0,
        elapsed="00:10:00",
        jobid="12345",
    )
    d = fj.as_legacy_dict()
    assert set(d.keys()) == {
        "state",
        "exit_code",
        "max_rss_mb",
        "req_mem_mb",
        "elapsed",
    }
    assert d["state"] == "OUT_OF_MEMORY"


def test_job_state_legacy_dict_shape(backend_mod):
    """JobState.as_legacy_dict() must include jobid + comment for recover.py."""
    js = backend_mod.JobState(
        jobid="12345",
        state="FAILED",
        exit_code="1:0",
        max_rss_mb=500.0,
        req_mem_mb=8000.0,
        elapsed="00:05:00",
        comment="",
    )
    d = js.as_legacy_dict()
    assert "jobid" in d and "comment" in d


def test_parse_helpers(backend_mod):
    """RSS / Mem string parsing — edge cases that bit us historically."""
    assert backend_mod._parse_rss_to_kb("") == 0.0
    assert backend_mod._parse_rss_to_kb("500K") == 500.0
    assert backend_mod._parse_rss_to_kb("2M") == 2048.0
    assert backend_mod._parse_rss_to_kb("3G") == 3 * 1024 * 1024
    assert backend_mod._parse_rss_to_kb("garbage") == 0.0
    assert backend_mod._parse_mem_to_mb("16G") == 16384.0
    assert backend_mod._parse_mem_to_mb("32000M") == 32000.0
