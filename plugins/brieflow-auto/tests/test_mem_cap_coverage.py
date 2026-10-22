"""Tests for validate_mem_cap_coverage in brieflow_preflight.py (HARDENING #18).

Cross-references resources-less rules against mem_recommendations.json and
slurm/config.yaml set-resources. Flags uncovered rules as HIGH severity.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_preflight.py"


@pytest.fixture(scope="module")
def preflight():
    spec = importlib.util.spec_from_file_location("brieflow_preflight", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_preflight"] = mod
    spec.loader.exec_module(mod)
    return mod


def _surface(preflight, rules: list[str]):
    """Build a BrieflowSurface with the given rules as resources-less."""
    s = preflight.BrieflowSurface()
    s.rules_without_resources = [
        {"rule": r, "file": "workflow/rules/fake.smk", "line": 1, "has_mem_mb": False}
        for r in rules
    ]
    return s


def test_uncovered_rules_flagged_high(preflight):
    """Resources-less rule with no mem_rec entry and no slurm override → HIGH."""
    surface = _surface(preflight, ["merge_phenotype", "prepare_bootstrap_data"])
    issues = preflight.validate_mem_cap_coverage(
        surface, mem_recs={}, slurm_set_resources={}
    )
    high = [i for i in issues if i.severity == "HIGH"]
    assert len(high) == 1
    assert high[0].code == "MEM_CAP_UNCOVERED"
    assert "merge_phenotype" in high[0].suggestion
    assert "prepare_bootstrap_data" in high[0].suggestion


def test_mem_recs_coverage(preflight):
    """A rule with a mem_mb_recommended entry → covered."""
    surface = _surface(preflight, ["merge_phenotype"])
    mem_recs = {"merge_phenotype": {"mem_mb_recommended": 128000}}
    issues = preflight.validate_mem_cap_coverage(surface, mem_recs, {})
    assert all(i.severity != "HIGH" or i.code != "MEM_CAP_UNCOVERED" for i in issues)


def test_slurm_set_resources_coverage(preflight):
    """A rule with a slurm set-resources override → covered."""
    surface = _surface(preflight, ["combine_metadata_sbs"])
    issues = preflight.validate_mem_cap_coverage(
        surface,
        mem_recs={},
        slurm_set_resources={"combine_metadata_sbs": 2500},
    )
    assert all(i.severity != "HIGH" or i.code != "MEM_CAP_UNCOVERED" for i in issues)


def test_group_stub_coverage(preflight):
    """A constituent rule's group_member_stub resolves to a group with a cap."""
    surface = _surface(preflight, ["align_sbs"])
    mem_recs = {
        "align_sbs": {"group": "sbs_tile_group", "obs_source": "group_member_stub"},
        "sbs_tile_group": {"mem_mb_recommended": 6100, "group_members": ["align_sbs"]},
    }
    issues = preflight.validate_mem_cap_coverage(surface, mem_recs, {})
    assert all(i.severity != "HIGH" or i.code != "MEM_CAP_UNCOVERED" for i in issues)


def test_group_stub_orphan_not_covered(preflight):
    """A group stub whose parent has no cap → NOT covered."""
    surface = _surface(preflight, ["align_sbs"])
    mem_recs = {
        "align_sbs": {"group": "sbs_tile_group", "obs_source": "group_member_stub"},
        "sbs_tile_group": {"group_members": ["align_sbs"]},  # no mem_mb_recommended
    }
    issues = preflight.validate_mem_cap_coverage(surface, mem_recs, {})
    high = [i for i in issues if i.severity == "HIGH"]
    assert len(high) == 1
    assert "align_sbs" in high[0].suggestion


def test_no_resourceless_rules(preflight):
    """Empty surface → no issues."""
    surface = preflight.BrieflowSurface()
    issues = preflight.validate_mem_cap_coverage(
        surface, mem_recs={}, slurm_set_resources={}
    )
    assert issues == []


def test_all_covered_info(preflight):
    """All rules covered → INFO-severity entry confirming coverage."""
    surface = _surface(preflight, ["merge_phenotype", "combine_metadata_sbs"])
    issues = preflight.validate_mem_cap_coverage(
        surface,
        mem_recs={"merge_phenotype": {"mem_mb_recommended": 128000}},
        slurm_set_resources={"combine_metadata_sbs": 2500},
    )
    info = [i for i in issues if i.code == "MEM_CAP_COVERED"]
    assert len(info) == 1
    assert info[0].severity == "INFO"


def test_load_slurm_set_resources(preflight, tmp_path: Path):
    """Round-trip slurm/config.yaml parsing."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "default-resources:\n"
        "  mem_mb: 3000\n"
        "set-resources:\n"
        "  merge_phenotype:\n"
        "    mem_mb: 128000\n"
        "  combine_metadata_sbs:\n"
        "    mem_mb: 2500\n"
        "  bad_rule:\n"  # no mem_mb — must be skipped
        "    threads: 4\n"
    )
    out = preflight.load_slurm_set_resources(p)
    assert out == {"merge_phenotype": 128000, "combine_metadata_sbs": 2500}


def test_load_slurm_set_resources_missing_file(preflight, tmp_path: Path):
    assert preflight.load_slurm_set_resources(tmp_path / "nope.yaml") == {}


def test_load_mem_recs(preflight, tmp_path: Path):
    p = tmp_path / "mem_recs.json"
    p.write_text(json.dumps({"merge_phenotype": {"mem_mb_recommended": 128000}}))
    assert preflight.load_mem_recs(p) == {
        "merge_phenotype": {"mem_mb_recommended": 128000}
    }


def test_load_mem_recs_missing(preflight, tmp_path: Path):
    assert preflight.load_mem_recs(tmp_path / "nope.json") == {}


def test_scaling_formula_coverage(preflight):
    """A resources-less rule with a scaling formula → covered (resolve_resources
    sizes it at run time from geometry), so no HIGH flag."""
    surface = _surface(preflight, ["segment_sbs", "format_cluster_anndata"])
    issues = preflight.validate_mem_cap_coverage(
        surface,
        mem_recs={},
        slurm_set_resources={},
        scaling_rules={"segment_sbs", "format_cluster_anndata"},
    )
    assert all(i.code != "MEM_CAP_UNCOVERED" for i in issues)


def test_drift_rule_not_in_any_layer_flagged(preflight):
    """A brand-new brieflow rule covered by no layer → still HIGH (drift signal)."""
    surface = _surface(preflight, ["brand_new_rule"])
    issues = preflight.validate_mem_cap_coverage(
        surface, mem_recs={}, slurm_set_resources={}, scaling_rules={"segment_sbs"}
    )
    high = [i for i in issues if i.severity == "HIGH" and i.code == "MEM_CAP_UNCOVERED"]
    assert len(high) == 1
    assert "brand_new_rule" in high[0].suggestion
    assert "scaling_formulas_baseline" in high[0].suggestion


def test_load_scaling_formula_rules_reads_plugin_baseline(preflight, tmp_path: Path):
    """No per-screen override → the plugin's bundled scaling baseline is read,
    and it covers the real compute rules the coverage check depends on."""
    rules = preflight.load_scaling_formula_rules(tmp_path)
    assert {"segment_sbs", "call_reads", "format_cluster_anndata"} <= rules


def test_load_mem_recs_with_baseline_fallthrough(preflight, tmp_path: Path):
    """No per-screen mem_recs → falls through to the plugin baseline (non-empty),
    matching brieflow_run so preflight and the run agree on coverage."""
    recs = preflight.load_mem_recs_with_baseline(tmp_path)
    assert len(recs) > 0


def test_load_mem_recs_with_baseline_prefers_per_screen(preflight, tmp_path: Path):
    """A per-screen slurm/mem_recommendations.json wins over the plugin baseline."""
    (tmp_path / "slurm").mkdir()
    (tmp_path / "slurm" / "mem_recommendations.json").write_text(
        json.dumps({"only_rule": {"mem_mb_recommended": 4096}})
    )
    recs = preflight.load_mem_recs_with_baseline(tmp_path)
    assert recs == {"only_rule": {"mem_mb_recommended": 4096}}
