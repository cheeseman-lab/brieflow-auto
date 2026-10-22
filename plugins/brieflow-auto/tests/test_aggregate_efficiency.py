"""Tests for brieflow_aggregate_efficiency.py.

Synthetic-fixture pytest checks covering the three load-bearing behaviors
called out in HARDENING_TODO #25 step 6:
 (a) normal rule with multiple observations rolls up to overhead × peak
 (b) manual_override entry preserved across re-aggregation
 (c) rule with peak ≥ 0.95 × cap flagged as peak_was_at_cap

Plus regression coverage for the observation-CSV output added for #23.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "brieflow_aggregate_efficiency.py"
)
FIXTURE_SRC = Path(__file__).resolve().parent / "fixtures" / "sample_analysis"


@pytest.fixture
def analysis_dir(tmp_path: Path) -> Path:
    """Copy the fixture analysis dir to a fresh tmp path per test."""
    dest = tmp_path / "analysis"
    shutil.copytree(FIXTURE_SRC, dest)
    return dest


def run_agg(analysis_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run aggregate_efficiency against the fixture.

    Always passes --no-baseline-seed so tests stay isolated from the
    shipped plugin baseline (otherwise baseline entries leak into the test
    output and pollute assertions about observed values).
    """
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--analysis-dir",
            str(analysis_dir),
            "--no-baseline-seed",
            "--quiet",
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def load_recs(analysis_dir: Path) -> dict:
    """Read the recs JSON from its canonical location (or fall back to the
    legacy cluster_optimization/results/ path for older fixtures)."""
    candidates = [
        analysis_dir / "slurm" / "mem_recommendations.json",
        analysis_dir / "cluster_optimization" / "results" / "mem_recommendations.json",
    ]
    for p in candidates:
        if p.exists():
            with open(p) as f:
                return json.load(f)
    raise FileNotFoundError(f"no mem_recommendations.json in {candidates}")


def test_aggregation_from_scratch(analysis_dir: Path):
    """Normal rule with multiple observations → overhead × peak ceiled to 100."""
    run_agg(analysis_dir)
    recs = load_recs(analysis_dir)

    # convert_phenotype: peak=1200 MB, overhead 1.5 → 1800 → ceil-to-100 = 1900
    assert "convert_phenotype" in recs
    cp = recs["convert_phenotype"]
    assert cp["obs_peak_rss_mb"] == 1200.0
    assert cp["obs_n"] == 3
    assert cp["mem_mb_recommended"] == 1900
    assert cp["obs_source"] == "efficiency_aggregated"
    assert cp["tier"] == "tile"
    assert "WARNING" not in cp


def test_manual_override_preserved(analysis_dir: Path):
    """Pre-existing manual_override entry must survive re-aggregation."""
    results = analysis_dir / "cluster_optimization" / "results"
    results.mkdir(parents=True, exist_ok=True)
    seed = {
        "convert_phenotype": {
            "mem_mb_recommended": 9999,
            "obs_source": "manual_override_test_seed",
            "tier": "tile",
        },
    }
    with open(results / "mem_recommendations.json", "w") as f:
        json.dump(seed, f)

    run_agg(analysis_dir)
    recs = load_recs(analysis_dir)

    # Manual override entry preserved verbatim — NOT overwritten by the
    # 1900 MB recommendation that the observations would otherwise produce.
    assert recs["convert_phenotype"]["mem_mb_recommended"] == 9999
    assert recs["convert_phenotype"]["obs_source"] == "manual_override_test_seed"


def test_peak_at_cap_flagged(analysis_dir: Path):
    """Rule whose peak ≥ 0.95 × cap gets WARNING flag (likely OOM-clipped)."""
    run_agg(analysis_dir)
    recs = load_recs(analysis_dir)

    # merge_phenotype: peak=31000, cap=32000 → 31000/32000 = 0.969 ≥ 0.95
    assert "merge_phenotype" in recs
    mp = recs["merge_phenotype"]
    assert mp["obs_peak_rss_mb"] == 31000.0
    assert mp["WARNING"] == "peak_was_at_cap_actual_peak_may_be_higher"


def test_grouped_rule_back_ref(analysis_dir: Path):
    """Every member rule gets a group back-ref. Members WITHOUT a fresh
    observation get a synthesized group_member_stub entry; members WITH a
    fresh observation keep their efficiency_aggregated entry plus the
    group back-ref.
    """
    run_agg(analysis_dir)
    recs = load_recs(analysis_dir)

    # align_sbs has a fresh observation in the fixture → its entry is from
    # the aggregator, with a group back-ref added.
    assert recs["align_sbs"]["group"] == "sbs_tile_group"
    assert recs["align_sbs"]["obs_source"] == "efficiency_aggregated"

    # extract_bases has NO observation in the fixture → synthesized stub.
    assert recs["extract_bases"]["obs_source"] == "group_member_stub"
    assert recs["extract_bases"]["group"] == "sbs_tile_group"

    # The parent group entry lists its members.
    if "sbs_tile_group" in recs:
        assert "group_members" in recs["sbs_tile_group"]
        assert "align_sbs" in recs["sbs_tile_group"]["group_members"]


def test_observation_csv_written(analysis_dir: Path):
    """Per-job observation CSV is written by default with the #23 schema."""
    run_agg(analysis_dir)
    results = analysis_dir / "cluster_optimization" / "results"
    obs_files = list(results.glob("observations_raw_*.csv"))
    assert len(obs_files) == 1, f"expected 1 observation csv, got {obs_files}"

    with open(obs_files[0]) as f:
        rows = list(csv.DictReader(f))

    # 3 convert_phenotype rows + 1 merge_phenotype + 1 align_sbs = 5
    assert len(rows) == 5
    expected_cols = {
        "rule",
        "wildcards",
        "MaxRSS_MB",
        "ReqMem_MB",
        "Elapsed_s",
        "CPU_Eff_pct",
        "source_report",
    }
    assert set(rows[0].keys()) == expected_cols

    first = rows[0]
    assert first["rule"] == "convert_phenotype"
    assert first["wildcards"] == "1_A1_001"
    assert float(first["MaxRSS_MB"]) == 500.0
    assert first["source_report"] == "efficiency_20260101_000000.log"


def test_no_observation_csv_flag(analysis_dir: Path):
    """--no-observation-csv suppresses the per-job CSV."""
    run_agg(analysis_dir, "--no-observation-csv")
    results = analysis_dir / "cluster_optimization" / "results"
    obs_files = list(results.glob("observations_raw_*.csv"))
    assert obs_files == []
