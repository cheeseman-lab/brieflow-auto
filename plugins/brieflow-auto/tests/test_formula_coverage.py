"""Every baseline rule must resolve to a positive cap: per_tile entries written as a flat mem_mb
used to resolve to nothing, so segment/extract/call rules ran at the 3000 MB slurm default."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "scripts"))
import brieflow_resolve_resources as rr  # noqa: E402

GEOMETRY = {
    "n_tiles_per_well_sbs": 368,
    "n_tiles_per_well_phenotype": 1460,
    "n_tiles_per_well_per_cycle_sbs": 368,
    "n_tiles_per_well_per_round_phenotype": 1460,
    "n_channel_combos": 2,
    "sgRNAs_per_gene_max": 4,
    "n_plates": 1,
}
RULES = yaml.safe_load((PLUGIN / "data" / "scaling_formulas_baseline.yaml").read_text())["rules"]


def test_every_baseline_rule_resolves_to_a_positive_cap():
    unresolved = [r for r, e in RULES.items() if not (rr.evaluate_formula(e, GEOMETRY) or 0) > 0]
    assert not unresolved, unresolved
    assert rr.evaluate_formula(RULES["extract_phenotype_cp"], GEOMETRY) == 6400


def test_ic_scales_with_sample_fraction(tmp_path):
    at_ref = rr.evaluate_formula(RULES["calculate_ic_phenotype"], {**GEOMETRY, "sample_fraction": 0.5})
    quarter = rr.evaluate_formula(RULES["calculate_ic_phenotype"], {**GEOMETRY, "sample_fraction": 0.25})
    assert at_ref == 270 * 1460 + 2000 and quarter == int(270 * 1460 * 0.5 + 2000)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yml").write_text("preprocess:\n  sample_fraction: 0.25\n")
    resolved = rr.resolve(tmp_path)
    assert "calculate_ic_phenotype" not in resolved  # no geometry yet: unresolvable, not guessed
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / ".brieflow" / "geometry.json").write_text(__import__("json").dumps(GEOMETRY))
    assert rr.resolve(tmp_path)["calculate_ic_phenotype"]["mem_mb"] == quarter


def test_new_baseline_entries():
    assert rr.evaluate_formula(RULES["annotate_cluster_anndata"], {}) > 0
    assert rr.evaluate_formula(RULES["bootstrap_gene"], {}) >= 6377


def test_positions_merge_baseline():
    assert rr.evaluate_formula(RULES["positions_merge"], GEOMETRY) == 2000
