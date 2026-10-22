"""Tests for brieflow_dryrun_preview.py (HARDENING #6 + #7 layer 2)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "brieflow_dryrun_preview.py"
)


@pytest.fixture(scope="module")
def dr():
    spec = importlib.util.spec_from_file_location("brieflow_dryrun_preview", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_dryrun_preview"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Job stats parser
# ---------------------------------------------------------------------------


def test_parse_job_stats_basic(dr):
    output = (
        "lots of stuff before\n"
        "Job stats:\n"
        "job                           count\n"
        "--------------------------  -------\n"
        "convert_phenotype                10\n"
        "convert_sbs                      90\n"
        "all_preprocess                    1\n"
        "total                           101\n"
        "\n"
        "Reasons:\n"
    )
    counts = dr.parse_job_stats(output)
    assert counts == {"convert_phenotype": 10, "convert_sbs": 90, "all_preprocess": 1}


def test_parse_job_stats_no_table(dr):
    """When dry-run output has no Job stats block (e.g. error), parser returns {}."""
    assert dr.parse_job_stats("snakemake failed to build DAG") == {}


def test_parse_job_stats_empty_table_ok(dr):
    """A Job stats: header with only total → empty pending."""
    output = "Job stats:\njob   count\n----  -----\ntotal     0\n"
    assert dr.parse_job_stats(output) == {}


# ---------------------------------------------------------------------------
# rule_phase mapping
# ---------------------------------------------------------------------------


def test_rule_phase_known_rules(dr):
    assert dr.rule_phase("convert_sbs") == "preprocess"
    assert dr.rule_phase("merge_phenotype") == "merge"
    assert dr.rule_phase("all_phenotype") == "phenotype"
    assert dr.rule_phase("sbs_tile_group") == "sbs"


def test_rule_phase_unknown(dr):
    assert dr.rule_phase("nonexistent_rule_xyz") is None


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def test_classify_clean_resume(dr):
    """Only all_<terminal> pending → clean_resume."""
    result = dr.classify({"all_preprocess": 1}, "preprocess")
    assert result.classification == "clean_resume"


def test_classify_partial_resume(dr):
    """Pending rules all in current phase → partial_resume."""
    result = dr.classify(
        {"calculate_ic_sbs": 2, "combine_metadata_sbs": 1, "all_preprocess": 1},
        "preprocess",
    )
    assert result.classification == "partial_resume"
    assert "preprocess" in result.by_phase
    assert result.total_jobs == 4


def test_classify_upstream_rerun(dr):
    """Pending rule from a PRIOR phase when terminal is sbs → upstream_rerun."""
    result = dr.classify(
        {"convert_sbs": 10, "align_sbs": 50, "all_sbs": 1},
        "sbs",
    )
    assert result.classification == "upstream_rerun"
    assert "preprocess" in result.detail or "convert_sbs" in result.detail


def test_classify_full_restart(dr):
    """Total jobs above threshold → full_restart."""
    pending = {"align_sbs": 4000, "all_sbs": 1}
    result = dr.classify(pending, "sbs", full_restart_threshold=500)
    # Both upstream_rerun (no upstream rules here actually) and full_restart
    # could fire; upstream_rerun has priority. align_sbs IS in sbs, so this
    # should be full_restart.
    assert result.classification == "full_restart"
    assert result.total_jobs == 4001


def test_classify_silently_skipped_edited_rule(dr):
    """--require-rerun X but X not in pending → silently_skipped_edited_rule."""
    result = dr.classify(
        {"all_merge": 1},
        "merge",
        require_rerun=["format_merge"],
    )
    assert result.classification == "silently_skipped_edited_rule"
    assert "format_merge" in result.silently_skipped_rules


def test_classify_require_rerun_present(dr):
    """If the required rule IS in pending, classification proceeds normally."""
    result = dr.classify(
        {"format_merge": 6, "all_merge": 1},
        "merge",
        require_rerun=["format_merge"],
    )
    assert result.classification == "partial_resume"
    assert result.silently_skipped_rules == []


def test_classify_unrecognized_rules_recorded(dr):
    """A new rule name not in PHASE_RULES is flagged in unrecognized_rules."""
    result = dr.classify({"new_mystery_rule": 5, "all_preprocess": 1}, "preprocess")
    assert "new_mystery_rule" in result.unrecognized_rules


def test_classify_downstream_rule_ok(dr):
    """A rule from a DOWNSTREAM phase (e.g. cluster) when terminal is sbs
    isn't an upstream rerun — it just means snakemake sees future work too.
    This shouldn't trigger upstream_rerun."""
    result = dr.classify(
        {"all_sbs": 1, "phate_leiden_clustering": 5},
        "sbs",
    )
    # 5 jobs total — neither upstream nor over threshold → partial_resume
    assert result.classification == "partial_resume"


def test_result_to_dict(dr):
    """to_dict serializes round-trips."""
    result = dr.classify({"all_preprocess": 1}, "preprocess")
    d = result.to_dict()
    assert d["terminal_module"] == "preprocess"
    assert d["classification"] == "clean_resume"
    assert "by_phase" in d
    assert "silently_skipped_rules" in d
